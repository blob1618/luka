# Ficha de incremento — HU-PRE-04 · Comparador de precios de supermercados

[Evidencias versionadas](evidencias/sprint-pendiente-hu-pre-04/)

## 1. Alcance y compromiso

- Historia y valor: [STK-262 / HU-PRE-04](https://philippem.atlassian.net/browse/STK-262), comparar precios y ofertas de un producto o canasta antes de comprar. Estimación acordada: 8 SP, sin puntos adicionales para subtareas.
- Autor de implementación: blob1618. Persona designada para validar, release, sprint, Sprint Goal, capacidad, priorización y fecha de Planning: Pendiente de confirmar en la planificación. No se infieren del estado de Jira.
- Fuente y dependencia: [STK-264](https://philippem.atlassian.net/browse/STK-264) seleccionó el ZIP minorista diario de SEPA/Precios Claros, sin API oficial por producto verificada. El CA4 originalmente habla de varias APIs; con esta fuente se verifica degradación por carga fallida, snapshot viejo o cobertura parcial.
- Estado: En curso en Jira desde 2026-11-19. Implementación inicial en [PR #123](https://github.com/blob1618/luka/pull/123) y primera carga automática en [PR #127](https://github.com/blob1618/luka/pull/127), aún sin aceptación humana.

## 2. Implementación y operación

- Consulta de producto por nombre y de canasta por lista. Una coincidencia ambigua pide marca o presentación, sin exigir códigos al usuario. Se muestran comercio, sucursal, localidad, fecha del dato, precio y promoción general verificable. El ahorro entre sucursales sólo compara el mismo producto, presentación y fecha. Faltantes e indisponibilidad son explícitos. La consulta no registra gastos.
- Ingesta asíncrona programada del ZIP oficial mediante [servicio](../../app/services/sepa_ingest.py); publica el snapshot al terminar y mantiene el previo si falla. Comparador y caché Redis en [servicio](../../app/services/supermarket_prices.py); integración conversacional en el catálogo de herramientas. [Guía de operación](../supermarket-prices.md).
- Modelos y [migración versionada](../../supabase/migrations/20261119190000_supermarket_prices.sql) para snapshot y precios. `SEPA_PROVINCES` limita cobertura; `SEPA_INGEST_ENABLED` inicia apagado. Las tablas están presentes en Supabase pero vacías al 2026-11-20; la ingesta productiva sigue pendiente. La tarea corre en un único worker.
- [PR #127](https://github.com/blob1618/luka/pull/127) agrega la primera carga automática al iniciar sin snapshot y tolera un ZIP interno vacío y filas CSV incompletas encontrados en el archivo oficial del 2026-11-19. El cron diario continúa a las 17:30 de Argentina.
- La primera ejecución en Render del 2026-11-20 se descartó por un retraso de 2,92 segundos frente al margen por defecto de un segundo. La corrección amplía a cinco minutos el margen de la carga inicial y de la actualización diaria; [evidencia operativa](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-bootstrap-omitido.md). La carga productiva aún no se completó.
- Tras el despliegue de esa corrección, la carga inicial sí comenzó, pero falló al solicitar el catálogo con `HTTPStatusError`. El registro sólo mostró el tipo de excepción; se añade el detalle de la respuesta HTTP para identificar y corregir la causa. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-http-error.md).
- El diagnóstico del 2026-11-20 confirmó HTTP 403 del catálogo oficial desde Render, mientras que la misma URL devolvió HTTP 200 desde el entorno local. Se prueba un `User-Agent` que identifica a Luka para comprobar si el rechazo se debe a la identificación del cliente; no se asume que resuelva un bloqueo de la red de Render. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-403.md).
- Recuperación: revisar logs `sepa_ingest_failed`, reintentar manualmente; el snapshot anterior sirve sólo dentro de dos días. Sin snapshot confiable, responder indisponibilidad. Redis expira cada comparación en 30 minutos y la clave incluye snapshot y fecha.
- Riesgos pendientes: medir duración y tamaño de la carga en PostgreSQL productivo y probar WhatsApp real. El catálogo oficial declara CC-BY-4.0 y Luka muestra fuente y fecha. El dato diario no asegura precio de góndola ni existencia de stock.

| Hito | Fecha | Evidencia |
| --- | --- | --- |
| Inicio e implementación local | 2026-11-19 | Commits `eea46dd`, `0011fd2`, `168253c` |
| Lista para revisión | 2026-11-19 | [PR #123](https://github.com/blob1618/luka/pull/123), borrador |
| Corrección operativa y piloto con archivo real | 2026-11-20 | [PR #127](https://github.com/blob1618/luka/pull/127) y [registro del piloto](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md) |
| Despliegue inicial y falla del disparo | 2026-11-20 | Merge `b41ac18`, Render desplegó la versión a las 11:06 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-bootstrap-omitido.md) |
| Ejecución de la carga y error HTTP | 2026-11-20 | Merge `ade009f`, Render desplegó la versión y ejecutó el job a las 11:14 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-http-error.md) |
| Diagnóstico de la respuesta HTTP | 2026-11-20 | Merge `dcaa823`, Render confirmó 403 a las 11:24 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-403.md) |
| Revisión, merge, despliegue, aceptación y cierre Jira | Pendiente | Cada hito requiere registro propio |

## 3. Verificación técnica y aceptación

| Prueba | Fecha, ambiente y versión | Resultado y evidencia |
| --- | --- | --- |
| Lint y suite completa de la rama combinada con HU-PRE-05 | 2026-11-19, local, `a6e58ce` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.358 passed y 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-19-verificacion-local.md). |
| Backend, migraciones y trazabilidad en CI | 2026-11-19, GitHub Actions, `8243074` | [CI Pipeline aprobado](https://github.com/blob1618/luka/actions/runs/44648812473) y [Evidencia del incremento aprobada](https://github.com/blob1618/luka/actions/runs/44648808123). No acredita entorno productivo ni WhatsApp real. |
| Ingesta del ZIP real y búsqueda por nombre | 2026-11-20, SQLite local aislada, PR #127 | 43.142 precios AR-H publicados; «rex traviata» en Resistencia ofrece dos presentaciones. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md). No acredita PostgreSQL productivo ni WhatsApp real. |
| Lint y suite completa de la corrección | 2026-11-20, local, `0e53e13` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.363 aprobadas y 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md). |

