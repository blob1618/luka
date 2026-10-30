# Etapa 5: entrada única y retirada del clasificador

La entrada textual e interactiva usa el mismo agente para todas las cuentas que
superan onboarding. Se mantienen las 42 herramientas de la
[matriz de etapa 4](assistant-stage4-coverage.md), sus servicios y bindings.
No hay flags por teléfono, fallback de clasificación ni intérprete NL alternativo.

El [mapa completo de pruebas](assistant-stage5-test-map.csv) identifica cada uno de
los 1601 símbolos originales por archivo, clase/función y línea del snapshot
preetapa 5. Permite distinguir capacidad preservada de contrato privado retirado;
una fila de inventario no equivale a un caso parametrizado de pytest.

## Qué cambió y dónde se demuestra

| Frontera | Implementación vigente | Evidencia concreta |
| --- | --- | --- |
| Ingreso global | Dispatcher con dos APIs, onboarding y actividad24h; DispatchResult en módulo propio | `test_assistant_stage5::test_every_known_phone_enters_tools_without_pilot_flags`, `test_unknown_onboarding_precedes_tools_and_honors_cooldown`; pruebas HTTP/onboarding conservadas |
| Interpretación | Runtime+registry, schemas/políticas de42tools, prompt breve configurable | `test_assistant_stage4::test_catalog_covers_current_domains_and_has_no_model_commit_tool`; proveedores nativos y prompt contract |
| Escritura/replay | Preview entregada, revisión/hash/owner, receipt financiero atómico; historial postenvío | `test_assistant_stage5::test_preview_delivery_confirm_replay_and_unique_visible_history`; Stage3 y aceptación PostgreSQL |
| Política económica | Efecto explícito cash_flow; principal/placement/existing_investment rechazados; indeterminación pregunta | `test_fixed_deposit_registration`, `test_assistant_stage5::test_semantic_effect_is_explicit_and_rejected_principal_never_mutates`, `test_mixed_batch_policy_rejects_whole_batch_without_partial_write` |
| Corrección de importe/lote | SQL real, presupuesto recalculado; IDs distintos y edición de una fila | `test_adaptive_conversation::test_amount_correction_recomputes_budget_without_duplicate_expense`, `test_multi_movement_message_retains_distinct_ids_for_ordinal_correction` |
| Selección/anulación | Subconjunto de refs mostradas, dueño y snapshot; prevalidación total y rollback | `test_numeric_movement_choice_applies_only_to_shown_item`; dos pruebas públicas FinanceService de anulación stale/ajena conservadas; `test_assistant_stage5_domains::test_multiple_annulment_prevalidates_every_snapshot_without_partial_write`, `test_batch_annulment_stale_after_preview_rolls_back_all_rows` |
| Consulta/CTA | Período y total backend condicionan link; cinco filas no equivalen al total | `test_dispatcher_query`; `test_webhook_query_movements::test_webhook_query_movements_offers_dashboard_link_only_for_period_and_more_rows`; filtro por tipo en `test_finance_query::TestFinanceQueryFilters::test_filter_by_movement_type` |
| Categorías/billeteras | Campos/ref separados; revalidación de moneda/owner; hija conserva datos del padre | `test_stk_245_categories`, `test_dispatcher_wallets`, `test_wallet_simulator_acceptance`; nuevos casos `test_category_choice_then_wallet_choice_remain_distinct_within_one_draft` y `test_category_child_preserves_parent_wallet_choice_without_inheriting_selection` |
| Límites/presupuesto | Ref identifica categoría/período; selección exacta, feedback y propuesta de compensación aparte | `test_assistant_stage4::test_limits_list_edit_selected_and_delete_with_visible_refs`, `test_assistant_stage5_domains::test_limit_multi_selection_deletes_only_selected_owned_months`; `test_dispatcher_budget`, `test_dispatcher_compensation`; `test_excess_suggests_new_compensation_without_applying_it` |
| Recordatorios/preferencia | Collecting conserva campos; lectura suspende; retoma completa mismo turno; gestión incluye pausados | `test_reminder` CRUD/SQL conservados, `test_assistant_stage5::test_interruption_preserves_reminder_and_same_message_resume_can_complete`, variantes de `test_reminder_update_changes_only_explicit_field`, `test_proactive_preference_needs_its_own_confirmation` |
| Recurrentes y feedback combinado | Presentación humana; verbo accept/reject validado; cooldown acredita objeto de propuesta exacto | `test_recurring_confirmation`, `test_recurring_integration`, `test_recurring_unknown_callback_verb_cannot_reject_or_accept`, `test_budget_feedback_and_recurring_offer_keep_amounts_and_both_actions`; cuatro variantes textual/interactivo de `test_recurring_delivery_marks_only_exact_bound_followup_once` |
| Fechas | Día backend/evidencia; no normalización inválida/futura→hoy | `test_fecha::test_single_expense_direct_path_passes_ayer`, `test_invalid_date_collects_and_never_silently_becomes_today`; Stage3 evidencia entre días y Stage4 lote |
| Gráficos | Query/prepare/render de dominio, PNG reales, filtros/distribución en operación readonly | Pruebas puras `test_advanced_charts`, `test_movement_chart`, `test_chart_layout`; Stage4 chart collecting/retoma/torta y `test_assistant_stage5_domains::test_chart_modes_emit_real_png_and_no_financial_confirmation` |
| Patrimonio/cotizaciones/educación | Facts protegidos, precisión/staleness; fuentes/catálogo y balance por moneda | `test_dispatcher_net_worth`, `test_dispatcher_usd_quotes`, `test_llm_financial_reply`; servicios puros conservados y pruebas Stage4/5 de capacidades readonly |
| Editor y botones/listas | Tokens por entrega/binding; callback tardío o botón viejo no autoriza otra propuesta | Stage4 navegación/año/hija/start_limit/reject_category; `test_published_confirm_button_binding_is_specific_to_proposal_at_ingress`; suites de modelo/servicio/admin/editor conservadas |
| Reset | Borra tareas/navigation e historial tras envío, conserva registros/receipts | `test_assistant_stage5::test_reset_clears_history_navigation_and_workspace_after_delivery_only` |
| Simulador/debug | Mismos wrappers/dispatcher, IDs únicos, callback real bajo entrega sintética; prompt efectivo, imágenes/links/opciones/trazas | `testing/tests/test_webhook_mode`, `test_components_ui`, `test_chat_state`, `test_debug_panel`; escenario de billeteras por WebhookModeService |
| Telemetría | Fases LLM/DB/Redis/events/reply en entrada común | `test_webhook_async`, `test_sql_telemetry`, `test_telemetry`; métricas del ensayo integrado |

