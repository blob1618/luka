"""Fase 3: evalúa elección de herramientas con modelos reales sobre un corpus.

Uso:
    .codex/venv/Scripts/python.exe evals/runner.py
    ... --runs 3 --providers deepseek
    ... --providers deepseek,gemini --runs 2
    ... --only register_expense --runs 5

No corre en CI ni en la suite: hace llamadas reales y escribe `evals/eval.db`
y un reporte JSON en `evals/results/`.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean
from unittest.mock import AsyncMock
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')
os.environ['DATABASE_URL'] = 'sqlite:///./evals/eval.db'

from app.models.database import (  # noqa: E402
    Base,
    Billetera,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Recordatorio,
    SessionLocal,
    Usuario,
    engine,
)
from app.services import assistant_runtime  # noqa: E402
from app.services.conversation import ConversationService  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from evals.fake_redis import FakeRedis  # noqa: E402

PROVIDER_KEYS = {'deepseek': 'DEEPSEEK_API_KEY', 'gemini': 'GEMINI_API_KEY', 'mistral': 'MISTRAL_API_KEY'}


def seed(scenario, index):
    owner = uuid4()
    phone = f'54911{index:08d}'
    with SessionLocal() as session:
        session.add(Usuario(id=owner, nombre='Eval', email=f'eval{index}@example.invalid',
                            whatsapp_id=phone))
        wallets = {}
        for currency in scenario.get('seed_wallets', ['ARS']):
            wallet = Billetera(
                usuario_id=owner,
                nombre='Pesos' if currency == 'ARS' else 'Dólares',
                moneda=currency,
            )
            session.add(wallet)
            wallets[currency] = wallet
        category = Categoria(usuario_id=owner, nombre='Comida')
        session.add(category)
        session.flush()
        categories = {'Comida': category}
        for name in scenario.get('seed_categories', []):
            if name in categories:
                continue
            extra = Categoria(usuario_id=owner, nombre=name)
            session.add(extra)
            session.flush()
            categories[name] = extra

        movements = scenario.get('seed_movements') or (
            [scenario['seed_movement']] if scenario.get('seed_movement') else [])
        for item in movements:
            currency = item.get('currency', 'ARS')
            session.add(MovimientoFinanciero(
                usuario_id=owner, billetera_id=wallets[currency].id,
                tipo=item.get('type', 'egreso'), cantidad=Decimal(item['amount']),
                moneda=currency, descripcion=item['description'],
                fecha_movimiento=date.today() + timedelta(days=item.get('date_offset', 0))))

        limit = scenario.get('seed_limit')
        if limit:
            limit_category = categories.get(limit.get('category', 'Comida'), category)
            session.add(LimiteCategoria(
                usuario_id=owner, categoria_id=limit_category.id,
                cantidad_max=Decimal(limit['amount']), moneda='ARS',
                inicio_periodo=date.today().replace(day=1),
                fin_periodo=date.today() + timedelta(days=30)))

        reminder = scenario.get('seed_reminder')
        if reminder:
            session.add(Recordatorio(
                usuario_id=owner, titulo=reminder['title'], dia_del_mes=10,
                monto=Decimal('1000'), moneda='ARS', estado='activo'))

        session.commit()
    return owner, phone


def _latest_movement(session, owner):
    return (session.query(MovimientoFinanciero)
            .filter(MovimientoFinanciero.usuario_id == owner)
            .order_by(MovimientoFinanciero.creado_en.desc())
            .first())


def verify(scenario, owner, reply):
    checks = scenario.get('verify', {})
    with SessionLocal() as session:
        for key, expected in checks.items():
            if key == 'movement':
                movement = _latest_movement(session, owner)
                if movement is None:
                    return False, 'sin movimiento'
                if 'amount' in expected and Decimal(movement.cantidad) != Decimal(expected['amount']):
                    return False, f'importe {movement.cantidad}'
                if 'currency' in expected and movement.moneda != expected['currency']:
                    return False, f'moneda {movement.moneda}'
                if 'type' in expected and movement.tipo != expected['type']:
                    return False, f'tipo {movement.tipo}'
                if 'description_contains' in expected and expected['description_contains'].lower() not in (movement.descripcion or '').lower():
                    return False, f'descripción {movement.descripcion}'
                if 'date_offset' in expected:
                    wanted = date.today() + timedelta(days=expected['date_offset'])
                    if movement.fecha_movimiento != wanted:
                        return False, f'fecha {movement.fecha_movimiento}'
                if expected.get('annulled') and movement.anulado_en is None:
                    return False, 'no anulado'
            elif key == 'movements':
                rows = (session.query(MovimientoFinanciero)
                        .filter(MovimientoFinanciero.usuario_id == owner).all())
                if 'count' in expected and len(rows) != expected['count']:
                    return False, f'cantidad {len(rows)}'
                if 'amounts' in expected:
                    amounts = {str(Decimal(row.cantidad).normalize()) for row in rows}
                    wanted = {str(Decimal(amount).normalize()) for amount in expected['amounts']}
                    if amounts != wanted:
                        return False, f'importes {sorted(amounts)}'
            elif key == 'wallet':
                wallet = (session.query(Billetera)
                          .filter_by(usuario_id=owner, moneda=expected['currency']).first())
                if expected.get('exists') and wallet is None:
                    return False, f'sin billetera {expected["currency"]}'
                if 'name' in expected and (wallet is None or wallet.nombre != expected['name']):
                    return False, f'nombre {wallet.nombre if wallet else None}'
            elif key == 'limit':
                limit = session.query(LimiteCategoria).filter_by(usuario_id=owner).first()
                if limit is None:
                    return False, 'sin límite'
                if 'amount' in expected and Decimal(limit.cantidad_max) != Decimal(expected['amount']):
                    return False, f'límite {limit.cantidad_max}'
            elif key == 'no_limit':
                if session.query(LimiteCategoria).filter_by(usuario_id=owner).count():
                    return False, 'límite inesperado'
            elif key == 'reminder':
                reminder = session.query(Recordatorio).filter_by(usuario_id=owner).first()
                if reminder is None:
                    return False, 'sin recordatorio'
                if 'title_contains' in expected and expected['title_contains'].lower() not in reminder.titulo.lower():
                    return False, f'título {reminder.titulo}'
            elif key == 'no_movement':
                if session.query(MovimientoFinanciero).filter_by(usuario_id=owner).count():
                    return False, 'movimiento inesperado'
            elif key == 'reply_contains':
                if expected.lower() not in reply.text.lower():
                    return False, 'texto sin el dato esperado'
            elif key == 'reply_nonempty':
                if not reply.text.strip():
                    return False, 'respuesta vacía'
    return True, 'ok'


async def run_once(scenario, index, fake):
    ConversationService._get_client = AsyncMock(return_value=fake)
    ConversationService._client = None
    ConversationService._loop_id = None
    owner, phone = seed(scenario, index)
    started = time.perf_counter()
    reply = await assistant_runtime.run_for_phone(
        phone, f'eval-{scenario["id"]}-{uuid4().hex}', scenario['text'],
        now=datetime.now(timezone.utc))
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    tools = [entry.get('tool') for entry in reply.trace]
    expected_hit = any(tool in scenario['expect_tools'] for tool in tools)
    preview = reply.confirmation_action is not None

    committed = None
    if scenario['kind'] == 'write' and preview:
        if reply.preview_delivery is not None:
            await assistant_runtime.record_preview_delivered(
                phone, reply.preview_delivery, now=datetime.now(timezone.utc))
        committed = await assistant_runtime.run_for_phone(
            phone, f'eval-confirm-{uuid4().hex}', action=reply.confirmation_action,
            now=datetime.now(timezone.utc))
    verified, detail = verify(scenario, owner, committed or reply)

    return {
        'id': scenario['id'],
        'kind': scenario['kind'],
        'tools': tools,
        'expected_tools': scenario['expect_tools'],
        'tool_hit': expected_hit,
        'preview': preview,
        'committed': committed is not None and committed.committed is not None,
        'verify': verified,
        'detail': detail,
        'latency_ms': latency_ms,
        'text': reply.text[:160],
    }


def aggregate(rows):
    total = len(rows) or 1
    writes = [row for row in rows if row['kind'] == 'write']
    return {
        'runs': len(rows),
        'tool_accuracy': round(sum(row['tool_hit'] for row in rows) / total, 3),
        'verify_rate': round(sum(row['verify'] for row in rows) / total, 3),
        'preview_rate': round(sum(row['preview'] for row in writes) / len(writes), 3) if writes else None,
        'commit_rate': round(sum(row['committed'] for row in writes) / len(writes), 3) if writes else None,
        'avg_latency_ms': round(mean(row['latency_ms'] for row in rows), 1),
    }


async def run_provider(provider, scenarios, runs, fake, counter):
    os.environ['LLM_PROVIDER'] = provider
    LLMService._provider = None
    report = {}
    all_rows = []
    for scenario in scenarios:
        rows = []
        for _ in range(runs):
            counter += 1
            try:
                rows.append(await run_once(scenario, counter, fake))
            except Exception as exc:  # noqa: BLE001 - el eval reporta y sigue
                rows.append({'id': scenario['id'], 'kind': scenario['kind'], 'tools': [],
                             'expected_tools': scenario['expect_tools'], 'tool_hit': False,
                             'preview': False, 'committed': False, 'verify': False,
                             'detail': f'{type(exc).__name__}: {exc}', 'latency_ms': 0, 'text': ''})
        all_rows.extend(rows)
        report[scenario['id']] = {'runs': rows, 'aggregate': aggregate(rows)}
        item = report[scenario['id']]['aggregate']
        print(f"{provider:<8} {scenario['id']:<20} acc={item['tool_accuracy']:.2f} "
              f"verify={item['verify_rate']:.2f} preview={item['preview_rate']} "
              f"commit={item['commit_rate']} {item['avg_latency_ms']}ms")
    return report, all_rows


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', action='append', default=[])
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--providers', default='deepseek')
    parser.add_argument('--corpus', default=str(Path(__file__).parent / 'corpus.json'))
    args = parser.parse_args()

    (ROOT / 'evals' / 'results').mkdir(exist_ok=True)
    (ROOT / 'evals' / 'eval.db').unlink(missing_ok=True)
    Base.metadata.create_all(engine)

    corpus = json.loads(Path(args.corpus).read_text(encoding='utf-8'))['scenarios']
    scenarios = [item for item in corpus if not args.only or item['id'] in args.only]
    if args.limit:
        scenarios = scenarios[:args.limit]

    fake = FakeRedis()
    counter = 0
    providers_report = {}
    for provider in [item.strip() for item in args.providers.split(',') if item.strip()]:
        key = PROVIDER_KEYS.get(provider)
        if key and not os.getenv(key):
            print(f'skip {provider}: falta {key}')
            continue
        report, rows = await run_provider(provider, scenarios, args.runs, fake, counter)
        counter += len(rows)
        summary = aggregate(rows)
        providers_report[provider] = {'summary': summary, 'scenarios': report}
        print(f"{provider} SUMMARY {json.dumps(summary, ensure_ascii=False)}")

    if not providers_report:
        sys.exit('No se ejecutó ningún proveedor; faltan API keys.')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    payload = {'generated_at': stamp, 'runs_per_scenario': args.runs, 'providers': providers_report}
    target = ROOT / 'evals' / 'results' / f'eval-{stamp}.json'
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'report={target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
