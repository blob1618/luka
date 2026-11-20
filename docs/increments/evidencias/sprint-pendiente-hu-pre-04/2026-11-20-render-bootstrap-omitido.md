# Primera ejecución productiva de la carga SEPA

- Fecha: 2026-11-20, horario de Argentina. Ambiente: servicio `luka` en Render, commit `b41ac18` de [PR #127](https://github.com/blob1618/luka/pull/127).
- Configuración aplicada: `SEPA_PROVINCES=AR-H`, `SEPA_INGEST_ENABLED=1`. El despliegue por cambio de entorno terminó correctamente y quedó Live.
- Evidencia: [registros de aplicación de Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/logs?t=app&r=1h&q=sepa). A las 11:06:20 se registraron los jobs `run_sepa_bootstrap` y `run_sepa_ingest`. El primero estaba previsto para las 11:07:20; a las 11:07:23 APScheduler registró `Run time ... was missed by 0:00:02.922704` y lo eliminó.
- Interpretación: el margen por defecto de un segundo hizo que el disparo único se descartara. No hay evidencia de descarga ni de snapshot listo. La tabla `sepa_price` seguía vacía al comprobarla antes de la carga.
- Acción: conceder 300 segundos de tolerancia al disparo inicial y diario, desplegar, y verificar que aparezca `sepa_ingest_ready` y que Supabase tenga filas antes de declarar la carga operativa.
