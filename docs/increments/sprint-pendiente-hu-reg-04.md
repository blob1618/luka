# Ficha de incremento — HU-REG-04 · Continuidad de categorías desde PDFs

[Evidencias versionadas](evidencias/sprint-pendiente-hu-reg-04/)

## 1. Alcance y compromiso

- Historia: [STK-38 / HU-REG-04](https://philippem.atlassian.net/browse/STK-38). Corrección compartida de [#119](https://github.com/blob1618/luka/issues/119) y [#120](https://github.com/blob1618/luka/issues/120).
- Valor: completar la categoría y los datos pendientes del comprobante correcto, revisar la propuesta y confirmar el gasto por separado.
- Sprint, Release, Sprint Goal, responsables humanos, capacidad, priorización y SP: Pendiente de confirmar; no se modifica la planificación ni se suman puntos.
- Estado: implementación en revisión técnica; aceptación humana Pendiente.
- Criterios de origen: los dos issues enlazados. Antecedentes: PR #115 y #116.

## 2. Implementación y operación

- Pregunta tipada ligada a operación, revisión, campo, opciones y vencimiento. Referencias propias validan dueño y conversación. Se acredita por el callback compartido después del envío completo.
- Selección inequívoca por nombre normalizado o número: completa el campo mediante backend. Texto libre usa `answer_category_question`, que deriva la relación padre/hija. Crear categoría independiente sigue siendo una operación independiente.
- Confirmar categoría muestra su resultado y el siguiente dato o propuesta del gasto. No registra el gasto sin una nueva propuesta entregada y una confirmación posterior.
- Sugerencia extraída, categoría requerida y selección explícita son campos separados. Fotos sin fecha piden la fecha del gasto.
- `operation_status` informa estado/recibos sin confirmar ni reanudar. Reanudar el activo vuelve a validar y presentar con revisión nueva. Resúmenes distinguen origen, descripción e importe.
- Sin nuevas dependencias ni migraciones de base. No se accedió a Supabase ni Meta.
- Recuperación: el namespace efímero de Redis cambia de `assistant:v3` a `assistant:v4`. Los borradores anteriores deben reenviarse; registros y recibos durables se conservan. Esta decisión evita atribuir entregas ficticias al estado antiguo.
- Límite: reenvíos con IDs diferentes conservan borradores distintos; no se deduplican compras por contenido del archivo. Opciones vencidas o interrumpidas requieren retomar y revisar.

| Hito | Fecha | Evidencia |
| --- | --- | --- |
| Inicio e implementación local | 2026-11-17 | Rama `codex/stk-37-stk-38-category-continuity` |
| Revisión y merge | Pendiente | PR de esta rama |
| Despliegue Render | Pendiente | No desplegado por esta tarea |
| Validación humana y cierre Jira | Pendiente | Requiere conclusión explícita |

## 3. Verificación técnica y aceptación

[Detalle de pruebas y límites](evidencias/sprint-pendiente-hu-reg-04/2026-11-17-continuidad.md).

| Criterio | Evidencia técnica | Conclusión humana |
| --- | --- | --- |
| Categoría hija devuelve siguiente pregunta o propuesta | Tests por texto, botón y acción vinculada del editor | Pendiente |
| Respuesta de categoría existente completa el comprobante tras consultas previas | PDF e imagen, nombre con espacios/case y número, sin invocación al modelo | Pendiente |
| Confirmaciones separadas y persistencia única | No guarda antes de entregar propuesta; confirma una vez y replay no duplica | Pendiente |
| Interrupción, dos adjuntos, vencimiento y aislamiento | Revisión vieja, callback antiguo, otro dueño y padre cancelado rechazados | Pendiente |
| Consulta de estado y reanudación del activo | Estados backend sin commit ni error interno | Pendiente |
| Proveedor real | Mistral: HTTP 429. Gemini: una continuación correcta y otra confusa; algunas llamadas posteriores: HTTP 429 | Pendiente |

- Definition of Done: pruebas y código revisables; CI se enlaza desde el PR. Integración, despliegue y aceptación en WhatsApp siguen pendientes.
- Demo humana propuesta: PDF con nueva categoría y fecha pendiente; PDF después de consulta con categoría existente; foto con nueva categoría; revisar y confirmar cada gasto, comprobar ausencia de duplicados.

## 4. Seguimiento durante el sprint

2026-11-17: diagnóstico confirmado sobre `f2e25ec`, implementación de ambos issues y regresión aislada. Se actualizan fixtures del nuevo contrato y el reloj fijo de una prueba preexistente de recurrencias que también fallaba en `main` al vencer su fecha.

Burndown, Daily y puntos pendientes: Pendiente; no se infieren de las pruebas.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. No se marca la HU como aceptada ni se altera Jira.

## 6. Sprint Retrospective

Pendiente. Hallazgo técnico para revisar: los tests con reloj fijo deben usar ese reloj también para evaluar vencimientos.

## 7. Traslado a la planificación

Pendiente. Mantener separados implementación, CI, merge, despliegue y aceptación humana.
