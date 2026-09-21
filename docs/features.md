# Funcionalidades

Comportamiento vigente de Luka por área. El detalle técnico de cómo se implementa
cada flujo está en [architecture.md](architecture.md).

## Registro de movimientos por texto

Un mensaje con `intent="expense"` y `movement_type` válido se registra como movimiento
financiero: `ingreso` o `egreso`. El backend confirma únicamente después de persistir.

- Un mensaje puede contener varios movimientos. Se registra cada uno con su propia
  clave de deduplicación (`<message_id>:movement:<índice>`) y la respuesta resume
  cuántos se registraron. Si falta algún dato en un ítem, se avisa cuántos quedaron
  pendientes sin revertir los ya registrados.
- Validaciones antes de persistir: remitente con usuario vinculado, monto positivo,
  tipo válido y descripción resoluble (`description`, `expense` o el texto original).
- La moneda se normaliza a mayúsculas y usa `ARS` si el mensaje no indica otra.
- La fecha del movimiento se resuelve contra la fecha actual cuando el mensaje usa
  expresiones relativas («ayer», «el martes») y por defecto es hoy.
- La categoría es opcional. Se resuelve contra la taxonomía base y las categorías
  activas del usuario; sin coincidencia, el movimiento se guarda con `categoria_id`
  nulo. Nunca se crea una categoría sin confirmación explícita.
- La respuesta de confirmación se construye con el resultado real de la persistencia.
  El `reply_text` del LLM no confirma escrituras.

Estados posibles del registro:

| Estado | Efecto |
| --- | --- |
| `registered` | La fila se persistió; el backend confirma el movimiento. |
| `duplicate` | El `whatsapp_message_id` ya existía; no se inserta otra fila y la respuesta visible se suprime. |
| `user_not_found` | No hay usuario vinculado a ese WhatsApp; no se persiste nada. |
| `invalid_data` | Faltan datos válidos (monto, tipo o descripción); se pide reformular. |
| `persistence_error` | La escritura falló; no se confirma el registro. |
| `not_a_movement` | El mensaje no es un movimiento financiero registrable. |
| `needs_category_confirmation` | La categoría propuesta no está en la taxonomía ni en las categorías del usuario; se inicia el flujo de confirmación. |

La deduplicación es doble: el webhook reclama el `message_id` una sola vez en Redis y
`FinanceService` verifica `whatsapp_message_id` antes de insertar. Un reenvío de Meta se
procesa en silencio para no repetir una confirmación ya enviada.

## Edición y anulación de movimientos

- `update_movement` aplica solo los campos mencionados por el usuario: importe,
  descripción, categoría, moneda, tipo y fecha. El resto del movimiento se conserva.
- La operación exige usuario propietario, movimiento activo y que los datos coincidan
  con lo mostrado (`stale_context` si cambió). La categoría nueva debe ser una categoría
  activa del usuario.
- `delete_movement` es una anulación lógica: marca `anulado_en` y conserva la fila y su
  `whatsapp_message_id` para que una entrega tardía del mensaje original no vuelva a
  crear el gasto. Repetir la operación devuelve un resultado idempotente
  (`already_annulled`).
- Se pueden anular varios movimientos en una sola transacción por referencia ordinal
  («el segundo»), por conteo («los últimos dos») o por nombres coordinados
  («ventilador y tv»). Si algo no coincide, no se anula ninguno.
- Los movimientos anulados se excluyen de consultas, totales por categoría, presupuestos
  y cualquier lectura financiera. La verificación de duplicados sí los sigue teniendo en
  cuenta.
- Después de editar o anular, el backend recalcula y muestra el estado del presupuesto
  de los períodos afectados.

## Categorías

- Existe una taxonomía base de categorías canónicas con sinónimos: Servicios, Comida,
  Transporte, Ocio, Vivienda, Salud, Ingresos, Educación y Ropa. Por ejemplo, «super»,
  «almacén» o «despensa» resuelven a Comida.
- Si el usuario ya tiene una categoría que resuelve al mismo canónico, el movimiento se
  asocia a esa categoría real en lugar del nombre canónico.
