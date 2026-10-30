# Herramientas de Luka — etapa 5

Texto, botones y listas entran al mismo dispatcher y runtime para todas las
cuentas que superan onboarding. Se mantienen las 42 herramientas y servicios de la
[matriz de etapa 4](assistant-stage4-coverage.md). El [cierre de etapa 5](assistant-stage5-coverage.md)
documenta corte, pruebas y límites; el [mapa cualificado](assistant-stage5-test-map.csv)
traza los 1601 símbolos originales sin confundir contratos retirados con capacidad perdida.

No hay flags de piloto por teléfono ni fallback a intents/normalizadores.
El catálogo compone contratos y políticas; el loop no enumera dominios.
`llm.py` conserva renderer/configuración/provider, sin clasificador ni stubs
multimedia ficticios. El prompt breve del agente es `prompts/core_prompt.md`;
la sidebar del simulador modifica ese prompt realmente.

## Autoridad y operaciones

Backend fija cuenta, reloj, datos/facts, snapshots y referencias opacas con dueño,
conversación, tipo, esquema y vencimiento. El proveedor no recibe UUIDs canónicos
ni decide commits. Períodos civiles son inclusivos, filtros de scopes sólo se
restringen y fechas conservan evidencia/día del turno que las aporta.

Workspace v2 usa namespace `assistant:v3` al corte, con una activa, hasta tres
abiertas y tres terminales; vigencia de operación: 30 minutos sin renovarla al retomar.
Estado previo incompatible no se migra inventando economic_effect. Receipts se
conservan. Lectura/nuevo pedido suspende el borrador y revoca confirmación anterior.
Selecciones de campos/tareas no se arrastran; hija categoría preserva campos del
padre y no lo aplica automáticamente.

Toda mutación requiere propuesta completa entregada y confirmación posterior
con operación/revisión/hash. Un callback viejo no habilita otra revisión; un sí
después de interrumpir o un botón anterior no autoriza. Lecturas incompletas,
como gráficos, recopilan filtros y producen multimedia sin aprobación financiera.

La política económica requiere efecto explícito: cash_flow para ingreso/gasto
real; principal, colocación e inversión existente se rechazan. Indeterminación
pide aclarar. No se reubicó un parser regex de inversiones ni un default permisivo.

## Transacción, presentación y entrega

Movimiento/receipt financiero son atómicos; lote de 2–5 usa IDs estables por item.
Replay devuelve resultado guardado sin feedback duplicado. Otros dominios usan
garantías concretas de sus servicios, sin una promesa genérica entre operaciones.
Compensación sugerida es otro borrador revisable, sin autocommit.

Facts obligatorios se presentan desde backend, aunque el modelo no copie sus
refs. Patrimonio/cotizaciones conservan renderer protegido; gráficos producen
PNG/captions backend. Editor acompaña esa presentación con binding interno por
operación/revisión o nonce, preservado entre nodos.

El wrapper acredita preview sólo tras envío exitoso de la propuesta completa y
guarda el único historial visible después de enviar. Propuesta recurrente acredita
su mensaje exacto, no otro followup. Reset limpia historial tras entrega y conserva
registros/recibos. BackgroundTasks sigue sin ser cola durable.

WebhookModeService comparte dispatcher y ambos wrappers, con inbound IDs únicos,
botones/listas, imágenes, links, historial, callback y trazas. La entrega local es
sintética explícita; no se presenta como aceptación de WhatsApp.

## Evidencia al cierre

- Coordinador: PostgreSQL Stage5 nuevo/descartable, 6 grupos PASS de concurrencia,
  rollback, lotes/update/anulación y migraciones/RLS/CHECK; cluster detenido con salida 0.
- DeepSeek real `deepseek-flash`, sin thinking: 20/20 pasos por entrada integrada,
  con Redis falso, HistoryService real y entrega sintética. Un escenario aprobado no mide fiabilidad
  semántica ni prueba Meta/Redis/producción.
- Primer verify integral detectó 18 imports de parser privado en setup de
  TestConceptValidation. Se retiró esa clase con trazabilidad, sin restaurar helpers.
- Verify final del coordinador: 1309 passed, 6 skipped (Redis real), 2 avisos de
  deprecación, 111.83 s; lint/tests 0 y diff-check 0. No cambios semánticos posteriores.
- Revisión independiente posterior (30/10): 5 correcciones acotadas con 7 regresiones;
  suite **1316 passed, 6 skipped** en 148.74 s; aceptación real **8/8** con
  `deepseek-flash` por el dispatcher unificado. Detalle en el
  [cierre de etapa 5](assistant-stage5-coverage.md).

El costo de contratos/facts y validación compra extensión sin ramas del core y
control backend de efectos. La selección semántica aún depende del modelo y no
tiene tasa medida. Despliegue e infraestructura real quedan fuera de esta entrega.
El [histórico hasta etapa 4](assistant-tools-history.md) preserva ensayos y gates
anteriores; sus flags y rutas no describen el motor vigente.
