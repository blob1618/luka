# Bitácora de pruebas — STK-52 / HU-EDU-02

- **Historia:** [STK-52 — HU-EDU-02 Recomendaciones de inversión](https://philippem.atlassian.net/browse/STK-52)
- **PR:** [#66 — Orientación educativa de solo lectura ante excedentes](https://github.com/blob1618/luka/pull/66)
- **Rama revisada:** `codex/HU-EDU-02-orientacion-educativa`
- **Commit de implementación:** `6eb93a91797944633e395fa5883c1cb160703c18`
- **Última corrida registrada:** 2026-10-15

## Estado general

La corrida automatizada focalizada pasó: **153 pruebas aprobadas**. También pasó
la regresión completa del backend: **1.382 pruebas aprobadas**. Los seis
criterios tienen cobertura automatizada identificada abajo. Esto confirma la
comprobación técnica local; no equivale a aceptación humana ni a una corrida en
el simulador.

## Alternativa end-to-end sin Docker/Podman

La prueba `test_acceptance_positive_surplus_is_read_only_and_isolated` recorre
el mensaje entrante por el dispatcher hasta la respuesta educativa, con SQLite
aislada y datos sintéticos. También comprueba el período, el total, las
salvedades, que no se llame al LLM y que movimientos/límites no cambien. Ya pasó
dentro de la corrida focalizada del 2026-10-15. Las demás pruebas automatizadas
cubren cero/déficit, multimoneda, falta de datos y contenido educativo; también
pasaron en esa corrida.

Esta alternativa verifica el flujo de backend de extremo a extremo sin
instalar un runtime de contenedores. No prueba la presentación/interacción de la
interfaz Streamlit ni equivale a un recorrido manual o a una conversación real
de WhatsApp. Esos puntos quedan separados como pendientes manuales.

La verificación manual en el simulador de WhatsApp sigue **pendiente**. No se
registraron pruebas en WhatsApp real, cambios en Jira, merge ni despliegue.

## Criterios de aceptación y evidencia automatizada

| Criterio | Qué se comprueba | Pruebas automatizadas relacionadas | Estado al 2026-10-15 |
| --- | --- | --- | --- |
| **CA-1 — Excedente registrado** | Período cerrado, moneda e importe; salvedad por datos faltantes/pagos futuros; oferta de orientación general sin elegir inversión. | `test_acceptance_positive_surplus_is_read_only_and_isolated`; `test_render_balance_uses_only_the_monthly_balance_result` | **Aprobado** — incluye ARS 100.000 − ARS 30.000 = ARS 70.000 para septiembre 2026 y verifica ambas salvedades. |
| **CA-2 — Alternativas a pedido** | Finalidad, horizonte, disponibilidad, riesgos, límites, fuentes y fecha de revisión; sin promesas ni recomendación personalizada. | `test_catalog_covers_required_topics_kinds_and_dimensions`; `test_render_topic_includes_all_dimensions_sources_and_review_date`; `test_rendered_content_has_no_prohibited_claims`; `test_guidance_topic_uses_the_versioned_catalog` | **Aprobado** — catálogo versionado cubre reserva, meta de ahorro, plazo fijo, FCI y comparación. |
| **CA-3 — Sin excedente inventado** | Cero o déficit informa el resultado y no afirma que haya dinero disponible para invertir. | `test_zero_difference_is_not_a_positive_surplus`; `test_negative_difference_is_not_a_positive_surplus`; `test_statuses_never_become_zero_or_guessed_values` | **Aprobado** — casos de cero y negativo cubiertos. |
| **CA-4 — Monedas separadas** | ARS y USD se presentan por separado; no se agregan sin conversión explícita. | `test_multiple_currencies_are_reported_separately` y casos de balance en `tests/test_monthly_balance.py` | **Aprobado** — prueba focal incluida en la corrida. |
| **CA-5 — Datos insuficientes** | Ausencia de movimientos, teléfono no vinculado o error no se convierten en un excedente ni en cero inventado. | `test_no_data_is_reported_without_inventing_zero`; `test_user_not_found_is_reported_without_guessing`; `test_balance_error_is_reported_without_guessing`; `test_acceptance_positive_surplus_is_read_only_and_isolated` | **Aprobado** — también se excluyen anulados, movimientos de otro usuario y movimientos fuera del período. |
| **CA-6 — Sin escrituras ni órdenes** | La orientación no modifica movimientos o límites, no llama al LLM para la ruta de balance y no existe una entidad de billetera; una intención de operar no se interpreta como educación. | `test_acceptance_positive_surplus_is_read_only_and_isolated`; `test_balance_request_is_read_only_and_never_calls_llm`; `test_current_schema_defines_no_wallet_entity`; `test_action_intent_on_an_instrument_is_never_educational` | **Aprobado** — los snapshots de movimientos/límites permanecen iguales y no se invoca el LLM en el caso integrado. |

## Registro de corrida automatizada

| Fecha | Comando | Resultado | Observaciones |
| --- | --- | --- | --- |
| 2026-10-15 | `.venv\Scripts\python.exe -m pytest -q tests/test_educational_route.py::test_acceptance_positive_surplus_is_read_only_and_isolated` | **1 passed** en 0,09 s | Corrida puntual del flujo end-to-end de backend, sin Docker/Podman. 1 aviso de `pytest-asyncio` sobre el event loop. |
| 2026-10-15 | `scripts/test_hu_edu_02.ps1` | **153 passed** en 0,95 s | Python 3.10.7. 12 avisos `DeprecationWarning` de `pytest-asyncio` sobre el event loop; sin fallos. |
| 2026-10-15 | `scripts/test_hu_edu_02.ps1 -Full` | Focal: **153 passed**; regresión: **1.382 passed** en 45,99 s | Python 3.10.7. La suite completa reportó 536 avisos deprecados (Starlette/httpx, AnyIO y `pytest-asyncio`); sin fallos. Ruff no se ejecutó. |

El runner ejecuta `tests/test_investment_guidance.py`,
`tests/test_financial_education.py`, `tests/test_educational_route.py` y
`tests/test_monthly_balance.py`.

## Pruebas manuales pendientes

Los escenarios se deben ejecutar en el simulador aislado descrito en
[`educational-surplus-guidance.md`](../educational-surplus-guidance.md), usando
datos sintéticos. Registrar cada corrida en la tabla al finalizar. La evidencia
automatizada no completa esta verificación.

**Intento del 2026-10-15:** no iniciado. No hay ejecutable `docker` ni `podman`
disponible en el equipo, requisito del simulador. El archivo `.env` requerido
por Compose existe; no se leyeron sus valores, no se levantaron contenedores y
no se tocó el volumen/base de prueba. Se descargó el MSI oficial WSL 3.0.3.0
x64 y se verificó su SHA-256; Windows canceló la elevación al iniciar el
instalador, por lo que WSL y sus componentes no se actualizaron. Para continuar
hace falta completar la instalación de WSL y luego instalar/iniciar Podman.

| Criterio/escenario manual | Estado | Fecha, entorno y resultado |
| --- | --- | --- |
| CA-1: excedente positivo del mes cerrado | **Pendiente** | — |
| CA-2: ampliar plazo fijo, FCI o comparación | **Pendiente** | — |
| CA-3: balance cero y balance negativo | **Pendiente** | — |
| CA-4: movimientos en ARS y USD | **Pendiente** | — |
| CA-5: sin movimientos en el período | **Pendiente** | — |
| CA-6: confirmar que no se registra ninguna operación | **Pendiente** | — |

## Historial de resultados

| Fecha | Actualización |
| --- | --- |
| 2026-10-15 | Corrida puntual end-to-end sin contenedores: `test_acceptance_positive_surplus_is_read_only_and_isolated`, aprobada (1/1). |
| 2026-10-15 | Se registró la primera corrida focalizada de los seis criterios: 153 aprobadas. La verificación manual permanece pendiente. |
| 2026-10-15 | Regresión completa del backend aprobada: 1.382 pruebas. Sin fallos; se registraron avisos de deprecación. |
| 2026-10-15 | Se intentó iniciar la validación manual. Quedó pendiente porque Docker y Podman no están disponibles; no se inició el simulador ni se modificó la base aislada. |
| 2026-10-15 | Se solicitó actualizar WSL para instalar Podman. El MSI oficial se descargó y su hash se verificó, pero Windows canceló la solicitud de elevación; no se instaló. |
