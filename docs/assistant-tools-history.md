# Histórico hasta etapa4

Este documento conserva la evidencia anterior al corte global. Sus flags/rutas/rollback de piloto son históricos. Para el estado vigente ver [herramientas de Luka](assistant-tools-pilot.md).

# Revisión independiente y correcciones — 30/10/2026

Una revisión estática independiente del paquete 1–5 encontró cuatro defectos y un mensaje
engañoso: selección multi-paso que sobrevivía al turno y bloqueaba un cambio explícito,
edición con moneda repetida que podía fallar al confirmar por ambigüedad de billetera,
errores de validación reportados como datos desactualizados, formatos crudos de importes
y estados internos, y «modificar» sin propuesta que simulaba suspender un borrador.
Se corrigieron los cinco, se descartaron dos observaciones no reproducibles y se
difirieron cuatro mejoras de harness. Cierre: 7 regresiones nuevas, 2 pruebas ajustadas,
**1316 passed / 6 skipped**, Ruff y diff-check verdes, y aceptación real **8/8** con
`deepseek-flash` por el dispatcher. Detalle en el [cierre de etapa 5](assistant-stage5-coverage.md).

# Piloto de herramientas financieras

El piloto tiene una sola ruta de herramientas nativas para Gemini, Mistral y
DeepSeek. El modelo propone capacidades; el backend fija la cuenta, valida
argumentos, calcula agregados, prepara borradores y conserva las cifras de la
respuesta. No se envía al usuario la respuesta financiera libre del modelo.

## Activación y frontera de confianza

Está **deshabilitado por defecto**. `run_for_phone` es la entrada local de backend
para una identidad ya confiable; resolver una cuenta por teléfono no autentica
el origen de un webhook. No exponer esa función como endpoint público.

La integración textual e interactiva solo se habilita cuando el operador establece:

- `LUKA_TOOLS_PILOT_ENABLED=1`.
- `LUKA_TOOLS_PILOT_VERIFIED_INGRESS=1`, únicamente después de comprobar la
  autenticidad de los mensajes entrantes en la infraestructura de destino.
- `LUKA_TOOLS_PILOT_PHONES`: lista explícita de teléfonos propios de prueba.
- `LUKA_TOOLS_PILOT_MODELS`: modelos concretos previamente verificados para tools,
  compatibles con el proveedor configurado. Un modelo fuera de esa lista falla
  antes de efectuar una solicitud de herramientas; no se usa fallback legacy.

Estas variables no sustituyen autenticación. La configuración efectiva de
producción y su frontera de identidad no fueron inspeccionadas para esta entrega.
La migración del recibo debe aplicarse antes de habilitar el piloto; no se aplica
automáticamente al arrancar.

## Capacidades

La etapa 4 amplía este catálogo a **42 herramientas**. La
[matriz de cobertura de etapa 4](assistant-stage4-coverage.md) enumera contratos,
servicios, pruebas de dominio, acciones del editor y límites de cada garantía.
Incluye lotes de movimientos, edición/anulación, categorías y billeteras,
límites/presupuestos/compensación, recordatorios/preferencias/recurrentes,
gráficos multimedia, patrimonio/cotizaciones, contenido educativo/dashboard,
saludo/ayuda/fuera de alcance y reset del mismo contexto.

El catálogo vigente se compone explícitamente en `assistant_catalog.py`. Cada
capacidad registra su esquema, handler async y política de permisos, efecto,
timeout, interrupción y fin de turno. El loop no enumera nombres de dominio.
Agregar una capacidad requiere su módulo y la composición del registro; los
adaptadores de proveedor sólo serializan el contrato.

- `compare_periods`: agregados completos por moneda, períodos exactos y aportes
  de categorías por identidad estable. Por defecto compara el mes actual hasta
  hoy con el mismo corte del anterior, truncado por duración del mes.
