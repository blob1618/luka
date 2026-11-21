# Ficha de incremento — HU-PRE-05 · Ofertas de canasta a pedido

[Evidencias versionadas](evidencias/sprint-pendiente-hu-pre-05/)

## 1. Alcance y compromiso

- Historia y valor: [STK-263 / HU-PRE-05](https://philippem.atlassian.net/browse/STK-263), permitir consultar ofertas de la canasta frecuente cuando la persona lo pida, sin depender del presupuesto. La versión original incluía un ofrecimiento automático al cruzar el 80 %; se retiró por decisión de producto del 21/11/2026. Estimación original: 5 SP.
- Autor de implementación: blob1618. Persona designada para validar, release, sprint, Sprint Goal, capacidad, priorización y fecha de Planning: Pendiente de confirmar en la planificación.
- Dependencia funcional actual: comparador [STK-262 / PR #123](https://github.com/blob1618/luka/pull/123). La alerta HU-PRE-02 ya no es una dependencia. La implementación inicial se revisó en [PR #124](https://github.com/blob1618/luka/pull/124).
- Estado: IN REVIEW en Jira al 21/11/2026, sin aceptación humana de la versión revisada.

## 2. Implementación y operación

- El usuario agrega o reemplaza productos de su canasta por nombre, cantidad y ubicación mediante propuesta completa y confirmación posterior. Si la búsqueda encuentra varias presentaciones, elige por número; el código de producto es interno. Puede verla o consultar ofertas sin haber cruzado el 80 %.
- El ajuste de experiencia de uso está en [PR #125](https://github.com/blob1618/luka/pull/125), con [evidencia local](evidencias/sprint-pendiente-hu-pre-05/2026-11-19-canasta-por-nombre.md). La prueba real comunicada por la persona usuaria mostró el pedido indebido de EAN y la ausencia del catálogo SEPA en el entorno; esta corrección todavía requiere despliegue y validación.
- El ajuste posterior de selección muestra cinco presentaciones pertinentes ordenadas por el menor precio publicado del paquete, con comercio y sucursal. El catálogo SEPA carece de categoría: un filtro semántico con el LLM existente elige sólo nombres presentes en el catálogo y pide precisión si no puede distinguirlos; el backend calcula los precios y conserva la confirmación separada. [Evidencia local](evidencias/sprint-pendiente-hu-pre-05/2026-11-20-opciones-con-precio.md). [PR #136](https://github.com/blob1618/luka/pull/136) integrado y [Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/deploys/dep-dc0bsv142hec73f44h2g) publicado el 20/11/2026; aceptación humana pendiente.
- El 21/11/2026 se corrigió el pedido mixto «agregar a mi canasta» + «mostrar opciones con precio»: el agente debe abrir la operación de canasta, no la consulta de precios. Las ambigüedades del comparador se presentan en renglones numerados. [PR #137](https://github.com/blob1618/luka/pull/137) integrado y [Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/deploys/dep-dc0qrh5g1s2s73ebib8g) publicado; la prueba real de la persona usuaria detectó problemas adicionales de formato y de la distinción entre precios y ofertas.
- Tras la prueba real del 21/11/2026, la propuesta y la canasta se muestran como listas; al consultar la canasta aparece el menor precio publicado y su sucursal con fecha, aclarando que no registra una compra. «Comparar precios» consulta precios por sucursal y «Consultar ofertas» queda para promociones verificadas. [Evidencia de esta corrección](evidencias/sprint-pendiente-hu-pre-05/2026-11-21-canasta-precios-y-formato.md). Aceptación humana de esta versión: pendiente.
- La consulta de ofertas se inicia por texto o desde el botón «Consultar ofertas» al mostrar la canasta. Usa los precios de HU-PRE-04; si no hay promociones utilizables, lo informa sin inventar precios ni ahorros. La alerta del 80 % queda exclusivamente financiera. [Decisión y verificación](evidencias/sprint-pendiente-hu-pre-05/2026-11-21-ofertas-a-pedido.md).
- Modelos y [migración versionada](../../supabase/migrations/20261119193000_shopping_basket.sql) para la canasta. El flujo usa la entrega y la confirmación del runtime existente. [Guía de operación](../supermarket-prices.md). No se ejecutó migración en Supabase/Render.
- Riesgos: depende de una carga de SEPA completa y fresca, de la integración de PR #123 y de prueba en WhatsApp real. Sin canasta guardada no hay ofertas personalizadas. La consulta informativa no garantiza stock ni precio de góndola.

| Hito | Fecha | Evidencia |
| --- | --- | --- |
| Inicio e implementación local | 2026-11-19 | Commit `d12b970` |
| Lista para revisión | 2026-11-19 | [PR #124](https://github.com/blob1618/luka/pull/124), borrador |
| Revisión, merge, despliegue, aceptación y cierre Jira | Pendiente | Cada hito requiere registro propio |

## 3. Verificación técnica y aceptación

| Prueba | Fecha, ambiente y versión | Resultado y evidencia |
| --- | --- | --- |
| Lint y suite completa de la rama combinada | 2026-11-19, local, `a6e58ce` | `ruff check .` sin hallazgos; `python -m pytest -v`: 1.358 passed, 2 warnings de dependencias. [Registro](evidencias/sprint-pendiente-hu-pre-05/2026-11-19-verificacion-local.md). |
| Backend y migraciones en CI | 2026-11-19, GitHub Actions, `36e5de4` antes del rebase documental | [CI Pipeline aprobado](https://github.com/blob1618/luka/actions/runs/44649171510). No acredita entorno productivo ni WhatsApp real. |

| Criterio de [STK-263](https://philippem.atlassian.net/browse/STK-263) | Evidencia técnica | Conclusión humana |
| --- | --- | --- |
| CA1 consulta de ofertas a pedido, sin presupuesto | Test de herramienta con canasta guardada y ningún límite | Pendiente |
| CA2 acceso desde la canasta y por texto | Test de botones independientes y herramienta de ofertas | Pendiente |
| CA3 resultados verificables y sin mutaciones | Tests de lectura de ofertas y comparador de HU-PRE-04 | Pendiente |
| CA4 sin ofertas | Tests de mensaje explícito sin comparativas inventadas | Pendiente |
| Alerta del 80 % exclusivamente financiera | Test del evento con canasta guardada y sin botón de ofertas | Pendiente |

Demo humana: guardar canasta, pedir ofertas por texto, mostrar la canasta y pulsar «Consultar ofertas» sin presupuesto; repetir con snapshot sin ofertas. En una prueba separada, cruzar el 80 % y verificar que sólo llegue la alerta financiera. Definition of Done: pendientes revisión humana, ingesta real, merge, despliegue y conclusión explícita de la persona designada.

## 4. Seguimiento durante el sprint

2026-11-19: implementación y pruebas locales; siguiente paso: revisar CI y flujo real en WhatsApp. Sprint y burndown: Pendiente de identificación; no se contabilizan 5 SP como aceptados.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. La HU sigue en curso.

## 6. Sprint Retrospective

Pendiente.

## 7. Traslado a la planificación

Pendiente. Conservar 5 SP y registrar por separado implementación, CI, merge, despliegue y aceptación.
