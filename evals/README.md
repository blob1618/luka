# Eval de herramientas con modelo real (Fase 3)

Corpus de 20 escenarios para medir la elección de herramientas del agente con el proveedor
real. **No corre en CI ni en la suite**: hace llamadas de red y escribe una base SQLite
descartable.

## Requisitos

- `DEEPSEEK_API_KEY` en `.env` para el proveedor por defecto.
- Para comparar, `GEMINI_API_KEY` y/o `MISTRAL_API_KEY` válidas (el runner omite el proveedor
  si falta la key; si la key es inválida, las corridas fallan y quedan registradas como error).
- No necesita Redis real: usa un `FakeRedis` en memoria (`evals/fake_redis.py`).
- No toca producción: fuerza `DATABASE_URL=sqlite:///./evals/eval.db`.

## Uso

```powershell
.\.codex\venv\Scripts\python.exe evals\runner.py
.\.codex\venv\Scripts\python.exe evals\runner.py --runs 3
.\.codex\venv\Scripts\python.exe evals\runner.py --providers deepseek,gemini --runs 2
.\.codex\venv\Scripts\python.exe evals\runner.py --corpus evals\holdout.json --runs 3
.\.codex\venv\Scripts\python.exe evals\runner.py --only register_expense --runs 5
.\.codex\venv\Scripts\python.exe evals\runner.py --limit 3 --runs 1
```

`--corpus` permite usar el set de desarrollo (`corpus.json`, por defecto) o el retenido
(`holdout.json`). El set retenido no se ajusta después de ver los resultados: sus métricas están
en [HOLDOUT.md](HOLDOUT.md).

Cada corrida imprime una fila **agregada por escenario** y un resumen por proveedor; además
escribe `evals/results/eval-<UTC>.json` con cada repetición, las herramientas elegidas, la
latencia y el resultado de la verificación. Los resultados de la última corrida están
resumidos en [RESULTS.md](RESULTS.md).

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
debe pedir datos faltantes y no escribir). `verify` admite `movement`, `movements`, `wallet`,
`limit`, `no_limit`, `reminder`, `no_movement`, `reply_contains` y `reply_nonempty`.

## Límites

- Tres repeticiones por escenario siguen siendo una muestra acotada, no una tasa estable.
- El proveedor es no determinista; un fallo aislado no es una regresión.
- La comparación entre proveedores depende de keys válidas; Gemini y Mistral respondieron 401
  en la última prueba local.
- No reemplaza la suite hermética ni la aceptación manual; es evidencia para la tesis.
