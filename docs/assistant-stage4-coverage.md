# Etapa 4 — cobertura del catálogo y paridad comprobada

Fecha: 28/10/2026. Rama local `feature/STK-260-tools-pilot`.
Gate: `APPROVED_PLAN_STK260_STAGE4_20261028`.

Esta matriz distingue implementación, pruebas aisladas y ensayo real. El catálogo
se compone en `assistant_catalog.py`: 42 herramientas, sin clasificador ni
normalizadores de intención dentro de sus handlers. Las rutas externas al piloto
continúan hasta el corte de la etapa 5.

## Capacidades registradas

Las pruebas citadas son de `tests/test_assistant_stage4.py`, salvo indicación.
Usan servicios reales con SQLite en memoria y dobles de proveedor/Redis/transporte.

| Capacidad | Adaptador y servicio reutilizado | Comprobación implementada |
|---|---|---|
| compare_periods | insights / period_insights | Agregados completos, monedas, categorías, corte inclusivo y scopes encadenados; aceptación Stage3 |
| find_movements | movements / consulta SQL propia | Selector, filtros antes de límite, importe/fecha/registro, ownership y conteo; piloto/Stage3 |
| wallet_balances | wallets / WalletService | Saldos por moneda y propietario; piloto/Stage3 |
| register_movement | movements / FinanceService + receipt | Preview, entrega, persistencia, replay, fecha y selección; piloto/Stage3 y categoría explícita faltante |
| correct_movement_date | movements / FinanceService.update_movement | Snapshot, sólo fecha, rechazo stale y publicación de referencia vigente; piloto/Stage3 |
| register_movement_batch | movement_management / FinanceService | Dos a cinco items en una transacción/receipt; rollback completo, IDs estables por item, replay y evidencia de fecha sin recalcular items intactos |
| update_movement | movement_management / FinanceService.update_movement | Patch tipado, snapshot, receipt, campos preservados, categoría hija anidada y update→annul sin reconsulta |
| annul_movements | movement_management / FinanceService.annul_movement/annul_movements | Selección propia exacta, uno/múltiples, transacción/receipt y replay; referencias anuladas retiradas de la proyección |
| list_categories | entities / FinanceService.get_categories_with_totals | Catálogo con refs; sin moneda sólo nombres, con moneda agregados separados; ARS/USD nunca se suman |
| create_category | entities / CategoryCreationService.confirm | Creación/reactivación con aprobación, vínculo padre, validación del padre antes del efecto y replay efímero del hijo |
| rename_category / delete_category | entities / FinanceService | Selección propia activa, nombre/snapshot, renombrado y eliminación lógica; refs vigentes o retiradas |
| create_wallet | entities / WalletService.create_wallet | Nombre, moneda, preview y creación propia confirmada |
| list_limits | budgets / consulta de límites propios | Refs/snapshots; moneda, categoría y día de referencia efectivos; selección entre categorías y períodos |
| create_limit / change_limit | budgets / LimitService.create_limit | Año/mes/categoría faltantes, aprobación, edición del ID seleccionado, snapshot y publicación actualizada |
| delete_limits | budgets / LimitService.delete_limits_by_ids | Conjunto exacto y snapshots, eliminación atómica existente; change→delete sin reconsulta |
| query_budget | budgets / BudgetService.query_statuses | Importe límite, consumido, disponible, excedido, porcentaje y estado calculados por backend |
| propose_compensation | budgets / BudgetCompensationService | Propuesta derivada y snapshot propio; aplicar sólo tras entrega y aprobación; replay del workspace |
| list_reminders | reminder_management / ReminderService | Activos o activos+pausados; refs visibles en el turno siguiente con snapshots vigentes |
| create_reminder | reminder_management / ReminderService | Título duplicado, día faltante, completar borrador y persistencia confirmada |
| update_reminder | reminder_management / ReminderService | Edición de día conserva título, importe y moneda no modificados |
| change_reminder_state | reminder_management / ReminderService | Pausar, activar y eliminar; pause→activate directo con referencia actualizada |
| set_proactive_prompts | reminder_management / ReminderService | Preferencia propia y efecto sólo después de aprobación |
| list_recurring_candidates | reminder_management / RecurringExpenseService | Candidatos propios vigentes, refs y detalle visible |
| accept_recurring_candidate / reject_recurring_candidate | reminder_management / RecurringExpenseService | Owner, vigencia, decisión explícita, creación idempotente y rechazo; callback con save fallido conserva éxito real |
| movement_chart | charts / ChartRequest.periods, query_chart, prepare_charts, MovementChartService | PNG real, recopilar filtros, interrumpir/retomar, mensual/comparación/categoría, barras/torta, elección de distribución y seguimiento por referencia |
| query_net_worth | financial_reads / calculate_net_worth + render_financial_reply | Saldos propios ARS/USD y redacción protegida por facts del backend |
| query_usd_quotes | financial_reads / get_usd_quotes + render_financial_reply | Tipos pedidos, precisión sin redondear, fuente, fechas y staleness; proveedor externo simulado |
| previous_month_balance | financial_reads / monthly_balance | Período cerrado y monedas separadas; diferencia registrada distinta de dinero disponible |
| financial_education | financial_reads / FinancialEducationService.answer | Término explícito del glosario y respuesta verificada; sin clasificar el mensaje global |
| investment_guidance | financial_reads / investment_guidance | Catálogo con IDs reales, tema, fuentes y límites; ID desconocido devuelve feedback uniforme |
| dashboard_link | financial_reads / DashboardLinkService | Vinculación y enlace backend en botón URL, sin URL elegible por el modelo |
| conversation_reply | conversation | Saludo, ayuda y fuera de alcance con salida útil de backend |
| reset_context | conversation + HistoryService | Reinicia tareas efímeras, conserva registros/receipts y propaga clear_memory para después de enviar |
| present_result / ask_clarification | conversation / Presenter | Facts obligatorios completos sin copiar IDs, aclaración y selección; piloto/Stage3 |
| operation_amend / operation_suspend / operation_resume / operation_cancel | conversation / OperationService | Único workspace, revisión/TTL, datos tipados, evidencia por campo, interrupción y retoma; piloto/Stage3/Stage4 |

