# Runbook Operacional: Detección Inteligente de Gastos Recurrentes (HU-REM-03 / STK-189)

Este runbook documenta la operación, configuración, observabilidad, procedimientos de mitigación y gestión de incidentes del subsistema de detección automática de gastos recurrentes, presentación interactiva de propuestas y despacho inteligente de recordatorios.

---

## 1. Parámetros de Operación y Configuración

El ciclo de vida del subsistema opera de forma desacoplada entre dos componentes principales: el detector batch en background y el despachador de propuestas/avisos.

### A. Detección Batch en Background (`RecurringExpenseService.run_daily_detection`)
- **Frecuencia y Hora de Ejecución**: Diaria a las **03:00 AM** hora local de Argentina (`America/Argentina/Buenos_Aires`), programada en `app/scheduler.py` con `hour=3, minute=0`.
  > [!NOTE]
  > La hora está definida de forma fija en el scheduler (`hour=3, minute=0, timezone=ARGENTINA_TZ`). No existe variable de entorno `SCHEDULER_RECURRING_DETECTION_HOUR`.
- **Ventana Histórica Móvil (`lookback_months`)**: 4 meses calendario hacia atrás a partir del primer día del mes de cálculo (p. ej., para una corrida el 21 de septiembre de 2026, la ventana abarca desde el 1 de junio de 2026 inclusive).
- **Criterio de Recurrencia**:
  - Mínimo 3 movimientos en meses consecutivos dentro de la ventana de análisis.
  - Tipo de movimiento estricto: `egreso` (`anulado_en IS NULL`).
  - Columna de importe evaluada: `movimientos_financieros.cantidad`.
  - Agrupación por: `usuario_id`, `patron_hash` (hash SHA-256 de descripción normalizada, categoría y moneda).
  - Tolerancia de variación de fecha: la desviación máxima de los días del mes respecto a la mediana debe ser $\le 3$ días.
- **Concurrencia Exclusiva (PostgreSQL Advisory Lock)**:
  - Clave de lock de sesión: `5354418701` (`SELECT pg_try_advisory_lock(5354418701)`).
  - Adquirido en una conexión física dedicada y pineada (`bind.connect()`), evitando fugas de lock al pool (`QueuePool`) causadas por los `session.commit()` intermedios de `detect_candidates()`.
  - Si otra instancia o worker posee el lock, la corrida actual omite la ejecución silenciosamente (`[RECURRING_BATCH_SKIPPED] reason=advisory_lock_busy`).
  - En caso de error de base de datos, la conexión dedicada ejecuta `rollback()` antes de llamar a `pg_advisory_unlock`, garantizando que transacciones abortadas no impidan la liberación física del lock.

### B. Presentación de Propuestas en Webhook (`_check_pending_candidate_proposal`)
- **Adjunción Determinista**: Se evalúa inmediatamente después del registro exitoso de un egreso por parte del usuario.
- **Condiciones de Elegibilidad**:
  1. `Usuario.proactivo_habilitado` debe ser `True`.
  2. El candidato en `public.candidato_gasto_recurrente` debe encontrarse en estado `pendiente`.
  3. `candidato.proxima_fecha_estimada >= hoy_argentina` (la fecha proyectada no debe estar vencida).
  4. Si ya fue propuesto previamente (`propuesta_en IS NOT NULL`), debe haber transcurrido un cooldown de al menos 30 días en UTC.
- **Formato Conversacional**: Adjunta 2 botones interactivos nativos de WhatsApp:
  - `rec_cand:accept:<id>`: "Sí, avisame"
  - `rec_cand:reject:<id>`: "No, gracias"

### C. Conversión y Despacho de Avisos (`check_reminders`)
- **Aceptación**: Al pulsar "Sí, avisame", el candidato pasa a `aceptado` en `public.candidato_gasto_recurrente` y se crea atómicamente un registro en `public.recordatorio` con `origen='recurrente_inteligente'`, `dias_anticipacion=3` y `dia_del_mes=candidato.dia_estimado`.
- **Rechazo**: Al pulsar "No, gracias", el candidato pasa a `rechazado` y se suprime permanentemente cualquier propuesta futura para ese patrón.
- **Scheduler de Envíos**: Se evalúa cada 5 minutos mediante `check_reminders` en `app/scheduler.py`. El día de alerta se calcula como `dia_del_mes - dias_anticipacion` (con ajuste circular para fin de mes).
- **Supresión Determinista**: Antes de emitir el aviso, se verifica si el usuario ya registró un egreso correspondiente al mismo concepto o patrón en el mes corriente. En tal caso, el recordatorio no se envía y se registra un `public.aviso_recordatorio` en estado `suprimido` con `motivo_supresion='gasto_registrado'`.

---