Los nombres abreviados de la tabla remiten al archivo `tests/test_*.py` o
`testing/tests/test_*.py` indicado. El CSV contiene rutas cualificadas completas.
La fila de consulta separa deliberadamente filtro de tipo SQL y scopes por entrada:
no atribuye a un test de all/month/period una aserción que no realiza.

## Retiradas y correspondencia

Se retiraron `intent_routing.py`, `llm_contract.py`, `reference_resolution.py`,
`educational_route.py` y los pendings/flags financieros de ConversationService.
`llm.py` conserva renderer/configuración/provider, sin `process_message` ni
stubs de audio/imagen. Gráficos reutilizan servicios puros sin chart_patch/ask ni
last_chart paralelo. Presenters y efectos tienen módulos dueños, con transporte
común; no hay alias dummy para conservar imports antiguos.

El mapa cuenta856 cuerpos AST conservados, 172 adaptados, 259 sustituidos,
301 contratos retirados y 13 semánticas obsoletas. Suman 1601 filas originales;
el inventario de pruebas incorpora 67 símbolos nuevos. Cuatro filas conservadas son
funciones locales de apoyo del inventario, no cuatro casos pytest independientes.
Todos los destinos se resolvieron contra el AST actual. La auditoría independiente
del coordinador coincide en172 adaptadas, 573 retiradas/sustituidas y 67 nuevas.

- **Conservada/adaptada**: mismo símbolo cualificado, con cuerpo conservado o
  argumentos/fixtures/aserciones adaptados. Los fixtures compartidos pueden usar
  ahora la entrada única aunque el cuerpo no cambie.
- **Sustituida**: efecto funcional ejercitado por el destino explícito, a veces
  dividido entre ingreso/delivery y servicio SQL. No promete igualdad del diálogo viejo.