- `find_movements`: detalle limitado con conteo total; admite tipo, moneda,
  categoría, período y orden por importe en la consulta. Una lista de cinco
  movimientos nunca se presenta como un total mensual. No se ordenan importes
  de monedas diferentes como si fueran comparables.
  Su selector discriminado es `scope`, `period`, `month`, `shown` o `all`.
  `DatePeriod.start/end` son días civiles inclusivos. `month` recibe `YYYY-MM` y
  backend calcula su último día. `sort_by` distingue fecha del movimiento,
  importe y fecha de registro; ningún selector reemplaza ese orden.
  Los scopes emitidos por comparación conservan período, moneda y tipo;
  los filtros nuevos sólo pueden restringirlos.
- `wallet_balances`: saldos propios, sin conversión de moneda.
- `register_movement` y `correct_movement_date`: preparan un borrador. El primero
  no crea categorías o billeteras nuevas; el segundo solo admite fecha y usa
  snapshot del movimiento mostrado.
- `ask_clarification` y `present_result`: preguntas y presentación a través de
  contratos; no son comandos financieros libres.
- `list_reminders`: consulta recordatorios activos propios mediante el servicio
  existente. Es el caso de extensión adicional del catálogo.
- `operation_amend`, `operation_suspend`, `operation_resume` y `operation_cancel`:
  controles por referencia opaca y revisión, sin herramienta de confirmación.

Toda escritura del piloto muestra fecha, importe, moneda y referencias y exige
confirmación. Workspace v2 conserva una operación activa y varias suspendidas;
una consulta o un nuevo borrador suspende la anterior. El presupuesto inicial
es tres operaciones abiertas y treinta minutos de vigencia, configurado en
OperationService. Al llegar al límite se pide completar o cancelar una; no se
descartan borradores. Suspender, modificar, retomar, cancelar o vencer incrementa
la revisión e invalida la confirmación. Retomar revalida dependencias y snapshot,
y muestra otra propuesta. La vigencia no se extiende al consultar o retomar.
Además se retienen como máximo tres operaciones terminales y sus resúmenes; la
purga conserva todos los borradores vivos y el replay sigue usando el recibo durable.

Cada campo aportado conserva texto, ID y día civil del turno que lo aportó. Una
fecha ya resuelta permanece al retomar; una fecha relativa nueva se interpreta
con el reloj del mensaje de modificación. Los campos por defecto no se atribuyen
al usuario. Los modelos y payloads preparados se reconstruyen y validan antes
de guardar: una copia frozen no constituye validación de una transición.
Las referencias comprueban cuenta, conversación, tipo, esquema y vencimiento;
el contexto del proveedor contiene handles y datos visibles sin UUIDs canónicos.
No hay memoria duradera de objetivos ni paginación general. Hay una operación
nueva por pedido; un lote de dos a cinco movimientos es una operación atómica
con una sola aprobación. Se conservan las rutas fuera del piloto hasta el corte de
paridad posterior; esta entrega no sustituye el editor ni sus servicios.

Las fechas literales se validan completas; una cita dentro de otro literal no es
una fecha independiente. Expresiones relativas/resueltas siguen siendo una
propuesta semántica del modelo y no garantizan comprensión universal. El borrador
permite revisarlas antes de persistir, sin añadir un intérprete de lenguaje por
listas crecientes de patrones.

## Confirmación, recibos y efectos posteriores

El comando confirmado bloquea la fila de su propietario en PostgreSQL y guarda la
mutación y `assistant_write_receipts` en la misma transacción. Una operación
identificada se repite desde su recibo; el ID de llamada del proveedor no autoriza
una escritura. Un fallo al guardar el recibo revierte también la mutación.

Preparar `ready` no habilita confirmar. El webhook llama al callback de entrega
sólo después del envío exitoso de la propuesta completa. El callback adquiere
su propio claim y valida referencia, revisión y hash; una entrega vieja no habilita
una revisión nueva. El botón contiene ID, revisión y token corto; el backend
resuelve el hash completo dentro del claim, sin recibirlo del modelo. “Sí”,
“si” y “confirmo” sólo valen con esa preview entregada y desde otro mensaje.
Una consulta, modificación o retoma exige una nueva entrega. Los recibos previos
siguen consultándose por sus IDs originales; un replay no vuelve a ejecutar efectos.