## 2. Política de Consentimiento y Ventana de 24 Horas de WhatsApp

Meta impone restricciones estrictas sobre la mensajería saliente de WhatsApp Business API:

1. **Dentro de Ventana de Atención al Cliente (<= 24 Horas)**:
   - Si el usuario interactuó con el bot en las últimas 24 horas (`Usuario.ultimo_mensaje_recibido >= now - 24h`), el recordatorio puede enviarse como mensaje de texto libre interactivo.
2. **Fuera de Ventana (> 24 Horas)**:
   - WhatsApp no permite el envío de texto libre fuera de la ventana. Es mandatorio utilizar una plantilla de mensaje comercial preaprobada por Meta, especificada en la variable de entorno `WHATSAPP_REMINDER_TEMPLATE_NAME`.

> [!WARNING]
> ### Ausencia de Kill-Switch Técnico y Riesgo de Template Missing
> - **No existe ninguna variable de entorno tipo "kill-switch"** (`FEATURE_FLAG_RECURRING` o similar) para apagar los recordatorios inteligentes en caliente.
> - **Modificar `Usuario.proactivo_habilitado = False` NO es un kill-switch técnico del subsistema**: es una preferencia individual del usuario que desactiva **todas** las notificaciones proactivas (incluyendo el resumen diario matutino y cualquier otra función proactiva). No debe usarse como switch técnico global de la infraestructura.
> - **Remover o dejar en blanco `WHATSAPP_REMINDER_TEMPLATE_NAME` está estrictamente prohibido como mecanismo de apagado**:
>   Si se remueve dicha variable, cualquier aviso dirigido a usuarios fuera de ventana incurre de inmediato en una falla permanente (`WhatsAppDeliveryStatus.PERMANENT`) con error `template_missing`, registrando `public.aviso_recordatorio` en estado `failed` y degradando las métricas de confiabilidad del servicio ante Meta.
> - Para suspender envíos o el subsistema de forma segura, referirse a la sección de **Mitigación y Respuesta a Incidentes**.

---

## 3. Métricas y Observabilidad (Sin PII)

Bajo las políticas de seguridad de datos, **los logs nunca deben contener teléfonos de usuarios (`whatsapp_id`), descripciones personales, importes individuales ni información confidencial**. Toda la telemetría utiliza identificadores UUID, códigos de estado y métricas agregadas.

### A. Eventos Estructurados en Logs

Los siguientes son los eventos efectivamente instrumentados y emitidos por el código en `app/scheduler.py` y `app/services/dispatcher.py`:

| Evento / Mensaje de Log | Módulo | Nivel | Significado y Formato |
| --- | --- | --- | --- |
| `[DAILY_DETECTION] Completado: creados=%d actualizados=%d` | `app.scheduler` | INFO | Resumen tras finalización exitosa del worker batch diario. |
| `[DAILY_DETECTION] Advisory lock 5354418701 ocupado; otro worker está en ejecución.` | `app.scheduler` | INFO | Omisión de ejecución por concurrencia (lock de PostgreSQL ocupado por otra instancia). |
| `[DAILY_DETECTION_ERROR] %s: %s` | `app.scheduler` | ERROR | Excepción capturada durante la ejecución de la detección batch diaria. |
| `[DAILY_DETECTION] Error al liberar advisory lock: %s` | `app.scheduler` | WARNING | Falla en `pg_advisory_unlock` en el bloque `finally`, desencadenando `invalidate()`. |
| `[DAILY_DETECTION] Tarea ya reclamada para la fecha %s` | `app.scheduler` | INFO | Omisión por reclamo de fila única en `CronJobClaim` (entorno local SQLite). |
| `[REMINDER_SENT] user_id=%s reminder=%s periodo=%s` | `app.scheduler` | INFO | Envío exitoso del recordatorio (WhatsApp API retornó `SUCCESS`). |
| `[REMINDER_SUPPRESSED] user=%s reminder=%s motivo=%s` | `app.scheduler` | INFO | Supresión determinista. Motivos: `gasto_registrado`, `candidato_no_activo`, `proactivo_deshabilitado`. |
| `[REMINDER_RETRYABLE_FAIL] aviso=%s attempt=%d/%d` | `app.scheduler` | WARNING | Falla temporal de entrega en Meta; reintento agendado con backoff exponencial. |
| `[REMINDER_PERMANENT_FAIL] aviso=%s error=%s` | `app.scheduler` | WARNING | Falla terminal (p. ej. `template_missing` fuera de ventana o número inválido). |
| `[REMINDER_UNKNOWN_STATUS] aviso=%s` | `app.scheduler` | WARNING | Estado no reconocido devuelto por el cliente de WhatsApp. |
| `[REMINDER_QUERY_ERROR] %s: %s` | `app.scheduler` | ERROR | Excepción en la consulta de evaluación o despacho periódico de recordatorios. |
| `[RECONCILE_STRANDED] Reconciliadas %d filas varadas en 'sending' a 'unknown'` | `app.scheduler` | WARNING | Reconciliación de avisos varados en `sending` tras reinicio o corte del worker. |
| `[RECURRING_PROPOSAL_CHECK_ERROR] %s: %s` | `app.services.dispatcher` | DEBUG | Excepción capturada al verificar si un egreso registrado es candidato a propuesta. |