- **Contrato retirado**: JSON de intent, normalizador, parser por patrones,
  formatter privado, serialización/cache de DTO v1 o stub sin capacidad real.
  Destinos relacionados muestran el dominio vigente, sin fingir equivalencia del
  assert privado ni una garantía de comprensión de cada frase NL.
- **Semántica obsoleta**: fecha inválida/futura convertida silenciosamente en hoy,
  precedencia de flags o destrucción de pending al interrumpir. La nueva semántica
  se identifica por separado: aclarar/revisar, suspender/conservar y refs explícitas.

Los helpers de test construyen planes deterministas desde datos declarados, con
pasos preview→delivery→confirm. No interpretan lenguaje ni emulan el clasificador.
Las pruebas SQL, ownership, cancelación, snapshots, callbacks y efectos posteriores
siguen en sus módulos o tienen destinos concretos; no se eliminaron por importar
una interfaz antigua.

## Verificación y hallazgos corregidos

El coordinador ejecutó PostgreSQL real en un cluster Stage5 nuevo y descartable:
**6 grupos PASS**, con cash_flow explícito en los comandos de aceptación. Cubren
concurrencia/replay de registro/corrección/lote/update/anulación, rollback total,
migraciones con RLS/revocaciones, CHECK finito y preservación de recibos.
Arranque, runner y parada terminaron0; archivos sintéticos conservados. Ninguna
base existente/remota se usó. Log local `.codex/artifacts/pg-receipts.log`.

El ensayo real DeepSeek `deepseek-flash`, sin thinking, pasó **20/20 pasos** por
dispatcher y ambos wrappers reales. Incluyó onboarding/actividad con cuenta
vinculada sembrada, ausencia de flags por teléfono, tools/outcomes, snapshots
update→annul, interés cobrado admitido frente a principal rechazado, recordatorio
incompleto→consulta→retoma/completar→confirmar→pausar y editor A/B con callback
tardío/botón antiguo. SQLite memoria y Redis falso; HistoryService real sobre ese Redis, con entrega
local sintética explícita. Artefactos `.codex/artifacts/runtime-stage5-final.json` y
`runtime-acceptance.json`. Un escenario aprobado no mide tasa de fiabilidad ni
acredita transporte WhatsApp, Redis real o producción.

Después se cerraron bugs focales de paridad: feedback de presupuesto podía perderse
al adjuntar propuesta recurrente; el cooldown podía acreditarse por otro followup;
verbo desconocido de callback podía tratarse como rechazo; selección de billetera
ARS persistía después de confirmar y bloqueaba una propuesta USD nueva. Las
regresiones comprueban importes/ambas acciones, objeto exacto en ambos wrappers,
no mutación del verbo inválido y selección entre operaciones/campos/padre-hija.
No se repitieron APIs ni PG por estos cambios acotados de flujo/presentación.

Focal final tras puertos/limpieza:110 passed. Cinco casos de selección/padre-hija/
simulador también pasaron; son verificaciones focales, no una nueva suite completa.
Primer verify integral del coordinador: 1309 passed, 6 skipped y 18 errores de setup
de `TestConceptValidation`, parser privado que aún importaba helpers retirados.
Se retiró esa clase con 18 filas del mapa; los tests SQL restantes permanecen.
El alias Config eliminó un aviso de colección de TestingConfig. Focal recordatorio+
UI:84 passed. Primer log conservado en `stage5-verify-attempt1.log/json`.

**Verify integral final aprobado por el coordinador: 1309 passed, 6 skipped,
2 warnings de deprecación de dependencias, 111.83 s; lint y tests exit0.**
Los seis omitidos requieren Redis real, fuera del runner offline. Diff-check
aprobado. Después sólo documentación y una limpieza EOF autorizada que conserva
AST; no se repitió la suite ni hubo cambios semánticos de producto/tests.
Tras las correcciones de la revisión independiente el recuento vigente es 1316;
ver la sección siguiente.

## Revisión independiente y correcciones (30/10/2026)