Los hechos obligatorios se incluyen desde backend aunque `present_result` no
copie sus referencias. El único historial visible es ConversationHistoryService,
escrito tras envío exitoso; el workspace no guarda una conversación paralela.

Los métodos existentes de FinanceService mantienen sus consumidores actuales.
Cuando reciben sesión del ejecutor, hacen flush sin commit ni cierre de esa
sesión. No llamar al camino de creación de categorías dentro de esta transacción.

El recibo no promete entrega exactamente una vez ni vuelve durable BackgroundTasks.
Un replay devuelve la confirmación financiera sin repetir feedback de presupuesto
o propuesta recurrente. Los mensajes configurados no sustituyen la presentación
protegida. El piloto no ejecuta automáticamente compensaciones de presupuesto:
son efectos fuera del catálogo de esta primera entrega.

Los recibos son datos financieros de backend: RLS habilitado, ningún permiso de
`PUBLIC`, `anon` o `authenticated`. El despliegue debe usar un rol servidor
autorizado (owner/BYPASSRLS o una política revisada), nunca una credencial pública.
No se comprobó ni cambió el rol real de producción.

## Verificación

### Etapa 4 — paridad implementada y gate final aprobado

Las nuevas capacidades usan el mismo registro, workspace y entrada protegida.
Las operaciones de lectura incompletas pueden recopilar filtros y producir
imágenes/datos al completarse, sin confirmar una escritura inexistente. Un
resume collecting permite continuar el loop y aplicar los datos del mismo
mensaje; la pregunta del backend queda como fallback si siguen faltando datos.
Cancelar una operación hija retoma el padre por su vínculo backend sin aplicarlo.

La proyección publica una referencia vigente por entidad tras mutar. Las refs
anteriores siguen conservando sus snapshots y las propuestas viejas se rechazan;
no se actualiza silenciosamente un comando sellado. Editor/botones/listas incluyen
un binding interno en su token, separado de variables editables, y lo conservan
al navegar: un botón anterior del mismo nodo no puede ejecutar otra operación.

El coordinador cerró la revisión independiente y ejecutó un ensayo real con
DeepSeek `deepseek-flash`, sin thinking. El tercer intento pasó diez pasos de
edición/anulación y de recordatorio incompleto, interrupción por lectura,
completar día, confirmar y pausar. Los dos intentos anteriores revelaron refs
obsoletas y un cierre prematuro de resume; se corrigieron ambos. El primer
checker buscaba el título exacto “Internet” aunque el modelo había creado
legítimamente “Pagar Internet”: ahora captura el único recordatorio propio
correspondiente y comprueba su ID/título exactos y demás campos al pausar.

Hay un pase aprobado del escenario y dos intentos anteriores conservados;
no se midió una tasa de fiabilidad. SQLite, Redis, historial y entregas fueron
sintéticos; el proveedor fue real. No es aceptación de WhatsApp o producción.
El cierre focalizado pasó 130 pruebas. Tras la revisión y el freeze final, el
coordinador ejecutó una vez la suite completa: **1976 passed**, dos avisos de
deprecación de dependencias, 99,41 segundos y salida 0. Son 48 casos nuevos de
etapa 4 respecto del cierre anterior de 1928. Después sólo se actualizaron documentos.

### Etapa 3 vigente — 28/10/2026

El coordinador revisó independientemente el registro, las transiciones de workspace,
las dos entradas del dispatcher, el callback de entrega, los recibos y su replay.
Tras cerrar producto y pruebas, ejecutó una vez la suite completa mediante el
runner aislado: **1928 passed**, dos avisos de deprecación de dependencias,
88,90 segundos y salida 0. La cobertura previa de piloto/proveedores se portó al
contrato nuevo; las nuevas aceptaciones incluyen scopes inclusivos, febrero,
interrupción y retoma, evidencia entre días, ausencia/antigüedad de entrega,
validación de payload/hash, retención terminal y extensión sin ramas en el loop.

El ensayo real usó el adaptador DeepSeek existente, modelo `deepseek-flash`,
**sin thinking**; no equivale a Harness Flash/Max. La primera consulta compuesta
calculó correctamente totales y corte, pero omitió buscar el gasto individual.
Se precisó en la metadata de `compare_periods` que su descomposición llega a
categorías y que identificar un movimiento requiere consultar detalles con el
scope devuelto. No se añadieron reglas de dominio al runtime ni se debilitó
el pedido o el checker.