### B. Paneles y KPIs Recomendados
- **Tasa de Detección**: Cantidad de nuevos candidatos creados en `public.candidato_gasto_recurrente` por corrida nocturna.
- **Ratio de Aceptación (Opt-in)**: Proporción de candidatos aceptados vs rechazados (`aceptado / (aceptado + rechazado)`).
- **Ratio de Supresión por Pago Previo**: Porcentaje de avisos en `public.aviso_recordatorio` con `estado='suprimido'` y `motivo_supresion='gasto_registrado'`.
- **Tasa de Errores de Plantilla Meta**: Cantidad de eventos `[REMINDER_PERMANENT_FAIL]` con `error=template_missing` o fallas de entrega de Meta.

---

## 4. Procedimientos de Mitigación y Contingencia

### Escenario 1: Necesidad de Pausar la Detección Batch Nocturna
- **Causa**: Mantenimiento en base de datos, sobrecarga en el worker o requerimiento de suspender temporalmente la generación de nuevas propuestas.
- **Acción Operativa**:
  1. No alterar variables de entorno del webhook ni vaciar plantillas.
  2. En el despliegue del worker de scheduler, comentar temporalmente el registro del job `recurring_expense_detection` en `app/scheduler.py` (líneas 750-756):
     ```python
     # scheduler.add_job(
     #     _run_daily_recurring_detection_sync,
     #     trigger="cron",
     #     hour=3,
     #     minute=0,
     #     timezone=ARGENTINA_TZ,
     #     id="recurring_expense_detection",
     #     replace_existing=True,
     # )
     ```
  3. Desplegar el cambio en el servicio worker. Los recordatorios ya aceptados en `public.recordatorio` continuarán despachándose normalmente.

### Escenario 2: Solicitud de Usuario de No Recibir Mensajes Proactivos
- **Causa**: El usuario solicita explícitamente no recibir propuestas automáticas ni mensajes proactivos del bot.
- **Acción Operativa**:
  1. Actualizar la preferencia de consentimiento del usuario en la base de datos:
     ```sql
     UPDATE public.usuario
     SET proactivo_habilitado = FALSE, actualizado_en = NOW()
     WHERE id = '<usuario_uuid>';
     ```
  2. Esto desactiva de inmediato la adjunción de propuestas interactivas en el dispatcher y los recordatorios proactivos diarios para este usuario específico.

### Escenario 3: Falla Permanente por Plantilla de Meta (`template_missing` o desaprobada)
- **Causa**: Se reciben alertas operacionales con `[REMINDER_PERMANENT_FAIL] error=template_missing` o códigos 132000 / 132001 de Meta.
- **Acción Operativa**:
  1. Verificar en Meta Business Manager el estado de la plantilla para recordatorios.
  2. Confirmar que la variable de entorno `WHATSAPP_REMINDER_TEMPLATE_NAME` en producción coincida exactamente con el nombre de la plantilla aprobada en Meta.
  3. Si la plantilla fue actualizada o renombrada, ajustar la variable en la configuración del entorno y reiniciar el worker.

---

## 5. Procedimientos de Rollback

### Rollback de Código (Git Rollback / Redeploy Anterior)
- **Estrategia**: Reversión a nivel de aplicación mediante Git (revert del commit o redeploy del tag previo).
- **Retrocompatibilidad Garantizada**:
  - STK-189 es un ticket de validación integrada y documentación operacional; no introduce migraciones ni cambios de esquema en base de datos.
  - El esquema introducido en STK-186/STK-187 (`public.candidato_gasto_recurrente` y las columnas `origen`, `candidato_id` en `public.recordatorio`) es **100% retrocompatible**: todas las columnas nuevas son nulables con valores por defecto.
  - El código de Luka anterior a estas HU ignora por completo la tabla `candidato_gasto_recurrente` y las columnas agregadas, funcionando con normalidad sin requerir ninguna modificación ni intervención en la base de datos.
- **Prohibición de Reversiones Destructivas de Esquema**:
  - **No ejecutar sentencias destructivas (`DROP TABLE`, `DROP COLUMN`, `CASCADE`) en la base de datos**.
  - Los datos históricos de candidatos y recordatorios deben permanecer íntegros para auditoría y reanudación posterior sin pérdida de información.
