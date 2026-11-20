# Ficha de incremento — HU-PRE-04 · Comparador de precios de supermercados

[Evidencias versionadas](evidencias/sprint-pendiente-hu-pre-04/)

## 1. Alcance y compromiso

- Historia y valor: [STK-262 / HU-PRE-04](https://philippem.atlassian.net/browse/STK-262), comparar precios y ofertas de un producto o canasta antes de comprar. Estimación acordada: 8 SP, sin puntos adicionales para subtareas.
- Autor de implementación: blob1618. Persona designada para validar, release, sprint, Sprint Goal, capacidad, priorización y fecha de Planning: Pendiente de confirmar en la planificación. No se infieren del estado de Jira.
- Fuente y dependencia: [STK-264](https://philippem.atlassian.net/browse/STK-264) seleccionó el ZIP minorista diario de SEPA/Precios Claros, sin API oficial por producto verificada. El CA4 originalmente habla de varias APIs; con esta fuente se verifica degradación por carga fallida, snapshot viejo o cobertura parcial.
- Estado: En curso en Jira desde 2026-11-19. Implementación en [PR #123](https://github.com/blob1618/luka/pull/123), aún sin aceptación humana.

## 2. Implementación y operación

- Consulta de producto por EAN o nombre y de canasta por lista. Una coincidencia ambigua por nombre pide EAN. Se muestran comercio, sucursal, localidad, fecha del dato, precio y promoción general verificable. El ahorro entre sucursales sólo compara el mismo EAN, presentación y fecha. Faltantes e indisponibilidad son explícitos. La consulta no registra gastos.
- Ingesta asíncrona programada del ZIP oficial mediante [servicio](../../app/services/sepa_ingest.py); publica el snapshot al terminar y mantiene el previo si falla. Comparador y caché Redis en [servicio](../../app/services/supermarket_prices.py); integración conversacional en el catálogo de herramientas. [Guía de operación](../supermarket-prices.md).
- Modelos y [migración versionada](../../supabase/migrations/20261119190000_supermarket_prices.sql) para snapshot y precios. `SEPA_PROVINCES` limita cobertura; `SEPA_INGEST_ENABLED` inicia apagado. No se ejecutó migración ni ingesta en Supabase/Render. La tarea corre en un único worker.
- Recuperación: revisar logs `sepa_ingest_failed`, reintentar manualmente; el snapshot anterior sirve sólo dentro de dos días. Sin snapshot confiable, responder indisponibilidad. Redis expira cada comparación en 30 minutos y la clave incluye snapshot y fecha.
- Riesgos pendientes: medir capacidad de almacenamiento y duración de carga con una provincia piloto, confirmar licencia/atribución y probar WhatsApp real. El dato diario no asegura precio de góndola ni existencia de stock.

| Hito | Fecha | Evidencia |
| --- | --- | --- |
| Inicio e implementación local | 2026-11-19 | Commits `eea46dd`, `0011fd2`, `168253c` |
| Lista para revisión | 2026-11-19 | [PR #123](https://github.com/blob1618/luka/pull/123), borrador |
| Revisión, merge, despliegue, aceptación y cierre Jira | Pendiente | Cada hito requiere registro propio |

## 3. Verificación técnica y aceptación

| Prueba | Fecha, ambiente y versión | Resultado y evidencia |
| --- | --- | --- |
| Lint y suite completa de la rama combinada con HU-PRE-05 | 2026-11-19, local, `e1fda3a` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.357 passed y 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-04/2026-11-19-verificacion-local.md). CI del PR, pendiente de concluir. |

| Criterio de [STK-262](https://philippem.atlassian.net/browse/STK-262) | Evidencia técnica | Conclusión humana |
| --- | --- | --- |
| CA1 producto y sin movimiento | Tests contractuales y consulta WhatsApp simulada con lista; no se persiste movimiento | Pendiente |
| CA2 canasta y faltantes | Tests de comparación por producto y faltantes explícitos | Pendiente |
| CA3 caché y vigencia | Tests de clave por snapshot/consulta/fecha y TTL | Pendiente |
| CA4 degradación controlada | Tests de fallo de carga y preservación de snapshot; cobertura parcial informada | Pendiente |
| CA5 sin resultados confiables | Tests sin precios inventados y respuesta de indisponibilidad | Pendiente |

Demo humana: cargar un snapshot real en staging, consultar producto/canasta desde WhatsApp, verificar fecha, sucursal, precio, faltantes y ausencia de movimientos. Definition of Done: pendientes revisión/CI, carga real, medición, merge, despliegue y conclusión explícita de la persona designada.

## 4. Seguimiento durante el sprint

2026-11-19: implementación y pruebas locales; siguiente paso: revisar CI y capacidad de una ingesta real. Sprint y burndown: Pendiente de identificación; no se contabilizan 8 SP como aceptados.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. La HU sigue en curso.

## 6. Sprint Retrospective

Pendiente.

## 7. Traslado a la planificación

Pendiente. Conservar 8 SP y registrar por separado implementación, CI, merge, despliegue y aceptación.