El segundo ensayo de esa consulta pasó sus ocho comprobaciones mediante
`compare_periods` → `find_movements` → `present_result`: gastos actuales ARS 6000,
base ARS 2000, diferencia ARS 4000, corte al 28/09, supermercado ARS 5000 incluido
y registro del 29/09 excluido, presentación completa y aislamiento de la otra cuenta.
El recorrido de escritura pasó sus cinco casos: propuesta de registro,
confirmación con fila/recibo persistidos, replay sin duplicar, propuesta de
corrección y confirmación de fecha conservando los demás atributos.

Hubo **un pase aprobado por escenario**; la comparación tuvo además el intento
inicial fallido. No es una tasa medida de fiabilidad semántica. Se usaron servicios de
Luka con SQLite en memoria, Redis simulado e historial visible simulado tras entrega
de texto. El script invocó el callback real bajo una entrega sintética explícita;
no envió a WhatsApp ni evaluó transporte, autenticación, Redis o PostgreSQL de
producción. Los logs locales del ensayo permanecen en `.codex/artifacts/`.

Pruebas unitarias e integración aislada en `tests/test_assistant_pilot.py`,
`tests/test_assistant_stage3.py` y
`tests/test_assistant_tool_providers.py`. Los proveedores HTTP están simulados:
verifican serialización nativa, correlación y conservación de firmas, no calidad
semántica ni disponibilidad de modelos reales. DeepSeek conserva el modo actual
sin thinking; no es equivalente al experimento previo de Harness Flash/Max.

El ensayo local opt-in `.codex/dev.py runtimecheck <provider>` consume el proveedor
configurado con registros sintéticos, SQLite en memoria y Redis simulado. Para
confirmar, el script invoca explícitamente el callback real de entrega y registra
`synthetic_preview_delivery`; simula el éxito del transporte y no envía mensajes
ni demuestra entrega en WhatsApp. En esta etapa no se cambiaron credenciales,
dependencias ni la migración existente de receipts.

`tests/acceptance/assistant_receipts_postgres.py` contiene los checks reutilizables
con PostgreSQL real: doble confirmación concurrente, rollback por constraint del
recibo, corrección concurrente sin alterar otros campos y aplicación de migración
con RLS/revocaciones. La etapa 4 amplía la aceptación con lotes, update/anulación
y la migración finita de command_kind, preservando recibos y permisos. El coordinador
ejecutó los seis grupos PASS con PostgreSQL descartable real.
`run_acceptance(engine, migration_path, command_migration_path)` exige un engine de
PostgreSQL descartable y crea/borra exclusivamente un esquema aleatorio propio.
No apuntarlo a una base existente o compartida. El wrapper local `.codex/dev.py
pgcheck` de esta evaluación permite solo el endpoint efímero exacto y conserva
las restricciones de dotenv, procesos y red; no relaja el runner SQLite habitual.

### Ensayos históricos anteriores a la etapa 3

Las evaluaciones siguientes corresponden al piloto previo de siete esquemas,
anterior al registro actual y a Workspace v2. No describen el catálogo vigente
ni sustituyen la verificación de la etapa 3.

Evaluación nativa histórica del 28/10/2026: Mistral, modelo `ministral-8b-2512`, completó
dos casos sintéticos mediante el adaptador real: `wallet_balances` seguido de
`present_result` conservando la referencia devuelta, y propuesta `register_movement`
con ARS 10000 y fecha 16/09/2026. El primer ensayo de fecha fue rechazado porque
el modelo normalizó la cita; se aclaró en el esquema de DateSpec que evidence_span
debe copiar el texto literal, y el caso pasó en dos ejecuciones posteriores.
La comprobación del importe compara su valor Decimal, sin exigir una única
representación textual equivalente.