- Cuando el LLM propone una categoría que no está en la taxonomía ni entre las
  categorías del usuario, el backend pregunta antes de crearla. La confirmación es
  multi-turno y, al aprobarse, la categoría se crea y el movimiento se registra.
- El listado de categorías muestra las activas con totales de ingresos y egresos
  (excluyendo anulados).
- Eliminar una categoría es lógico: desvincula sus movimientos (`categoria_id` nulo) y
  elimina los límites asociados. Si más adelante se vuelve a usar ese nombre, la
  categoría se reactiva.
- No existe renombrado de categorías. Para cambiar la categoría de un movimiento se usa
  `update_movement`.

## Límites de gasto y presupuestos

- Un límite es único por usuario, categoría, inicio de período y moneda. Crear sobre una
  combinación existente actualiza el monto (upsert); no se acumulan duplicados.
- El monto debe ser positivo, con dos decimales y un máximo de `9999999999.99`. La
  moneda por defecto es `ARS`.
- Si no se indica mes, se usa el actual. Si se indica un mes ya pasado sin año, el
  backend propone el año siguiente y pide confirmación.
- Si la categoría no existe, se pide confirmación explícita antes de crearla y aplicar
  el límite.
- `list_limits` muestra las configuraciones (categoría, monto y período). `budget_query`
  muestra el estado de consumo: una categoría o todos los presupuestos del período.
- El consumo se calcula desde los egresos persistidos con categoría, moneda coincidente,
  fecha dentro del período y no anulados. No se persisten saldos derivados.
- Al registrar un egreso categorizado o al cambiar su categoría, el backend evalúa el
  presupuesto y agrega una alerta con consumido y exceso si se superó el tope. No existe
  un job periódico de alertas.
- La edición y la eliminación de límites admiten selección múltiple (por mes, por
  nombres de meses o «ambos») y se ejecutan en una sola transacción. Si algún límite no
  coincide, no se elimina ninguno.

## Compensación de presupuesto

- La compensación mueve cupo entre categorías del mismo período y moneda: sube el límite
  de una categoría excedida y baja el de categorías con disponible. El total de los
  límites se mantiene y ningún movimiento se modifica.
- Se dispara por pedido explícito (`compensate_budget`) o de forma automática al registrar
  un egreso que deja una categoría excedida. En ambos casos el cálculo sale de los límites
  y egresos persistidos; la propuesta automática solo se muestra si se pudo guardar.
- La propuesta es multi-turno: muestra el antes y el después de cada categoría y espera una
  confirmación explícita (`confirm_compensation`) o un rechazo
  (`reject_compensation`/«cancelar»). Un mensaje distinto descarta la propuesta pendiente
  y sigue su curso normal.
- Antes de aplicar, el backend revalida límites y saldos contra el estado actual; si
  cambiaron, no escribe y pide un nuevo cálculo. La aplicación es idempotente por mensaje:
  reintentar el mismo `message_id` no aplica dos veces.
- La propuesta vence a los 30 minutos. Una categoría donante puede quedar en cero; el
  sistema elige donantes con disponible suficiente y nunca crea límites nuevos.

## Consulta de movimientos

- `query_movements` filtra por tipo (`ingreso`/`egreso`), categoría y rango de fechas,
  con orden determinista por fecha y antigüedad de carga.
- Muestra como máximo cinco movimientos y aclara cuántos hay en total cuando hay más.
  Los movimientos anulados no aparecen.
- Si hay más resultados que los mostrados y la consulta tiene filtro de fechas, se
  adjunta un enlace de acceso al dashboard conservando el período, cuando el usuario
  está vinculado.
- Los comandos `/movimientos` y `/egresos` ejecutan la consulta directamente. Si el
  pedido es ambiguo, Luka pide aclaración antes de responder.
- `/link` genera (o reutiliza) un enlace mágico de acceso al dashboard para usuarios ya
  vinculados, con vencimiento corto y un solo uso.

## Gráficos de movimientos en WhatsApp

