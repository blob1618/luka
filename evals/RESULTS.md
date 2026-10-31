# Resultados del eval (Fase 3)

**Fecha:** 31/10/2026
**Proveedor:** DeepSeek (`deepseek-flash`, vía `LLM_PROVIDER=deepseek`)
**Corrida:** 20 escenarios × 3 repeticiones = **60 conversaciones**
**Reporte crudo:** `evals/results/eval-20261031T195111Z.json` (ignorado por git)

## Resumen

| Métrica | Valor |
|---|---|
| `tool_accuracy` | **1.000** |
| `verify_rate` | **1.000** |
| `preview_rate` (solo escrituras) | **1.000** |
| `commit_rate` (solo escrituras) | **1.000** |
| Latencia media | **2.761,8 ms** |

## Por escenario

`preview` y `commit` solo aplican a escenarios de escritura; las lecturas y aclaraciones no
deben pedir confirmación.

| Escenario | Tool | Verify | Preview | Commit | Latencia (ms) |
|---|---|---|---|---|---|
| register_expense | 1.0 | 1.0 | 1.0 | 1.0 | 2324.2 |
| register_income | 1.0 | 1.0 | 1.0 | 1.0 | 2229.7 |
| register_yesterday | 1.0 | 1.0 | 1.0 | 1.0 | 3101.9 |
| register_usd | 1.0 | 1.0 | 1.0 | 1.0 | 2169.8 |
| register_batch | 1.0 | 1.0 | 1.0 | 1.0 | 2583.8 |
| update_amount | 1.0 | 1.0 | 1.0 | 1.0 | 4037.7 |
| correct_date | 1.0 | 1.0 | 1.0 | 1.0 | 3997.1 |
| annul_taxi | 1.0 | 1.0 | 1.0 | 1.0 | 3934.4 |
| balances | 1.0 | 1.0 | — | — | 3465.1 |
| list_movements | 1.0 | 1.0 | — | — | 3798.0 |
| find_yesterday | 1.0 | 1.0 | — | — | 4030.6 |
| create_limit | 1.0 | 1.0 | 1.0 | 1.0 | 1958.8 |
| limit_missing_slots | 1.0 | 1.0 | — | — | 2339.4 |
| create_reminder | 1.0 | 1.0 | 1.0 | 1.0 | 1922.2 |
| dashboard_link | 1.0 | 1.0 | — | — | 1814.5 |
| create_wallet_usd | 1.0 | 1.0 | 1.0 | 1.0 | 1922.9 |
| rename_wallet | 1.0 | 1.0 | 1.0 | 1.0 | 2094.7 |
| financial_education | 1.0 | 1.0 | — | — | 3674.4 |
| out_of_scope | 1.0 | 1.0 | — | — | 1741.5 |
| chart_categories | 1.0 | 1.0 | — | — | 2095.5 |

## Comparación de proveedores

No se pudo completar: las keys locales de Gemini y Mistral respondieron **401 Unauthorized** en
la prueba de humo, así que no se incluyen como resultados de calidad. Para repetir la
comparación hace falta una key válida y ejecutar:

```powershell
.\.codex\venv\Scripts\python.exe evals\runner.py --runs 3 --providers deepseek,gemini,mistral
```

## Límites

- Tres repeticiones por escenario siguen siendo una muestra acotada; no es una tasa estable.
- El proveedor es no determinista: un 1.0 en esta corrida no garantiza un 1.0 permanente.
- El corpus cubre 20 flujos; no agota todos los dominios (recurrentes, gráficos avanzados,
  presupuestos con datos, categorías hijas).
- El eval no corre en CI ni reemplaza la suite hermética (1.239 pruebas) ni el E2E de staging.
