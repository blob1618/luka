# Bitácora de pruebas — STK-45 / STK-217

- **Historias:** [STK-45](https://philippem.atlassian.net/browse/STK-45) y [STK-217](https://philippem.atlassian.net/browse/STK-217)
- **Rama revisada:** `feature/STK-45-STK-217-respuestas-concisas`
- **Último commit de código validado:** `be76c0e` — `fix(STK-45/STK-217): alinear contrato de redacción y facts`
- **Head de la matriz manual:** `4f4fcd6` — `fix(STK-45/STK-217): reconocer consultas de fuente y actualización de cotizaciones`
- **Documentación:** este archivo se actualiza en un commit posterior de docs sobre el head `be76c0e`.
- **Última corrida registrada:** 2026-10-24

## Estado general

La validación automatizada pasó completa sobre el head final `be76c0e`, y la
matriz manual se ejecutó antes, sobre `4f4fcd6`, en el simulador aislado con un
LLM real. Después del head final se repitió un smoke test en el mismo simulador.
Las respuestas observadas son concisas y coinciden con lo pedido: patrimonio con
tres totales, detalle de cotizaciones solo de los tipos solicitados, y guard
educativo para las preguntas conceptuales.

Esta bitácora registra lo verificado en el simulador y en las suites locales.
**No** registra entrega real por WhatsApp, despliegue en Render ni validación
sobre datos de producción. Esos puntos quedan listados como pendientes.

## Entorno

| Elemento | Valor |
| --- | --- |
| Head de código validado | `be76c0e` (más el commit de documentación posterior que actualiza este archivo) |
| Head de la matriz manual | `4f4fcd6` |
| Rama base | `main` |
| Rama de trabajo | `feature/STK-45-STK-217-respuestas-concisas` |
| Stack de pruebas | Docker Compose de `testing/docker-compose.yml`: Streamlit en `:8501` y Redis |
| Base de datos | SQLite aislada del entorno de testing (`testing_luka.db`) |
| WhatsApp / Meta | **No** se usó la API real |
| Supabase productivo | **No** se usó |
| Proveedor LLM del simulador | `gemini` |
| Modelo del simulador | `gemini-3.1-flash-lite` |
| Prompt | `prompts/core_prompt.md` |

Nota de entorno: el modelo por defecto Gemini 3.6 Flash respondió `503` y
Mistral respondió `429` durante la sesión. Por eso las corridas manuales usaron
Gemini 3.1 Flash Lite. Es una observación del entorno del proveedor, no una
falla funcional de Luka.

Nota visual de Streamlit: la interfaz interpreta pares de `$` como LaTeX y oculta
los signos. El texto crudo exportado de la sesión es la fuente autoritativa. En
WhatsApp se enviaría el `reply_text` crudo, no el markdown de Streamlit.

## Preparación de datos por chat

Se crearon dos billeteras y dos ingresos desde el simulador:

- Billetera **Pesos (ARS)** e ingreso ARS 1.000.000.
- Billetera **Dólares (USD)** e ingreso USD 300.

Totales base resultantes:

| Concepto | Valor |
| --- | --- |
| Saldos ARS | ARS 1.000.000 |
| Saldos USD | USD 300,00 |
| Total Oficial | ARS 1.450.000 |
| Total MEP | ARS 1.467.220 |
| Total Blue | ARS 1.463.500 |

## Matriz manual

Las respuestas de abajo son, en todos los casos, el texto crudo exportado por el
simulador, sin parafrasear ni normalizar. La matriz se ejecutó sobre el head
`4f4fcd6`; el head final `be76c0e` se validó con el smoke test de la sección
siguiente.

### 1. «¿Cuánto tengo?»

```text
*Tu patrimonio actual*
• Saldos ARS: $1.000.000
• Saldos USD: u$s300,00
• Oficial: $1.450.000 ARS
• MEP: $1.467.220 ARS
• Blue: $1.463.500 ARS
```

Sin compra, venta, fuente, timestamps ni footer. **Aprobado.**

### 2. «¿Cuánto tengo en blue?»

```text
*Tu patrimonio actual*
• Saldos ARS: $1.000.000
• Saldos USD: u$s300,00
• Blue: $1.463.500 ARS
```

Sin Oficial, sin MEP, sin compra/venta. **Aprobado.** Los saldos ARS y USD siguen
apareciendo a propósito: la aprobación de este ítem es sobre el total de
cotización filtrado, no sobre la omisión de los saldos base.

### 3. «¿Cuánto tengo en oficial y MEP?»

```text
*Tu patrimonio actual*
• Saldos ARS: $1.000.000
• Saldos USD: u$s300,00
• Oficial: $1.450.000 ARS
• MEP: $1.467.220 ARS
```

Solo los totales Oficial y MEP, con el mismo encabezado. Sin Blue, sin
compra/venta. **Aprobado.** Igual que en el ítem 2, los saldos ARS y USD se
mantienen por diseño; lo aprobado es el filtro de totales de cotización.

### 4. «¿A cuánto está el dólar blue?»

```text
*Cotización del dólar blue*
• Compra Blue: $1.545
• Venta Blue: $1.565
• Actualizado Blue: 24/10 17:59
• Fuente: DolarAPI
• Consultado: 24/10 21:19
```

Sin saldos ni totales de patrimonio. **Aprobado.**

### 5. «Mostrame compra y venta del oficial y MEP»

```text
*Cotizaciones del dólar*
• Compra Oficial: $1.500
• Venta Oficial: $1.550
• Actualizado Oficial: 23/10 15:55
• Compra MEP: $1.557,4
• Venta MEP: $1.563,9
• Actualizado MEP: 24/10 17:59
• Fuente: DolarAPI
• Consultado: 24/10 21:19
```

Solo Oficial y MEP, con compra, venta y hora de actualización de cada tipo, más
fuente y hora de consulta. Se conservan los decimales del MEP: `$1.557,4` y
`$1.563,9`. Sin Blue y sin saldos. **Aprobado.**

### 6. «¿De dónde salen y cuándo se actualizaron las cotizaciones?»

```text
*Cotizaciones del dólar*
• Compra Oficial: $1.500 | Venta: $1.550 (23/10 15:55)
• Compra MEP: $1.557,4 | Venta: $1.563,9 (24/10 17:59)
• Compra Blue: $1.545 | Venta: $1.565 (24/10 17:59)
• Fuente: DolarAPI
• Consultado: 24/10 21:19
```

Hallazgo y corrección documentados: antes del arreglo de prompt en `4f4fcd6`
esta consulta se clasificaba como `out_of_scope`. Después del arreglo devolvió
`query_usd_quotes` con los tres tipos, compra/venta, actualización, fuente y
hora de consulta. **Aprobado tras el fix.**

### 7. «¿Qué es una cotización?»

```text
No tengo una definición verificada de ese concepto en mi glosario actual. Puedo ayudarte con presupuesto, gastos, ahorro, intereses, inflación, deuda o CFT.
```

Se mantuvo como `financial_education`: respuesta educativa, sin cotizaciones de
mercado. **Aprobado.**

## Smoke test del head final (`be76c0e`)

Después del arreglo final de contrato de redacción, el controlador repitió
«¿Cuánto tengo?» en el mismo simulador aislado. Texto crudo:

```text
• Saldos ARS: $1.000.000
• Saldos USD: u$s300,00
• Oficial: $1.450.000 ARS
• MEP: $1.467.220 ARS
• Blue: $1.463.500 ARS
```

Datos exactos por ítem y sin compra, venta, fuente, timestamps ni footer.
**Cumple.** Esta respuesta no incluyó encabezado; el encabezado es opcional
según el contrato, así que la ausencia no es una desviación. Es un smoke test de
una sola consulta: la matriz completa de siete consultas sigue siendo la de
`4f4fcd6`.

## Evidencia de debug

- Última captura de `query_usd_quotes`: `"intent":"query_usd_quotes"`,
  `service_invoked=currency`, `quote_types=[]`, y el JSON crudo del clasificador
  conservado en `raw_llm_response`.
- **Segunda llamada de redacción:** los encabezados y el agrupamiento del texto
  final (por ejemplo `*Cotizaciones del dólar*` con líneas `Compra … | Venta: …`
  en el ítem 6) no coinciden con el `reply_text` enlatado del clasificador
  (`raw_llm_response`), que conserva solo el JSON de intent. Esa diferencia de
  formato, sumada a la latencia de **19.067 ms**, es consistente con que se
  ejecutó la llamada de presentación (`render_financial_reply`). La UI de debug
  **no** expone la segunda llamada de forma directa: la evidencia es indirecta y
  se declara como tal.
- Latencia observada en la consulta completa de detalle de los tres tipos:
  **19.067 ms** con el modelo seleccionado. Es una medición puntual, no un SLA;
  el webhook es asíncrono.
- Las siete respuestas crudas transcriptas arriba son el texto crudo exportado
  de la sesión; no hay paráfrasis en la matriz.

## Registro de corridas automatizadas

### Antes del arreglo final de prompt (`83f2e68`)

| Comando | Resultado |
| --- | --- |
| `.venv/bin/python -m pytest -q -m unit` | **147 passed**, 1442 deselected, 9,35 s |
| `.venv/bin/python -m pytest -q tests/test_net_worth.py tests/test_currency.py tests/test_api_cotizaciones.py` | **45 passed**, 1,50 s |
| `.venv/bin/python -m pytest -q tests/test_llm_financial_reply.py tests/test_llm_net_worth.py tests/test_prompt_contract.py` | **37 passed**, 0,51 s |
| `.venv/bin/python -m pytest -q tests/test_dispatcher_net_worth.py tests/test_dispatcher_usd_quotes.py tests/test_intent_routing.py tests/test_financial_education.py` | **62 passed**, 3,29 s |
| `.venv/bin/python -m pytest -q tests/test_webhook.py tests/test_webhook_idempotency.py --durations=40` | **41 passed**, 1,63 s |
| `.venv/bin/ruff check .` | sin hallazgos, exit 0 |
| `.venv/bin/python -m pytest -v` | **1589 passed**, 129,28 s |
| `docker compose -f testing/docker-compose.yml exec streamlit python -m pytest -v testing/tests` | **152 passed**, 10,64 s |

### Head de la matriz manual (`4f4fcd6`)

Estas cifras corresponden a la re-ejecución del **controlador** sobre el head
`4f4fcd6`, después del arreglo de prompt; son la fuente autoritativa de la
validación de ese head. Los logs completos **no** se commitean: los números y las
advertencias se transcriben acá y en el reporte interno de la tarea.

| Comando | Resultado |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_prompt_contract.py tests/test_dispatcher_usd_quotes.py tests/test_intent_routing.py` | **42 passed**, 1,49 s |
| `.venv/bin/ruff check .` | sin hallazgos, exit 0 |
| `.venv/bin/python -m pytest -v` | **1589 passed**, 128,02 s |
| `docker compose -f testing/docker-compose.yml exec streamlit python -m pytest testing/tests` | **152 passed**, 10,16 s |

Stack de testing: `docker compose -f testing/docker-compose.yml build` construyó
la imagen `testing-streamlit` (exit 0) y `up -d` dejó `testing-streamlit-1` y
`testing-redis-1` en estado `Up`; el readiness probe a `http://localhost:8501/`
devolvió HTTP `200`.

Sin fallos ni errores. Los avisos son los preexistentes: `datetime.utcnow()`
deprecado en SQLAlchemy, deprecación de `httpx` en `starlette.testclient`, un
`PytestCollectionWarning` por `TestingConfig` y un
`RuntimeWarning: coroutine 'connect_tcp.<locals>.try_connect' was never awaited`
en `tests/test_webhook.py::test_handle_webhook_duplicate_message_is_suppressed_before_dispatch`.
Este último ya existía en las corridas previas; no es un efecto de estos cambios.

### Head final (`be76c0e`)

Re-ejecución del **controlador** sobre el head final `be76c0e`
(`fix(STK-45/STK-217): alinear contrato de redacción y facts`). Estas cifras son
la validación autoritativa del head final; los logs completos **no** se
commitean.

| Comando | Resultado |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_llm_financial_reply.py tests/test_dispatcher_net_worth.py tests/test_dispatcher_usd_quotes.py tests/test_prompt_contract.py` | **63 passed** |
| `.venv/bin/ruff check .` | sin hallazgos, exit 0 |
| `.venv/bin/python -m pytest -q` | **1595 passed**, 299 warnings, 123,87 s |
| `docker compose -f testing/docker-compose.yml exec -T streamlit python -m pytest -q testing/tests` | **152 passed**, 1 warning, 8,76 s |

El aumento de 1589 a 1595 es por las 6 pruebas nuevas de contrato
renderizador/facts que agregó el arreglo final. El smoke test de «¿Cuánto tengo?»
en el mismo simulador también corresponde a este head.

Cambios del arreglo final revisados en este head: tope de 12 líneas en el prompt
con agrupamiento permitido; pruebas reales de contrato dispatcher-facts ↔
renderizador; limitación de atribución semántica documentada; guard de llaves
sueltas; regex con límites de palabra.

## Límites no verificados

- **No** se probó entrega real por WhatsApp ni la API de Meta; el envío saliente
  no se ejercitó.
- **No** se validó el despliegue en Render.
- **No** se provocó una falla real del proveedor de cotizaciones (DolarAPI); el
  camino de error solo está cubierto por pruebas automatizadas.
- **No** se probó una cotización realmente desactualizada en el simulador.
- **No** se validó contra Supabase productivo ni con usuarios reales.
- La latencia de 19.067 ms es una observación puntual; no se midió carga ni
  concurrencia.

## Historial de resultados

| Fecha | Actualización |
| --- | --- |
| 2026-10-24 | Suites automatizadas aprobadas sobre `83f2e68`: unit 147, focalizadas 45/37/62/41, backend 1589, testing 152, ruff limpio. |
| 2026-10-24 | Se detectó que «¿De dónde salen y cuándo se actualizaron las cotizaciones?» caía en `out_of_scope`; se corrigió el prompt en `4f4fcd6`. |
| 2026-10-24 | Head de matriz manual `4f4fcd6` revalidado por el controlador tras el fix de prompt: 42 focalizadas (1,49 s), backend 1589 (128,02 s), testing 152 (10,16 s), ruff limpio. |
| 2026-10-24 | Matriz manual de siete consultas ejecutada en el simulador con Gemini 3.1 Flash Lite; respuestas concisas y sin datos sobrantes. |
| 2026-10-24 | Head final `be76c0e` (`fix(STK-45/STK-217): alinear contrato de redacción y facts`) validado por el controlador: 63 focalizadas, ruff exit 0, backend 1595 (123,87 s, 299 warnings), testing 152 (8,76 s, 1 warning). Las 6 pruebas nuevas explican el salto desde 1589. |
| 2026-10-24 | Smoke test posterior a `be76c0e` en el mismo simulador: «¿Cuánto tengo?» devolvió los cinco ítems exactos, sin compra, venta, fuente, timestamps ni footer; sin encabezado, que es opcional. Cumple. |
