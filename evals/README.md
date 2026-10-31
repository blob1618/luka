# Eval de herramientas con modelo real (Fase 3)

Corpus mínimo para medir la elección de herramientas del agente con el proveedor real. **No
corre en CI ni en la suite**: hace llamadas de red y escribe una base SQLite descartable.

## Requisitos

- `DEEPSEEK_API_KEY` en `.env` (o `EVAL_PROVIDER` apuntando a otro proveedor configurado).
- No necesita Redis real: usa un `FakeRedis` en memoria (`evals/fake_redis.py`).
- No toca producción: fuerza `DATABASE_URL=sqlite:///./evals/eval.db`.

## Uso

```powershell
.\.codex\venv\Scripts\python.exe evals\runner.py
.\.codex\venv\Scripts\python.exe evals\runner.py --only register_expense
.\.codex\venv\Scripts\python.exe evals\runner.py --limit 3
```

Cada corrida imprime una fila por escenario y un resumen; además escribe
`evals/results/eval-<UTC>.json` con el detalle (herramientas elegidas, latencia, texto y
resultado de la verificación).

## Métricas

| Métrica | Significado |
|---|---|
| `tool_accuracy` | El modelo eligió alguna de las herramientas esperadas |
| `preview_rate` | Sobre escenarios de escritura: hubo preview con confirmación |
| `commit_rate` | Sobre escrituras: la confirmación persistió el efecto |
| `verify_rate` | El estado final de la base coincide con lo esperado |
| `avg_latency_ms` | Latencia del primer turno |

`preview_rate` y `commit_rate` se calculan solo sobre escenarios `write`; las lecturas y las
aclaraciones no deben pedir confirmación.

## Cómo agregar escenarios

Editar `evals/corpus.json`:

```json
{
  "id": "register_expense",
  "text": "gasté 1000 en taxi",
  "expect_tools": ["register_movement"],
  "kind": "write",
  "seed_movement": {"description": "taxi", "amount": "1000", "type": "egreso"},
  "verify": {"movement": {"amount": "1000", "currency": "ARS", "type": "egreso"}}
}
```

`kind`: `write` (preview + confirmación), `read` (sin confirmación) o `clarify` (el agente
debe pedir datos faltantes y no escribir). `verify` admite `movement`, `limit`, `no_limit`,
`reminder`, `reply_contains` y `event_key`.

## Límites

- Una corrida por escenario: mide el comportamiento de una muestra, no una tasa estable.
- El proveedor es no determinista; un fallo aislado no es una regresión.
- No reemplaza la suite hermética ni la aceptación manual; es evidencia para la tesis.
