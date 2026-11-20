# Ficha de incremento — HU-PRE-04 · Comparador de precios de supermercados

[Evidencias versionadas](evidencias/sprint-pendiente-hu-pre-04/)

## 1. Alcance y compromiso

- Historia y valor: [STK-262 / HU-PRE-04](https://philippem.atlassian.net/browse/STK-262), comparar precios y ofertas de un producto o canasta antes de comprar. Estimación acordada: 8 SP, sin puntos adicionales para subtareas.
- Autor de implementación: blob1618. Persona designada para validar, release, sprint, Sprint Goal, capacidad, priorización y fecha de Planning: Pendiente de confirmar en la planificación. No se infieren del estado de Jira.
- Fuente y dependencia: [STK-264](https://philippem.atlassian.net/browse/STK-264) seleccionó el ZIP minorista diario de SEPA/Precios Claros, sin API oficial por producto verificada. El CA4 originalmente habla de varias APIs; con esta fuente se verifica degradación por carga fallida, snapshot viejo o cobertura parcial.
- Estado: En curso en Jira desde 2026-11-19. Implementación inicial en [PR #123](https://github.com/blob1618/luka/pull/123), primera carga automática en [PR #127](https://github.com/blob1618/luka/pull/127) y carga productiva en [PR #133](https://github.com/blob1618/luka/pull/133), aún sin aceptación humana.

## 2. Implementación y operación

- Consulta de producto por nombre y de canasta por lista. Una coincidencia ambigua pide marca o presentación, sin exigir códigos al usuario. Se muestran comercio, sucursal, localidad, fecha del dato, precio y promoción general verificable. El ahorro entre sucursales sólo compara el mismo producto, presentación y fecha. Faltantes e indisponibilidad son explícitos. La consulta no registra gastos.
- Ingesta asíncrona programada del ZIP oficial mediante [servicio](../../app/services/sepa_ingest.py); publica el snapshot al terminar y mantiene el previo si falla. Comparador y caché Redis en [servicio](../../app/services/supermarket_prices.py); integración conversacional en el catálogo de herramientas. [Guía de operación](../supermarket-prices.md).
- Modelos y [migración versionada](../../supabase/migrations/20261119190000_supermarket_prices.sql) para snapshot y precios. `SEPA_PROVINCES` limita cobertura; `SEPA_INGEST_ENABLED` inicia apagado por defecto. En Render están configurados `AR-H` y `1`; la carga productiva del 20/11/2026 publicó 43.142 precios. La tarea corre en un único worker.
- [PR #127](https://github.com/blob1618/luka/pull/127) agrega la primera carga automática al iniciar sin snapshot y tolera un ZIP interno vacío y filas CSV incompletas encontrados en el archivo oficial del 2026-11-19. El cron diario continúa a las 17:30 de Argentina.
- La primera ejecución en Render del 2026-11-20 se descartó por un retraso de 2,92 segundos frente al margen por defecto de un segundo. La corrección amplía a cinco minutos el margen de la carga inicial y de la actualización diaria; [evidencia operativa](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-bootstrap-omitido.md). En ese momento la carga productiva no se había completado.
- Tras el despliegue de esa corrección, la carga inicial sí comenzó, pero falló al solicitar el catálogo con `HTTPStatusError`. El registro sólo mostró el tipo de excepción; se añade el detalle de la respuesta HTTP para identificar y corregir la causa. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-http-error.md).
- El diagnóstico del 2026-11-20 confirmó HTTP 403 del catálogo oficial desde Render, mientras que la misma URL devolvió HTTP 200 desde el entorno local. Se prueba un `User-Agent` que identifica a Luka para comprobar si el rechazo se debe a la identificación del cliente; no se asume que resuelva un bloqueo de la red de Render. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-403.md).
- El `User-Agent` tampoco resolvió el 403. Se agrega una ruta de lectura de las cabeceras de los siete ZIP semanales oficiales para elegir el más reciente cuando el API de metadatos responde 403, con límite de antigüedad y tamaño; la descarga real desde Render sigue pendiente de comprobarse. [Evidencia](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-recursos-semanales.md).
- El despliegue `347d212` comprobó que Render recibe 403 también de los siete ZIP oficiales. [PR #132](https://github.com/blob1618/luka/pull/132) agrega un espejo automático en una release pública del proyecto, actualizado desde GitHub Actions, con fecha oficial y SHA-256 verificado antes de cargar. En ese momento la publicación y la ingesta productiva aún debían comprobarse. [Evidencia del bloqueo](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-zip-403.md).
- GitHub Actions también recibió 403 del origen oficial. Se publicó manualmente el ZIP compacto AR-H del 19/11 para iniciar la carga; Render lo descargó, pero PostgreSQL rechazó la primera inserción por prepared statements incompatibles con el pooler transaccional. El workflow nuevo publicó correctamente desde la copia archivada fechada con metadatos oficiales incorporados, y la conexión PostgreSQL deshabilita los prepared statements automáticos. El nuevo despliegue publicó 43.142 precios en Supabase. [Evidencia](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-espejo-archivo-pooler.md). La ejecución diaria programada y WhatsApp real siguen pendientes de verificar.
- Recuperación: revisar logs `sepa_ingest_failed`, reintentar manualmente; el snapshot anterior sirve sólo dentro de dos días. Sin snapshot confiable, responder indisponibilidad. Redis expira cada comparación en 30 minutos y la clave incluye snapshot y fecha.
- La primera carga productiva tardó 12m32s desde la lectura del espejo hasta `sepa_ingest_ready` y publicó 43.142 precios. Quedan pendientes la medición de almacenamiento, la próxima actualización diaria y una prueba satisfactoria en WhatsApp real. El catálogo oficial declara CC-BY-4.0 y Luka muestra fuente y fecha. El dato diario no asegura precio de góndola ni existencia de stock.
- La primera prueba real de WhatsApp aportada por la persona usuaria el 20/11/2026, 14:03–14:05, **rechazó la búsqueda por nombre**: Luka no encontró Traviata ni arroz en Resistencia aunque el snapshot contenía esos productos. La provincia «Chaco» no coincidía con el código «AR-H» del archivo, y la búsqueda genérica de arroz priorizaba alfajores. Se corrigieron ambos recorridos y se añadieron pruebas; [diagnóstico y evidencia](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-whatsapp-busqueda-provincia.md). Una nueva prueba real sigue pendiente.

| Hito | Fecha | Evidencia |
| --- | --- | --- |
| Inicio e implementación local | 2026-11-19 | Commits `eea46dd`, `0011fd2`, `168253c` |
| Lista para revisión | 2026-11-19 | [PR #123](https://github.com/blob1618/luka/pull/123), borrador |
| Corrección operativa y piloto con archivo real | 2026-11-20 | [PR #127](https://github.com/blob1618/luka/pull/127) y [registro del piloto](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md) |
| Despliegue inicial y falla del disparo | 2026-11-20 | Merge `b41ac18`, Render desplegó la versión a las 11:06 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-bootstrap-omitido.md) |
| Ejecución de la carga y error HTTP | 2026-11-20 | Merge `ade009f`, Render desplegó la versión y ejecutó el job a las 11:14 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-http-error.md) |
| Diagnóstico de la respuesta HTTP | 2026-11-20 | Merge `dcaa823`, Render confirmó 403 a las 11:24 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-403.md) |
| Identificación del cliente y ruta alternativa | 2026-11-20 | Merge `e295769`, Render mantuvo 403 a las 11:33 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-recursos-semanales.md) |
| Comprobación de los siete ZIP oficiales | 2026-11-20 | Merge `347d212`, Render recibió 403 de los siete a las 11:43 AR; [registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-render-zip-403.md) |
| Espejo verificable | 2026-11-20 | [PR #132](https://github.com/blob1618/luka/pull/132) integrado; primer workflow falló por 403 oficial |
| Primera release y error de PostgreSQL | 2026-11-20 | [Release](https://github.com/blob1618/luka/releases/tag/sepa-feed), bootstrap descargó el ZIP y encontró el [error del pooler](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-espejo-archivo-pooler.md) |
| Corrección y carga productiva | 2026-11-20 | [PR #133](https://github.com/blob1618/luka/pull/133) integrado como `fa50352`; [Render Live](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/deploys/dep-dc06l6m7bikc73a1dio0); `sepa_ingest_ready` a las 12:58 AR con 43.142 precios |
| Aceptación humana y cierre Jira | Pendiente | Requieren conclusión explícita de la persona designada |

## 3. Verificación técnica y aceptación

| Prueba | Fecha, ambiente y versión | Resultado y evidencia |
| --- | --- | --- |
| Lint y suite completa de la rama combinada con HU-PRE-05 | 2026-11-19, local, `a6e58ce` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.358 passed y 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-19-verificacion-local.md). |
| Backend, migraciones y trazabilidad en CI | 2026-11-19, GitHub Actions, `8243074` | [CI Pipeline aprobado](https://github.com/blob1618/luka/actions/runs/44648812473) y [Evidencia del incremento aprobada](https://github.com/blob1618/luka/actions/runs/44648808123). No acredita entorno productivo ni WhatsApp real. |
| Ingesta del ZIP real y búsqueda por nombre | 2026-11-20, SQLite local aislada, PR #127 | 43.142 precios AR-H publicados; «rex traviata» en Resistencia ofrece dos presentaciones. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md). No acredita PostgreSQL productivo ni WhatsApp real. |
| Lint y suite completa de la corrección | 2026-11-20, local, `0e53e13` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.363 aprobadas y 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-piloto-sepa-real.md). |
| Espejo y verificación de integridad | 2026-11-20, local, `7fe91a6` | `ruff check .` sin hallazgos; `python -m pytest -q -m unit`: 201 aprobadas; `python -m pytest -v`: 1.368 aprobadas y 2 warnings de dependencias. PR #132. |
| Publicación mediante archivo histórico y corrección del pooler | 2026-11-20, local y GitHub Actions, `88fb1c9` | `ruff check .` sin hallazgos; 203 tests unitarios y 1.370 en la suite completa aprobados; [workflow del espejo aprobado](https://github.com/blob1618/luka/actions/runs/44765414857). |
| Carga y datos productivos | 2026-11-20, Render y Supabase, `fa50352` | [Render registró `sepa_ingest_ready`](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/logs?t=app&r=1h&q=sepa_ingest_ready): 43.142 precios, 27.767 descartes. Supabase mostró 43.142 filas y 24 coincidencias de Traviata en Resistencia. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-20-espejo-archivo-pooler.md). |

| Criterio de [STK-262](https://philippem.atlassian.net/browse/STK-262) | Evidencia técnica | Conclusión humana |
| --- | --- | --- |
| CA1 producto y sin movimiento | Tests contractuales y consulta WhatsApp simulada con lista; conversación real del 20/11 rechazó la búsqueda por nombre y motivó una corrección | Pendiente; repetir en WhatsApp |
| CA2 canasta y faltantes | Tests de comparación por producto y faltantes explícitos | Pendiente |
| CA3 caché y vigencia | Tests de clave por snapshot/consulta/fecha y TTL | Pendiente |
| CA4 degradación controlada | Tests de fallo de carga y preservación de snapshot; cobertura parcial informada | Pendiente |
| CA5 sin resultados confiables | Tests sin precios inventados y respuesta de indisponibilidad | Pendiente |

Demo humana pendiente: consultar producto/canasta desde WhatsApp real, verificar fecha, sucursal, precio, faltantes y ausencia de movimientos. Definition of Done: pendientes revisión humana, próxima actualización diaria, medición de almacenamiento y conclusión explícita de la persona designada.

## 4. Seguimiento durante el sprint

2026-11-19: implementación y pruebas locales. 2026-11-20: piloto local con archivo diario real, PR #127 integrado y Render configurado para AR-H. La primera carga se omitió por 2,92 segundos de retraso; PR #128 corrigió el margen y el job arrancó, pero el API de metadatos y los siete ZIP oficiales devolvieron HTTP 403 desde Render y GitHub Actions. Una release manual permitió descargar el ZIP compacto, pero la primera inserción falló por prepared statements del pooler. PR #133 corrigió la conexión y permitió una carga completa desde el archivo histórico fechado. WhatsApp real, Sprint y burndown siguen pendientes; no se contabilizan 8 SP como aceptados.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. La HU sigue en curso.

## 6. Sprint Retrospective

Pendiente.

## 7. Traslado a la planificación

Pendiente. Conservar 8 SP y registrar por separado implementación, CI, merge, despliegue y aceptación.