## Editor, callbacks y transporte

Se conservan los nombres y variables de los eventos publicados. El evento se
presenta como suplemento de los facts/preview protegidos; imágenes, botones URL
y followups viajan por el contrato saliente existente.

Los bindings se guardan separados de las variables editables del template. Los
tokens de botones/listas de la ruta nueva incluyen el hash de ese binding; la
navegación next_node lo conserva. Un botón del mismo nodo/versión anterior no
puede operar sobre un borrador posterior. La ruta legacy sin binding conserva su
contrato hasta el corte.

| Acción | Destino implementado | Evidencia |
|---|---|---|
| start_limit | Abrir create_limit collecting | Flujo publicado→botón→mismo workspace collecting, sin preview financiera ni persistencia |
| request_category_change / movement:change_category:* | Abrir update_movement con changes.category_ref faltante | Botón de movimiento y creación de categoría hija anidada |
| confirm_category | Confirmar hijo y producir nueva preview del padre sin aplicarlo | Padre límite y padre update; replay del hijo no repite mutación |
| reject_category | Cancelar hijo y retomar padre por vínculo backend | Flujos publicados con padre collecting/ready: pregunta o preview nueva, sin crear categoría ni aplicar padre |
| confirm_limit_year / reject_limit | Amend del año / cancel de operación vinculada | Navegación conserva binding; año produce preview, sin insertar límite |
| confirm_compensation / reject_compensation | Confirm/cancel de snapshot vinculado | Handler común de binding y aceptación de compensación confirmada |
| cancel_pending_operation | Cancelación por vínculo exacto | Validación de ID/revisión; no inferir otra operación activa |
| rec_cand:accept/reject | Servicio de decisión propia vigente desde callback de usuario | Idempotencia y save fallido conservando resultado real |
| assistant:confirm/cancel | Resolución de versión/token bajo claim | Pruebas piloto/Stage3 y recorridos nuevos |
| next_node | ConversationFlowRuntime | Navegación con binding y regresión de flujos existente |

## Garantías y límites

Movimientos individuales y lotes conservan mutación y receipt en la misma
transacción. La única migración nueva amplía el CHECK finito de command_kind a
register, correct_date, register_batch, update y annul; no cambia RLS ni permisos.
La aceptación PostgreSQL descartable verifica concurrencia, rollback de lote,
replay, ambos nombres previos de constraint y preservación de recibos.

Los demás dominios reutilizan sus transacciones, snapshots e idempotencia
existentes. El workspace conserva el resultado de confirmación mientras retiene
la operación; esto no es una nueva garantía universal de receipt durable. La
retención sigue acotada y no se ofrece al modelo una referencia obsoleta tras
mutar: los snapshots anteriores permanecen en backend para rechazar propuestas
viejas, mientras la proyección sólo anuncia la referencia vigente de cada entidad.

Las operaciones de lectura incompletas usan el mismo workspace. Al completarlas
producen datos/imágenes sin botón de confirmación financiera; las operaciones de
mutación siempre requieren aprobación de una preview entregada. No se añadieron
dependencias, clasificador alternativo, servidor MCP ni OAuth real.

El corte global y la retirada de legacy siguen en la etapa 5. Esta matriz no
convierte pruebas offline en aceptación de WhatsApp, producción o comprensión
semántica general del modelo.

## Evidencia de cierre

- Revisión independiente del coordinador raíz cerrada, sin bloqueos materiales.
- Cierre focalizado: 130 pruebas aprobadas de piloto, Stage3, Stage4, proveedores
  y flujos; Ruff y diff-check aprobados. La regresión ampliada previa pasó 200
  casos antes de las correcciones finales de continuidad.
- PostgreSQL descartable real: seis grupos PASS para concurrencia de registro,
  rollback de receipt, corrección, lote/replay/rollback total/update/anulación,
  migración original con RLS y ampliación del CHECK preservando recibos/permisos.
  El cluster propio de etapa 4 quedó detenido y sus datos sintéticos conservados.
- DeepSeek real `deepseek-flash`, sin thinking: tercer ensayo aprobado, diez pasos
  de update/annul y recordatorio incompleto→consulta→completar→confirmar→pausar.
  El primer intento reveló refs obsoletas tras actualizar y un checker de título
  demasiado exacto; el segundo reveló que resume collecting cortaba el mismo
  mensaje antes de permitir completar sus datos. Se corrigió el protocolo y la
  identificación del harness, conservando snapshots, pedido y comprobaciones de
  dueño, ID, importe, día, moneda, estado y título exacto una vez creado.
- Hay un pase aprobado del escenario compuesto, además de los dos intentos
  anteriores conservados. No es una tasa estadística de fiabilidad ni entrega
  real de WhatsApp. Proveedor real; DB, Redis, historial y entregas sintéticos.

El gate completo fue ejecutado una vez por el coordinador tras el freeze final:
**1976 passed**, dos avisos de deprecación, 99,41 segundos y runner exit 0.
Son 48 casos nuevos de etapa 4 frente al cierre anterior de 1928. No hubo cambios
de producto o tests posteriores a ese gate; esta actualización es documental.