- También permite comparar ingresos y gastos, filtrar categorías, seguir la evolución
  mes a mes (hasta 24 meses) y comparar exactamente dos meses. El mes actual se marca
  parcial en consultas mensuales. Una comparación en pastel ofrece opciones numeradas.
- Modificaciones como «que sea pastel» o «ahora solo ocio» conservan los demás filtros
  del último gráfico durante 30 minutos. Las series largas se dividen en imágenes de
  hasta seis barras con escala compartida. Ver [detalle y ejemplos](movement-charts.md).

- Un pedido explícito de gráfico o diagrama por categoría usa `movement_chart` y devuelve
  una imagen PNG en el mismo chat. Los pedidos genéricos de resumen siguen siendo texto y
  `/link` continúa reservado para abrir el dashboard.
- El formato predeterminado es barras; torta/pastel/circular selecciona el formato de
  pastel. El alcance predeterminado son egresos; los ingresos se grafican solo si el
  usuario los pide expresamente.
- Si no se indica período se usa el mes actual en `America/Argentina/Buenos_Aires`. Un
  rango parcial se aclara antes de consultar. Si el período contiene más de una moneda,
  el usuario debe elegir una y nunca se suman monedas diferentes.
- Se muestran como máximo seis elementos: cinco categorías relevantes y `Otros`. El
  usuario puede pedir las categorías de mayor o menor importe; `Otros` conserva la suma
  exacta de las categorías restantes.
- La imagen se genera completamente en memoria, sin archivos persistentes. Solo incluye
  movimientos propios, activos y dentro del rango solicitado. Si no hay datos se responde
  con texto y no se envía una imagen vacía.

## Educación financiera

- Luka explica en chat presupuesto, gastos fijos y variables, ahorro, interés simple y
  compuesto, inflación, deuda y costo financiero total (CFT), con una definición breve
  y un ejemplo ilustrativo.
- Las consultas educativas no crean ni cambian movimientos, categorías, límites,
  presupuestos ni recordatorios, incluso si el ejemplo incluye un importe.
- Ante «interés» sin más contexto pide aclaración entre interés simple y compuesto. Si
  se solicita un valor actual, una tasa vigente o un concepto no revisado, reconoce el
  límite en vez de inventar información. Las recomendaciones personalizadas de inversión
  están fuera de alcance.
- El contenido y el proceso de revisión están en
  [financial-glossary.md](financial-glossary.md).

## Recordatorios

- Un recordatorio de pago tiene concepto, día del mes (1-31) y, opcionalmente, monto y
  moneda (`ARS` por defecto).
- El título es único por usuario. Si ya existe uno con el mismo nombre, se inicia un
  flujo multi-turno para elegir otro. Si falta el día, Luka lo pregunta antes de crear.
- Se pueden listar (activos y pausados), actualizar (día y/o monto), pausar, reactivar y
  eliminar por título (coincidencia exacta y luego parcial, insensible a mayúsculas) o
  por identificador.
- El scheduler envía el aviso el día anterior al vencimiento, una sola vez por mes. Si
  la ventana de 24 horas de WhatsApp está cerrada, usa el template configurado.
- El recordatorio proactivo diario se activa y desactiva por chat. Solo se envía dentro
  de la hora configurada, con la ventana de 24h abierta, a usuarios habilitados y que no
  hayan registrado movimientos ese día, con un máximo de un aviso diario.

## Recordatorios inteligentes de gastos recurrentes

