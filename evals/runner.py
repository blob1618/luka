"""Fase 3: evalúa elección de herramientas con un modelo real sobre un corpus.

Uso:
    .codex/venv/Scripts/python.exe evals/runner.py
    ... --only register_expense
    ... --limit 3

No corre en CI ni en la suite: hace llamadas reales al proveedor y escribe
`evals/eval.db` y un reporte JSON en `evals/results/`.
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
from unittest.mock import AsyncMock
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')
os.environ['DATABASE_URL'] = 'sqlite:///./evals/eval.db'
os.environ['LLM_PROVIDER'] = os.getenv('EVAL_PROVIDER', 'deepseek')

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
from evals.fake_redis import FakeRedis  # noqa: E402

CORPUS = json.loads((Path(__file__).parent / 'corpus.json').read_text(encoding='utf-8'))['scenarios']


def seed(scenario, index):
    owner = uuid4()
    phone = f'54911{index:08d}'
    with SessionLocal() as session:
        session.add(Usuario(id=owner, nombre='Eval', email=f'eval{index}@example.invalid',
                            whatsapp_id=phone, auth_user_id=uuid4()))
        wallet = Billetera(usuario_id=owner, nombre='Pesos', moneda='ARS')
        session.add_all([
            wallet,
            Billetera(usuario_id=owner, nombre='Dólares', moneda='USD'),
            Categoria(usuario_id=owner, nombre='Comida'),
        ])
        session.flush()
        seed_movement = scenario.get('seed_movement')
        if seed_movement:
            session.add(MovimientoFinanciero(
                usuario_id=owner, billetera_id=wallet.id, tipo=seed_movement['type'],
                cantidad=Decimal(seed_movement['amount']), moneda='ARS',
                descripcion=seed_movement['description'], fecha_movimiento=date.today()))
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
            elif key == 'reply_contains':
                if expected.lower() not in reply.text.lower():
                    return False, 'texto sin el dato esperado'
            elif key == 'event_key':
                if reply.event_key != expected:
                    return False, f'event_key {reply.event_key}'
    return True, 'ok'


async def run_scenario(scenario, index, fake):
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


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', action='append', default=[])
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()

    if os.getenv('LLM_PROVIDER') == 'deepseek' and not os.getenv('DEEPSEEK_API_KEY'):
        sys.exit('Falta DEEPSEEK_API_KEY (o usá EVAL_PROVIDER con otro proveedor configurado).')

    (ROOT / 'evals' / 'results').mkdir(exist_ok=True)
    (ROOT / 'evals' / 'eval.db').unlink(missing_ok=True)
    Base.metadata.create_all(engine)

    scenarios = [item for item in CORPUS if not args.only or item['id'] in args.only]
    if args.limit:
        scenarios = scenarios[:args.limit]

    fake = FakeRedis()
    rows = []
    for index, scenario in enumerate(scenarios, start=1):
        try:
            row = await run_scenario(scenario, index, fake)
        except Exception as exc:  # noqa: BLE001 - el eval reporta el fallo y sigue
            row = {'id': scenario['id'], 'kind': scenario['kind'], 'tools': [],
                   'expected_tools': scenario['expect_tools'],
                   'tool_hit': False, 'preview': False, 'committed': False, 'verify': False,
                   'detail': f'{type(exc).__name__}: {exc}', 'latency_ms': 0, 'text': ''}
        rows.append(row)
        print(f"{'OK ' if row['tool_hit'] and row['verify'] else 'FAIL'} {row['id']:<20} "
              f"tools={row['tools']} preview={row['preview']} verify={row['detail']} {row['latency_ms']}ms")

    total = len(rows) or 1
    writes = [row for row in rows if row['kind'] == 'write']
    summary = {
        'scenarios': len(rows),
        'tool_accuracy': round(sum(row['tool_hit'] for row in rows) / total, 3),
        'verify_rate': round(sum(row['verify'] for row in rows) / total, 3),
        'preview_rate': round(sum(row['preview'] for row in writes) / len(writes), 3) if writes else None,
        'commit_rate': round(sum(row['committed'] for row in writes) / len(writes), 3) if writes else None,
        'avg_latency_ms': round(sum(row['latency_ms'] for row in rows) / total, 1),
    }
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = {'summary': summary, 'rows': rows, 'provider': os.getenv('LLM_PROVIDER')}
    (ROOT / 'evals' / 'results' / f'eval-{stamp}.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