| Criterio de [STK-262](https://philippem.atlassian.net/browse/STK-262) | Evidencia técnica | Conclusión humana |
| --- | --- | --- |
| CA1 producto y sin movimiento | Tests contractuales y consulta WhatsApp simulada con lista; no se persiste movimiento | Pendiente |
| CA2 canasta y faltantes | Tests de comparación por producto y faltantes explícitos | Pendiente |
| CA3 caché y vigencia | Tests de clave por snapshot/consulta/fecha y TTL | Pendiente |
| CA4 degradación controlada | Tests de fallo de carga y preservación de snapshot; cobertura parcial informada | Pendiente |
| CA5 sin resultados confiables | Tests sin precios inventados y respuesta de indisponibilidad | Pendiente |

Demo humana: cargar un snapshot real en staging, consultar producto/canasta desde WhatsApp, verificar fecha, sucursal, precio, faltantes y ausencia de movimientos. Definition of Done: pendientes revisión humana, carga real, medición, merge, despliegue y conclusión explícita de la persona designada.

## 4. Seguimiento durante el sprint

2026-11-19: implementación y pruebas locales. 2026-11-20: piloto local con archivo diario real, PR #127 integrado y Render configurado para AR-H. La primera carga se omitió por 2,92 segundos de retraso; PR #128 corrigió el margen y el job arrancó, pero la fuente devolvió HTTP 403. Se prueba una identificación HTTP explícita y se mantiene pendiente la validación de Supabase y WhatsApp. Sprint y burndown: Pendiente de identificación; no se contabilizan 8 SP como aceptados.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. La HU sigue en curso.

## 6. Sprint Retrospective

Pendiente.

## 7. Traslado a la planificación

Pendiente. Conservar 8 SP y registrar por separado implementación, CI, merge, despliegue y aceptación.