Una revisión estática independiente del paquete de las etapas 1–5 encontró cuatro
defectos y un mensaje engañoso. Se contrastó cada hallazgo contra este árbol: los cinco
se corrigieron, se ajustó la severidad de uno (los formatos son visibles pero no alteran
datos) y se descartaron dos observaciones por no reproducirse —`context.preview` no
sobrevive al turno, así que no duplica facts, y `append_exchange` no recibe `title`—.
Se difirieron dos bordes de producto (fecha de referencia de la compensación y abandono
de flujos al recibir texto) y cuatro mejoras de harness por no bloquear la entrega:
filtrado dinámico de herramientas, cortacircuitos del loop, recibo de trazas en
telemetría y reintento del lock de ráfagas.

- **Selección multi-paso.** `open()` limpiaba `selected_ref` antes de preparar y
  `amend()` no lo consumía nunca: una selección vieja bloqueaba un cambio explícito
  posterior con un mensaje de datos desactualizados, y la lista de opciones anterior se
  reimprimía bajo otra pregunta. Ahora las opciones se limpian antes de preparar y la
  selección se consume después; el guard solo aplica al turno en que se elige.
- **Edición con moneda repetida.** `prepare_update` incluía `currency` aunque fuera la
  misma del movimiento y resolvía la billetera recién al confirmar, donde podía fallar
  por ambigüedad. Ahora la moneda redundante se descarta y la billetera se resuelve al
  preparar: si hay varias opciones se piden antes de confirmar; si no hay ninguna en esa
  moneda, se dice explícitamente.
- **Errores de validación disfrazados de `stale`.** `OperationService.prepare()` convertía
  cualquier `ValueError` en «los datos necesitan una nueva revisión». Se agregó
  `StaleReference` para lo que realmente venció o cambió —referencias, operaciones y
  snapshots— y el resto conserva su mensaje; una fecha imposible en edición ya no culpa
  al registro.
- **Formatos visibles.** Se eliminaron importes crudos (`1200.0000000000`), claves en
  inglés (`amount:`, `currency:`) y estados internos (`create:`, `created`, `pause`) en
  movimientos, recordatorios, categorías, billeteras y límites.
- **«modificar» sin propuesta.** Respondía que había suspendido un borrador inexistente;
  ahora indica que no hay una propuesta activa.

Evidencia: 7 pruebas de regresión nuevas y 2 existentes ajustadas porque fijaban la fuga
de estado como comportamiento esperado. Las nuevas: selección por número y cambio
explícito (`test_explicit_wallet_change_after_number_selection_is_not_stale`), moneda
repetida con dos billeteras, cambio a USD ambiguo, fecha inválida en edición, formato de
anulación, etiquetas de recordatorio y «modificar» sin propuesta. **Suite completa:
1316 passed, 6 skipped, 2 warnings, 148.74 s**; Ruff y `git diff --check` sin errores. Aceptación con
`deepseek-flash` real por el dispatcher unificado y entrega sintética: **8/8 casos**
(registro, selección por número y cambio explícito de billetera, edición con moneda
repetida, cambio a USD ambiguo, fecha inválida, anulación formateada, recordatorio en
español y «modificar» sin propuesta), con 30/30 comprobaciones. También se corrigió un
test de límites dependiente del calendario que usaba períodos fijos 10–11/2026 y fallaba
desde el 29/10 por el rechazo de períodos vencidos; no era un defecto de producto.

Límites: la aceptación con modelo real es una corrida acotada, no una tasa de fiabilidad
semántica; no se repitieron PostgreSQL, Meta ni despliegue, y no hubo commit, PR ni
cambio de Jira.

## Aceptación independiente con navegador (30/10/2026, segunda ronda)

Dos corridas independientes sobre el simulador —una por interfaz y otra instrumentando el
dispatcher— reportaron 6 fallas mayores y 2 menores. El contraste mostró que **cuatro de las
seis fallas mayores eran estado de prueba contaminado**: el reset del simulador no borraba
billeteras ni purgaba Redis, y el límite de tres operaciones abiertas bloqueaba cualquier
mutación posterior. Aislado el estado, el motor quedó en 11/16 casos aprobados y 2 fallas
reales.

Se corrigieron en esta ronda:

- **Reset del simulador**: ahora borra también billeteras y recibos, y purga el workspace,
  su claim y el historial visible en Redis. Una corrida nueva no arranca con «Wallet already
  exists» ni con borradores acumulados.
