# Plan de mejora de la conversación financiera de Luka

**Estado:** backend implementado; migración aplicada en Supabase remoto y validada por el usuario; despliegue del backend y pruebas manuales pendientes
**Fecha:** 17 de septiembre de 2026
**Alcance:** backend de WhatsApp, interpretación de mensajes, estado conversacional, movimientos y límites.

## 1. Objetivo y casos de aceptación

Luka debe reconocer si un mensaje crea, consulta, modifica o elimina un objeto financiero; identificar el objeto al que el usuario se refiere; y ejecutar exactamente la operación solicitada. Una referencia inequívoca se resuelve sin pedir datos adicionales. Una referencia ambigua produce una pregunta breve con las opciones reales. Ningún texto generado por el LLM confirma una escritura antes de que el servicio de dominio la complete.

| Conversación | Resultado esperado |
| --- | --- |
| «Compré una pizza por 10000» → «Era por 13000 en realidad» | Actualizar el importe del mismo movimiento a 13000. Conservar descripción y categoría. No insertar otro movimiento. Recalcular el presupuesto desde la base. |
| «Borrá el límite de comida» → Luka muestra septiembre y octubre → «Ambos» | Eliminar exactamente los dos límites mostrados en una sola transacción y confirmar ambos períodos. |
| Luka lista un límite de transporte de septiembre → «Cambialo para octubre» | Editar ese límite, conservando categoría, importe y moneda. Si existen varios límites de transporte, pedir el período de origen. |
| «Compré verduras a 5000» → «Fue un error, borrá ese movimiento» | Anular el movimiento recién registrado. Excluirlo de consultas y presupuestos. |
| Luka lista movimientos → «Borrá el de verduras» | Resolver el registro mostrado por su ID interno. Si hay varias coincidencias, pedir una selección. |

## 2. Diagnóstico en el código actual

- `prompt.md` contiene la regla «si el usuario menciona un monto, es un nuevo movimiento» y restringe `change_limit` al límite recién creado. Estas reglas producen clasificaciones equivocadas en correcciones y ediciones de límites existentes.
- `app/services/dispatcher.py` envía al LLM fecha y categorías; solo agrega el último límite creado en algunos mensajes. `change_limit` exige `ConversationService.get_last_limit()`, aunque el usuario haya identificado un límite existente.
- `app/services/conversation.py` conserva el último movimiento y el último límite en Redis, pero los usa para flujos concretos. El estado de eliminación de límites ya contiene IDs candidatos, aunque la respuesta se interpreta como un único mes.
- `app/services/finance.py` permite cambiar la categoría de un movimiento y consultarlo, pero carece de una operación para editar otros campos o anularlo. `MovementItem` ya incluye un ID interno; el dispatcher no conserva la selección mostrada.
- `app/services/limit.py` edita mediante el contexto del último límite creado y elimina un límite por llamada. `LimitEntry` no expone un ID interno para guardar la lista mostrada como contexto.
- Los flujos configurables de `docs/conversation-flows.md` personalizan la presentación de resultados seguros. La resolución de referencias y las escrituras seguirán siendo responsabilidad del dispatcher y de los servicios de dominio.

## 3. Contrato y reglas de conversación

### 3.1 Interpretación estructurada

Mantener `intent="expense"` para crear ingresos y egresos. Añadir `update_movement` y `delete_movement`; conservar `change_limit` y `delete_limit`, ampliando sus datos de selección. El contrato normalizado en `app/services/llm.py` distinguirá:

- **Operación:** crear, consultar, editar o eliminar.
- **Entidad:** movimiento o límite.
- **Referencia:** último registro, elemento recién mostrado o filtros explícitos (descripción/categoría, período de origen, moneda, fecha).
- **Selección:** un elemento, un subconjunto o todos los candidatos de la pregunta vigente.
- **Cambios:** solo los campos que el usuario pidió modificar. El período de origen de un límite y su nuevo período serán campos distintos.

El LLM interpreta expresiones coloquiales («era por», «me equivoqué», «ese», «ambos», «los dos»); el backend decide a qué IDs se refieren. El prompt elimina las reglas contradictorias y agrega ejemplos positivos y negativos de los cuatro casos. El LLM no inventa descripción, categoría, importe ni identidad del registro para completar una edición. `reply_text` no decide el resultado de ninguna mutación.

Ejemplos del contrato semántico (los IDs se resuelven después, en el backend):

```json
{"intent":"update_movement","reference":"last_registered","changes":{"amount":13000}}
{"intent":"change_limit","reference":{"category":"transporte","source_month":9},"changes":{"target_month":10}}
{"intent":"delete_limit","selection":"all_pending_candidates"}
```

### 3.2 Resolución de referencias

Crear un servicio acotado de resolución de objetivos en `app/services/` y usarlo antes de llamar a `FinanceService` o `LimitService`:

