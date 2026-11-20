# Falla HTTP de la primera carga productiva

- Fecha: 2026-11-20, horario de Argentina. Ambiente: Render, servicio `luka`, commit `ade009f` de [PR #128](https://github.com/blob1618/luka/pull/128), con `SEPA_PROVINCES=AR-H` y `SEPA_INGEST_ENABLED=1`.
- [Registros de Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/logs?t=app&r=1h&q=sepa): a las 11:13:47 se registró `run_sepa_bootstrap`; a las 11:14:53 se inició el job programado para las 11:14:47. A las 11:14:55 apareció `sepa_bootstrap_failed error=HTTPStatusError`.
- El error ocurrió antes de publicar un snapshot listo. El registro sólo conservó el tipo de excepción, sin el código HTTP ni la URL de la respuesta. Desde el entorno local, el catálogo oficial respondió HTTP 200 en una comprobación contemporánea; eso no prueba la conectividad desde Render.
- Cambio: registrar la excepción completa en los jobs SEPA. El siguiente despliegue ejecutará de nuevo la carga inicial y permitirá ver el código HTTP y el recurso que falló. La causa externa y la disponibilidad productiva siguen pendientes.