- **Confirmación y cancelación escritas**: se normalizan acentos y mayúsculas y se aceptan
  `sí`, `confirmo`, `confirmar`, `cancelar` y `cancelá`; `dale` y `ok` solo actúan si hay
  una propuesta entregada pendiente, para no interceptar una conversación normal.
- **Mensaje de ordenamiento**: ordenar por importe sin moneda ya no expone el texto técnico;
  pregunta «¿pesos o dólares?».

Se descartaron dos atribuciones: el alias implícito de «Efectivo» (el motor no inventa
alias; los nombres de billetera del ensayo no coincidían con el dato de prueba) y el
`asyncio.Lock` de Redis (el archivo citado no existe y el cliente ya se recrea al cambiar de
loop). Sobre el límite de operaciones abiertas se eligió **no descartar borradores**: la
apertura se rechaza con la lista de propuestas abiertas y la indicación de confirmar o
cancelar una; pedir cancelar sin propuesta activa también lista los borradores. Se conserva
así la garantía de etapa 3 (`test_workspace_budget_does_not_discard_drafts`) y el usuario
deja de quedar en un callejón sin salida.

Cierre: 6 pruebas nuevas (confirmación y cancelación escritas, mensaje de moneda, purga de
estado, rechazo accionable del límite y listado al cancelar sin propuesta) y una adaptada;
**1322 passed, 6 skipped, 122.81 s**; Ruff y diff-check limpios.
También se retiraron siete scripts sueltos que la auditoría externa había dejado en la raíz
del worktree y que rompían el lint.

## Prueba en producción (30/10/2026)

Con el PR #77 mergeado, el bot quedó desplegado y se probó desde WhatsApp real. Dos
registros fallaron con «La fecha no es válida» aunque el mensaje sí traía fecha
(«ayer gaste 6000 en uber») o no traía ninguna («gaste 2000 en uber»). El defecto se
reprodujo con un proveedor real: el modelo envía una expresión relativa como
`kind=literal` («hoy», «ayer»), y el contrato sólo aceptaba fechas de calendario en ese
tipo, así que la preparación se rechazaba y el usuario quedaba en un bucle de aclaración.

Correcciones:

- `DateSpec.resolve` resuelve desde el texto citado los días relativos inequívocos
  («hoy», «ayer», «anteayer», «antier», «pasado mañana») aunque lleguen como `literal`;
  el span sigue teniendo que estar en el mensaje. Una fecha imposible o un importe citado
  como fecha se siguen rechazando.
- La descripción del esquema y el prompt indican `relative_days` con `day_offset` para
  esos casos y `absent` cuando no hay fecha.
- La pregunta de aclaración ofrece formatos («hoy», «ayer», «30/10») y el backend registra
  `kind`, presencia de span y motivo del rechazo, sin guardar el texto del usuario.
- El log de fallo del proveedor ahora incluye el detalle de la excepción para diagnosticar
  el `TypeError` visto en producción.

Evidencia: 7 pruebas nuevas (4 relativas parametrizadas, importe/no-date y dos de
integración); **1329 passed, 6 skipped, 115.47 s**; Ruff y diff-check limpios. Con
`deepseek-flash` real, «ayer», sin fecha y «hoy» quedan en preview correcto.

Límite: la clave Gemini del `.env` local está revocada (`API_KEY_INVALID`), así que no se
pudo replicar el proveedor exacto de producción; la tolerancia cubre el caso observado y
el log de detalle permite confirmar el resto en el próximo intento.

## Riesgo y alcance de la evidencia

El costo es validación/serialización y contratos/facts explícitos; a cambio las
capacidades se extienden sin ramas de dominio en el loop. La elección semántica
sigue siendo del modelo: una omisión/selección incorrecta puede producir una
respuesta incompleta o pedir aclaración. No hay tasa medida de ese riesgo;
preview, refs, servicio y confirmación limitan sus efectos sobre datos.

Los recibos no convierten BackgroundTasks en cola durable ni prometen entrega
exactamente una vez. Categorías, recordatorios y otros dominios conservan sus
garantías concretas, sin inventar una transacción genérica entre capacidades.
No hubo aceptación Meta/Redis de producción, despliegue, nuevas dependencias,
schema Stage5, secretos, SQL remoto, commit/push/PR ni publicación.