1. Un ID del contexto de la última operación o de la lista recién mostrada es una referencia candidata, nunca autorización suficiente por sí sola.
2. Una categoría, descripción o período expresos permiten buscar en la base aunque Redis haya expirado.
3. Cero coincidencias: informar que no se encontró el objeto. Una: ejecutar. Varias: guardar los IDs candidatos y preguntar cuál o cuáles.
4. «Ambos», «todos» o «los dos» se aplican únicamente a los candidatos de la selección pendiente. Sin esa lista, Luka pide precisión.
5. Antes de cada escritura, el servicio de dominio vuelve a consultar los IDs y exige `usuario_id` del teléfono vinculado. Un registro eliminado, anulado o alterado desde que se mostró invalida la selección; no se sustituye silenciosamente por otro.

Extender `ConversationState` para la selección pendiente y guardar en Redis un contexto breve de objetos mostrados, con IDs, tipo de entidad, datos necesarios para distinguirlos y vencimiento. Usar los IDs de `MovementItem` y agregar el ID a `LimitEntry`. El contexto es una ayuda de conversación; la base sigue siendo la fuente de verdad. Si Redis no está disponible y la frase depende de «ese» o «ambos», pedir una referencia explícita y no escribir.

Una petición nueva y explícita puede interrumpir una pregunta pendiente; `cancelar` cierra solo esa operación. No enviar todo el historial al LLM: pasar el resumen mínimo de la operación o selección activa. Los recorridos visuales configurables no crean intents ni reglas financieras.

## 4. Entregas incrementales

### Entrega 1 — Corrección de movimientos

**Cambios:** `prompt.md`, `app/services/llm.py`, contrato de interpretación, `app/services/dispatcher.py`, `app/services/conversation.py` y `app/services/finance.py`.

- Reconocer `update_movement` y un parche de campos expresamente mencionados: importe, descripción, categoría, moneda, tipo y fecha. Migrar el cambio de categoría existente a esta operación, sin dejar dos rutas que compitan.
- Guardar la referencia a cada registro exitoso. Si un mensaje registra varios movimientos, conservar el conjunto mostrado para que «el segundo» sea resoluble; no escoger uno arbitrariamente como «el último».
- Resolver «el último», «el de pizza» y referencias a los movimientos listados. Preguntar solo si hay varias coincidencias o falta el valor nuevo.
- En `FinanceService`, actualizar una fila existente por ID y usuario en una transacción; validar importe positivo, categoría activa, moneda, tipo y fecha. Conservar todo campo ausente del parche.
- Tras el commit, responder con el cambio efectivo y recalcular el estado de presupuesto de los períodos afectados. Nunca usar una cifra calculada por el LLM.
- Probar registro → corrección con un solo movimiento, registro múltiple → referencia ordinal, categoría existente, usuario ajeno, importe inválido y cambio de período/categoría que afecta dos presupuestos.

**Criterio de salida:** el caso de la pizza modifica un único ID, conserva «pizza» y «comida», y el total del ejemplo pasa de 50000 a 53000, no a 63000.

### Entrega 2 — Anulación de movimientos

**Cambios:** contrato LLM, `FinanceService`, dispatcher, `app/models/database.py`, migración versionada en `supabase/migrations/` y todas las lecturas de movimientos.

- Incorporar `delete_movement` y resolver «ese» después de un registro o «el de verduras» después de una lista.
- Añadir `anulado_en` nullable al movimiento. La operación marca la fila como anulada por ID y usuario. Conservar la fila y su `whatsapp_message_id` evita que una entrega tardía del mensaje original vuelva a crear el gasto. Una segunda petición sobre la misma fila devuelve un resultado idempotente.
- Excluir filas anuladas de `FinanceService.query_movements`, totales por categoría, `BudgetService`, el dashboard y cualquier otra consulta financiera. Mantener la comprobación de duplicados contra filas anuladas.
- Confirmar la anulación solo tras el commit; mostrar el presupuesto recalculado si corresponde. Si hay varios movimientos de verduras, preguntar cuál antes de escribir.
- Probar anulación desde el último registro y desde `/movimientos`, ausencia de cambios ante ambigüedad, repetición de mensaje, aislamiento por usuario y recálculo de totales.

**Criterio de salida:** el movimiento de verduras deja de aparecer y de contar en el presupuesto; una entrega repetida no lo restaura ni modifica otra fila.

### Entrega 3 — Edición de límites existentes

**Cambios:** `prompt.md`, `LimitService`, dispatcher y contexto de listas.

- Resolver un límite por categoría y período de origen desde la base, aunque no exista `last_limit` en Redis. Usar `last_limit` solo para pronombres inequívocos después de una creación.
- Aplicar un parche al límite identificado por ID: categoría, importe, moneda o período nuevo. Mantener lo demás. Rechazar una colisión con otro límite de la misma categoría, período y moneda.
- Si «modificá el límite de transporte» no indica qué cambiar, preguntar qué campo desea modificar; si hay varios límites de transporte, pedir primero el período de origen.
- Probar edición sin `last_limit`, referencia a una fila listada, varios períodos de origen, colisión con un límite existente y conservación de importe/moneda.

