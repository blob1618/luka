# Ficha de incremento — HU-PRE-05 · Ofertas vinculadas a alertas de presupuesto

[Evidencias versionadas](evidencias/sprint-pendiente-hu-pre-05/)

## 1. Alcance y compromiso

- Historia y valor: [STK-263 / HU-PRE-05](https://philippem.atlassian.net/browse/STK-263), permitir consultar ofertas de la canasta frecuente al cruzar el 80 % del presupuesto de alimentos o a pedido. Estimación acordada: 5 SP.
- Autor de implementación: blob1618. Persona designada para validar, release, sprint, Sprint Goal, capacidad, priorización y fecha de Planning: Pendiente de confirmar en la planificación.
- Dependencias: alerta existente de HU-PRE-02 y comparador [STK-262 / PR #123](https://github.com/blob1618/luka/pull/123). Este incremento se revisa en [PR #124](https://github.com/blob1618/luka/pull/124), con base en la rama de PR #123.
- Estado: En curso en Jira desde 2026-11-19, sin aceptación humana.

## 2. Implementación y operación

- El usuario agrega o reemplaza productos de su canasta por nombre, cantidad y ubicación mediante propuesta completa y confirmación posterior. Si la búsqueda encuentra varias presentaciones, elige por número; el código de producto es interno. Puede verla o consultar ofertas sin haber cruzado el 80 %.
- El ajuste de experiencia de uso está en [PR #125](https://github.com/blob1618/luka/pull/125), con [evidencia local](evidencias/sprint-pendiente-hu-pre-05/2026-11-19-canasta-por-nombre.md). La prueba real comunicada por la persona usuaria mostró el pedido indebido de EAN y la ausencia del catálogo SEPA en el entorno; esta corrección todavía requiere despliegue y validación.
- El ajuste posterior de selección muestra cinco presentaciones pertinentes ordenadas por el menor precio publicado del paquete, con comercio y sucursal. El catálogo SEPA carece de categoría: un filtro semántico con el LLM existente elige sólo nombres presentes en el catálogo y pide precisión si no puede distinguirlos; el backend calcula los precios y conserva la confirmación separada. [Evidencia local](evidencias/sprint-pendiente-hu-pre-05/2026-11-20-opciones-con-precio.md). [PR #136](https://github.com/blob1618/luka/pull/136) integrado y [Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/deploys/dep-dc0bsv142hec73f44h2g) publicado el 20/11/2026; aceptación humana pendiente.
- El 21/11/2026 se corrigió el pedido mixto «agregar a mi canasta» + «mostrar opciones con precio»: el agente debe abrir la operación de canasta, no la consulta de precios. Las ambigüedades del comparador se presentan en renglones numerados. [PR #137](https://github.com/blob1618/luka/pull/137) integrado y [Render](https://dashboard.render.com/web/srv-d9656eeq1p3s73bgl210/deploys/dep-dc0qrh5g1s2s73ebib8g) publicado; la prueba real de la persona usuaria detectó problemas adicionales de formato y de la distinción entre precios y ofertas.
- Tras la prueba real del 21/11/2026, la propuesta y la canasta se muestran como listas; al consultar la canasta aparece el menor precio publicado y su sucursal con fecha, aclarando que no registra una compra. «Comparar precios» consulta precios por sucursal y «Consultar ofertas» queda para promociones verificadas. [Evidencia de esta corrección](evidencias/sprint-pendiente-hu-pre-05/2026-11-21-canasta-precios-y-formato.md). Aceptación humana de esta versión: pendiente.
- Un pedido real de banana fresca repitió una pregunta de aclaración y terminó buscando literalmente una descripción larga. La búsqueda ahora retrocede a términos más generales cuando la descripción completa no figura en SEPA; el filtro semántico decide cuáles presentaciones corresponden. Si ninguna corresponde, Luka informa que no agregó el producto en vez de repetir la aclaración. Esto no demuestra que haya banana fresca disponible en el catálogo de Resistencia ni resuelve productos sin EAN; verificación humana pendiente. [Evidencia](evidencias/sprint-pendiente-hu-pre-05/2026-11-21-banana-aclaraciones.md).
- Un gasto confirmado que cruza el 80 % de un presupuesto de alimentos agrega «Consultar ofertas» a la alerta, una sola vez por disparo y sólo si existe canasta. El botón consulta el comparador de HU-PRE-04; si no hay promociones utilizables, lo informa sin inventar precios ni ahorros. Leer la canasta o pulsar el botón no cambia el límite, saldo ni movimientos.
- Modelos y [migración versionada](../../supabase/migrations/20261119193000_shopping_basket.sql) para la canasta. El flujo usa la entrega y la confirmación del runtime existente. [Guía de operación](../supermarket-prices.md). No se ejecutó migración en Supabase/Render.
- Riesgos: depende de una carga de SEPA completa y fresca, de la integración de PR #123 y de prueba en WhatsApp real. Sin canasta no se ofrece el botón. La consulta informativa no garantiza stock ni precio de góndola.

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
| CA1 botón al cruzar 80 % y presupuesto intacto | Tests del evento de gasto confirmado y botón suplementario condicionado a canasta | Pendiente |
| CA2 consulta a pedido | Tests de herramienta de canasta sin alerta previa | Pendiente |
| CA3 comparación tras pulsar, sin mutaciones | Tests de acción y lectura del comparador de HU-PRE-04 | Pendiente |
| CA4 sin ofertas | Tests de mensaje explícito sin comparativas inventadas | Pendiente |

Demo humana: guardar canasta, cruzar 80 % con gasto confirmado, pulsar botón y consultar la canasta a pedido; repetir con snapshot sin ofertas. Definition of Done: pendientes revisión humana, dependencia PR #123, ingesta real, merge, despliegue y conclusión explícita de la persona designada.

## 4. Seguimiento durante el sprint

2026-11-19: implementación y pruebas locales; siguiente paso: revisar CI y flujo real en WhatsApp. Sprint y burndown: Pendiente de identificación; no se contabilizan 5 SP como aceptados.

## 5. Sprint Review, cierre y replanificación

Pendiente de Review y decisión humana. La HU sigue en curso.

## 6. Sprint Retrospective

Pendiente.

## 7. Traslado a la planificación

Pendiente. Conservar 5 SP y registrar por separado implementación, CI, merge, despliegue y aceptación.