- **Detección automática en background**: Un worker nocturno analiza los movimientos de los últimos 4 meses calendario. Si detecta al menos 3 egresos en meses consecutivos con la misma descripción normalizada (y categoría/moneda) con variación máxima de $\pm 3$ días respecto al día mediano del mes, genera un `CandidatoGastoRecurrente` en estado `pendiente`.
- **Propuesta no invasiva por WhatsApp**: Cuando el usuario registra un nuevo egreso que coincide con un candidato pendiente y tiene `proactivo_habilitado=True`, Luka adjunta a la confirmación habitual del gasto una propuesta con botones interactivos nativos: «Sí, avisame» y «No, gracias». No se interrumpe el registro del egreso ni se requiere un comando especial.
- **Conversión atómica**: Si el usuario pulsa «Sí, avisame», el candidato pasa a `aceptado` y se crea un `Recordatorio` (`origen='recurrente_inteligente'`, `dias_anticipacion=3`). Si pulsa «No, gracias», el candidato pasa a `rechazado` y el sistema no vuelve a sugerirlo para ese concepto.
- **Despacho anticipado y supresión determinista**: El scheduler evalúa los recordatorios cada 5 minutos y despacha el aviso 3 días antes del vencimiento estimado. Si el usuario paga y registra el egreso antes del día de alerta dentro del período, el envío se suprime automáticamente (`AvisoRecordatorio` en estado `suprimido` con motivo `gasto_registrado`), evitando avisos redundantes.
- **Cumplimiento de ventana de 24h de WhatsApp**: Dentro de las 24h del último mensaje del usuario se envía texto libre interactivo; fuera de la ventana se envía mediante la plantilla aprobada en Meta (`WHATSAPP_REMINDER_TEMPLATE_NAME`).

## Onboarding y vinculación

- Un remitente con `whatsapp_id` ya vinculado continúa el flujo normal.
- Un remitente desconocido recibe una invitación con enlace de registro que incluye un
  token de un solo uso. El token nunca se persiste: la base guarda su hash.
- La invitación vence a los 30 minutos por defecto, tiene un enfriamiento entre
  reenvíos y un máximo de reenvíos; fuera de esos límites la respuesta se suprime. Una
  invitación vencida se marca y se genera una nueva.
- Las tablas `acuerdo_version` y `acuerdo_aceptado` forman parte del contrato de datos,
  pero todavía no hay contenido legal aprobado ni aceptaciones registradas por este
  flujo.

## Resolución de referencias conversacionales

- Después de registrar o listar elementos, Luka guarda un contexto acotado con los IDs
  mostrados. Las frases «ese», «el último», «el segundo», «ambos», «los dos», «todos»,
  «los últimos dos», meses nombrados o el nombre de un movimiento se resuelven solo
  contra ese contexto o contra búsquedas explícitas en la base.
- Ante varias coincidencias, Luka guarda los candidatos y pregunta cuál o cuáles. No
  inventa ni elige en silencio.
- Antes de escribir, el servicio de dominio vuelve a validar propietario y estado. Una
  referencia que cambió desde que se mostró se rechaza en lugar de sustituirse.
- Un pedido nuevo y explícito abandona la selección pendiente; «cancelar» la cierra sin
  cambios. Si Redis no está disponible, Luka pide una referencia explícita y no escribe.
- Luka conserva una memoria conversacional con los últimos 4 turnos completos de cada
  usuario, vigente por 24 horas desde la última actividad y aislada por usuario. El
  contenido se sanea antes de guardarse. Un pedido explícito de reinicio («olvidá lo
  anterior») borra la memoria y el estado pendiente.
- Los flujos de presentación administrables solo cambian cómo se muestra un resultado
  seguro: no crean intents ni reglas financieras. Un texto libre abandona el recorrido
  interactivo y vuelve al dispatcher general. Ver
  [conversation-flows.md](conversation-flows.md).

## Fuera de alcance

- Asesoramiento financiero, recomendaciones de inversión o temas ajenos a las finanzas
  personales.
- Acceso directo del frontend a Supabase: todo el acceso financiero pasa por el backend.
- Menú principal obligatorio: cualquier operación se puede pedir en lenguaje natural.
- Notas de voz e imágenes: `LLMService` todavía no las procesa.
- El dashboard vive en otro repositorio; este backend solo genera enlaces de acceso.

## Documentación relacionada

- [architecture.md](architecture.md): componentes, flujo end-to-end, contrato LLM y
  limitaciones.
- [database.md](database.md): contrato de datos y migraciones.
- [development.md](development.md): setup, tests, variables de entorno y deploy.
- [conversation-flows.md](conversation-flows.md): flujos de presentación administrables.
- [recurring-expenses-runbook.md](recurring-expenses-runbook.md): operación, observabilidad, mitigación y rollback de gastos recurrentes.