**Criterio de salida:** el límite de transporte de septiembre pasa a octubre con el mismo ID e importe, incluso si Redis no conserva un límite recién creado.

### Entrega 4 — Selección múltiple y eliminación de límites

**Cambios:** estado `PendingLimitDelete`, intérprete de selección, `LimitService` y respuesta de WhatsApp.

- Interpretar un mes, varios meses, «ambos», «todos los mostrados» y «cancelar» sobre la lista pendiente.
- Agregar en `LimitService` una eliminación por IDs: verificar todos los registros y su propietario antes de eliminarlos; ejecutar el conjunto en una sola transacción. Si alguno ya no coincide, no eliminar ninguno y actualizar la pregunta.
- Confirmar por nombre y período cada límite eliminado. Limpiar el contexto y los caches de «último límite» que apunten a esos IDs.
- Probar «ambos», «los dos», «septiembre y octubre», «solo septiembre», cancelación, selección vencida, registro ajeno y fallo transaccional sin eliminación parcial.

**Criterio de salida:** «Ambos» después de ver septiembre y octubre borra exactamente esos dos límites; otros límites, como transporte, permanecen intactos.

### Entrega 5 — Integración y calidad conversacional

- Reunir las cuatro conversaciones reales y sus paráfrasis rioplatenses en una batería de evaluación. Cada entrega incorpora sus pruebas del dispatcher/webhook con SQLite y LLM simulado; esta etapa revisa el conjunto completo y mensajes que interrumpen un flujo pendiente.
- Completar las pruebas cruzadas de selección vencida, cambio concurrente, falta de Redis, fallos antes del commit y operaciones con campos parciales.
- Registrar en logs estructurados el intent normalizado, tipo de referencia, cantidad de candidatos, operación y estado final, sin texto financiero sensible. Medir tasa de aclaraciones, correcciones mal clasificadas, mutaciones erróneas y latencia.
- Registrar los nuevos eventos seguros (`movement.updated`, `movement.annulled`, `limit.bulk_deleted`) en el contrato de flujos configurables, con respuestas por defecto producidas por el backend. Comprobar que texto libre nuevo pueda abandonar la presentación interactiva pendiente.

**Criterio de salida:** los cuatro casos y sus paráfrasis pasan en pruebas de integración; no hay escrituras sobre filas de otro usuario, mutaciones sobre referencias ambiguas ni confirmaciones antes de commit; pasan `python -m ruff check .` y `python -m pytest -v`.

## 5. Orden, datos y despliegue

Implementar y verificar una entrega completa antes de iniciar la siguiente. La entrega 1 corrige la duplicación de gastos; la 2 permite anular errores; las entregas 3 y 4 resuelven límites. Cada entrega debe conservar operativos los registros, consultas, recordatorios y límites que ya funcionan.

La entrega 2 exige generar la migración con el flujo de Supabase CLI del repositorio, actualizar el modelo SQLAlchemy y probar tanto SQLite como PostgreSQL reconstruido desde migraciones. Desplegar la migración antes del código que usa `anulado_en`; verificar su aplicación remota según `docs/database.md`. Ninguna migración local demuestra por sí sola el estado de producción.

Las pruebas automáticas no necesitan WhatsApp ni LLM reales. Antes de activar cada entrega en producción, repetir sus conversaciones en `testing/` mediante Docker/Podman con un número de prueba y revisar el registro real, las respuestas y los totales. La validación manual no sustituye la suite automática.

## 6. Límites del alcance

Este plan no añade asesoramiento financiero, creación de nuevas reglas desde el panel de flujos, acceso directo del frontend a Supabase ni memoria ilimitada de conversación. La adaptabilidad se construye con referencias acotadas, selección explícita y operaciones de dominio validadas.

## 7. Seguimiento de implementación

- Implementados los cuatro casos de las capturas en el dispatcher y los servicios, con IDs seleccionados desde contexto acotado y comprobación de propietario antes de escribir.
- La anulación lógica de `supabase/migrations/20260917143651_annul_financial_movements.sql` está integrada en `main`; el usuario confirmó la columna `anulado_en` en Supabase remoto antes del despliegue del backend.
- Las consultas y presupuestos de este backend excluyen movimientos anulados. El dashboard de `luka_frontend` vive en otro repositorio y debe aplicar el mismo filtro en su propio backend antes de mostrar estos datos.
- La CI validó PostgreSQL reconstruido desde migraciones (`supabase db reset` y `supabase db lint`). Sigue pendiente una conversación manual por WhatsApp/testing con Redis y LLM reales. Este entorno no tiene Docker ni Podman instalados.
- La base SQLite persistente de `testing_luka.db` se creó con un esquema anterior. `Base.metadata.create_all()` no agrega columnas a tablas existentes: conservar una copia si se necesitan sus datos y recrearla antes de las pruebas manuales con el esquema nuevo.