Ese ensayo usó los siete esquemas de aquel piloto y un prompt de protocolo específico;
no ejecuta tools sobre una base ni el runtime conversacional completo. No demuestra
paridad funcional, comprensión general ni un porcentaje de fiabilidad del modelo.
En ese corte, los demás proveedores conservaban sólo cobertura HTTP simulada.
No se probaron autenticación de WhatsApp ni producción. El runner opt-in y sus
resultados están en `.codex/` local; no se versionan credenciales ni se leen .env.

### Evaluación histórica posterior con runtime y servicios reales

También se ejecutó el runtime con Mistral real, SQLite en memoria y Redis simulado.
El recorrido final de registro completó propuesta, confirmación, replay sin
duplicado, propuesta de corrección y confirmación preservando los otros campos.
Se verificaron filas y recibos, no solamente texto. No incluye dispatcher,
transporte de WhatsApp ni la infraestructura de PostgreSQL/Redis real.

La consulta compuesta mostró resultados inconsistentes con `ministral-8b-2512`.
Hubo una ejecución correcta, pero la última eligió `last_registered` para buscar
el gasto relevante, mostró un registro fuera del corte y agotó el ciclo sin
presentación completa. No está aceptado ese caso como comportamiento fiable.
El dataset incluye un gasto del 29/09 que debe excluirse del corte al día28.
El checker ahora exige mismo corte, detalle relevante y una ejecución real de
`present_result` con status `presented`; cifras correctas en fallback no bastan.

Los ensayos motivaron ajustes acotados: formato YYYY-MM explícito, feedback de
validación sin ejecutar lotes parcialmente, recuperación de presentación con
referencias completas y contexto del modelo sin UUIDs canónicos alternativos a
los handles. Estado, snapshots y comandos internos conservan sus identificadores.
Estas correcciones no demuestran que la inconsistencia restante sea exclusivamente
del modelo ni validan el reemplazo global.

Franco autorizó contrastar el mismo caso con `mistral-small-latest` sin cambiar
la configuración guardada. Dos intentos, separados por 30 segundos, devolvieron
HTTP429 antes de generar llamadas; el último diagnóstico del proveedor indicó
rate limit. Small no pudo evaluarse: no contar esos intentos como fallos de
razonamiento ni afirmar que resuelve el problema. No se añadieron reintentos
automáticos al adaptador del producto.

## Riesgos y continuación

La comprensión semántica aún no tiene una tasa medida. La omisión de detalle
observada produjo una respuesta incompleta recuperable, sin mutación financiera;
el ajuste de metadata y un caso posterior correcto no demuestran comprensión
general. El transporte y Redis reales no fueron evaluados por estos ensayos;
las pruebas offline verifican los contratos y la conducta ante fallos simulados.

Los contratos, la validación de estado y los facts añaden código y trabajo de
validación/serialización. Ese costo permite extender capacidades y operaciones
sin enumerar nombres de dominio en el loop y conserva cifras, referencias y
autorización bajo control del backend. No se añadieron frameworks, dependencias
ni una nueva base para lograrlo; la retención acota el estado de tareas.

La etapa 4 implementa las capacidades y bindings de la matriz de cobertura y
tiene aprobado su gate final. La etapa 5 debe realizar el corte y
retirar las rutas sustituidas sólo después de esa paridad. Esta entrega no
autoriza publicación ni cambios de producción.

## Desactivación y rollback

Deshabilitar `LUKA_TOOLS_PILOT_ENABLED` detiene nuevos pedidos del piloto. Sus
botones antiguos se rechazan y no se interpretan como acciones legacy. Los
pendientes se cancelan explícitamente o vencen; no se migran por inferencia.

No borrar recibos mientras puedan existir reintentos o mientras se necesite su
trazabilidad. Rollback de código: deshabilitar el piloto y conservar la tabla.
Rollback de esquema, solo tras detener escritores y archivar/verificar los
recibos bajo una autorización aparte: `DROP TABLE public.assistant_write_receipts`.
Ese DROP no revierte movimientos confirmados ni debe ejecutarse automáticamente.

El piloto no reemplaza el PR de fechas globalmente: las rutas fuera del piloto
conservan su comportamiento. STK-247 no queda resuelta para todos los usuarios
por habilitar esta ruta para una sola cuenta.
