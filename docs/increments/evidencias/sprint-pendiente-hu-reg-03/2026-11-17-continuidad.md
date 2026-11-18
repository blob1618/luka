# Verificación de continuidad — 2026-11-17

Base: `f2e25ecc58b13fdb64e9e1c6ed65701e47fa62c9`, rama `codex/stk-37-stk-38-category-continuity`, cambios locales. Python 3.11; SQLite y Redis de pruebas. Extracción y tools programadas; sin comprobantes personales ni servicios productivos.

- `tests/test_media_category_continuity.py`: 19 casos, incluyendo seis combinaciones de confirmación hija por texto/botón/editor con fecha pendiente/resuelta. La categoría se crea una vez; el movimiento espera su propia propuesta entregada y confirmación; replay guarda una sola fila.
- Categoría existente por nombre/número en PDF/foto luego de una búsqueda: no se invoca al proveedor. Pregunta sin entrega, callback antiguo, dueño distinto, padre cancelado, interrupción y expiración no aplican una respuesta a otro borrador.
- Consulta de estado conserva workspace; retomar el activo genera revisión nueva. Una foto sin fecha no obtiene por defecto la fecha de recepción.
- 27 pruebas focalizadas aprobadas (continuidad, transporte, contrato de prompt y regresiones corregidas).
- Unitarias: 194 aprobadas. Webhook/idempotencia: 22 aprobadas. Registros JSON adjuntos contienen comandos, fecha y resultado.
- Suite completa: 1.350 pruebas aprobadas tras el refuerzo de procedencia del adjunto; registro JSON en la carpeta STK-38. Pruebas del modo webhook del simulador: 23 aprobadas. Regresión final de ingesta y continuidad: 47 aprobadas.
- El requisito de categoría de PDF/foto depende también de la procedencia fijada por backend; el modelo no puede eliminarlo cambiando el flag del borrador.

## Evaluación externa acotada

SQLite temporal, Redis en memoria, usuario y factura ficticios. Mistral `mistral-small-latest` respondió HTTP 429 antes de devolver herramientas para «Jueguitos» y «¿Registraste el gasto?».

Gemini `gemini-2.5-flash` respondió con herramientas. En una corrida eligió `answer_category_question` para «Jueguitos», preparó la categoría hija y, tras su confirmación independiente, mostró la pregunta de fecha del mismo gasto. También eligió `operation_status` para consultar si se registró. La otra corrida mezcló respuesta de categoría y reanudación, por lo que quedó en pregunta pendiente. Solicitudes posteriores recibieron HTTP 429. No hubo movimiento financiero persistido. La interpretación del proveedor es variable y el recorrido completo con él y por WhatsApp sigue pendiente.

## Regresión preexistente

La prueba `test_budget_feedback_and_recurring_offer_keep_amounts_and_both_actions` también falla en una copia limpia de `origin/main` (`f2e25ec`): su candidato del reloj fijo de octubre ya venció frente al reloj real de noviembre. Se fijó el reloj de elegibilidad al del fixture, manteniendo la validación real. No cambia el comportamiento productivo.

## Límites

No se enviaron mensajes de WhatsApp, no se desplegó y no se consultó Supabase. Aceptación humana, cierre Jira y despliegue: Pendiente. CI del SHA se enlaza desde el PR; un resultado local con cambios no acredita un SHA desplegado.
