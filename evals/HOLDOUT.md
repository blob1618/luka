# Conjunto retenido (held-out)

**Propósito:** medir generalización sobre escenarios que no se usaron para depurar el corpus de
desarrollo. No se ajustan sus prompts después de ver los resultados; si se cambia un escenario,
se registra como una nueva versión y se reporta aparte.

**Corrida válida:** 31/10/2026, DeepSeek Flash, 10 escenarios × 3 repeticiones = 30
conversaciones. Reporte crudo: `evals/results/eval-20261031T203332Z.json`.

## Resultado

| Métrica | Valor |
|---|---|
| `tool_accuracy` | **1.000** |
| `verify_rate` | **1.000** |
| `preview_rate` (escrituras) | **1.000** |
| `commit_rate` (escrituras) | **1.000** |
| Latencia media | **2.863,9 ms** |

Escenarios: registro de transporte, ingreso en USD, cambio de descripción, anulación de ingreso,
listado de ingresos, límite en transporte, recordatorio de alquiler, renombre de billetera,
fondo de emergencia y gráfico mensual.

## Primera corrida: qué encontró

La primera corrida (reporte `eval-20261031T203132Z.json`) dio `tool_accuracy=0.867` y
`verify_rate=0.9`. Dos casos:

1. **`create_limit_transport` (0/3): defecto del fixture, no del modelo.** El seed solo creaba
   la categoría «Comida»; el agente pidió correctamente elegir una categoría existente. Se
   agregó «Transporte» al setup y se repitió. El prompt no se tocó.
2. **`chart_monthly` (2/3): variación real del modelo.** En una de las tres corridas respondió
   «Necesito que aclares qué operación necesitás» en lugar de llamar `movement_chart`. En la
   segunda corrida las tres salieron bien.

## Lectura honesta

- El 30/30 posterior **no borra** la variación observada: es otra muestra, no una prueba de que
  el modelo sea determinista.
- La primera corrida también sirvió para detectar un error de setup; corregirlo es legítimo
  porque el escenario no era válido, pero significa que la segunda corrida ya no es
  completamente “a ciegas”.
- El set queda **congelado**. Cualquier cambio futuro se versiona y se reporta como una nueva
  muestra, no se presenta como el mismo holdout.
- 30 conversaciones siguen siendo una muestra chica; el resultado se reporta con esa salvedad.

## Reproducir

```powershell
.\.codex\venv\Scripts\python.exe evals\runner.py --corpus evals\holdout.json --runs 3 --providers deepseek
```
