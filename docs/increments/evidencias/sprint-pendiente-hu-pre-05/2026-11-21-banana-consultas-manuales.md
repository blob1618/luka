# Consultas de banana en WhatsApp: límites observados

Fecha: 21/11/2026. Evidencia: transcripción comunicada por la persona usuaria; no se verificó independientemente el catálogo, los logs ni Render. Esta nota documenta el resultado observado, sin cambiar la implementación.

| Hora informada | Pedido | Respuesta observada | Alcance de la observación |
| --- | --- | --- | --- |
| 19:37 | «hola quiero añadir la fruta banana a mi canasta, compro en resistencia, a cuanto esta el menor precio ?» | «No encontré una presentación de «banana fruta» que corresponda a tu pedido en el catálogo de Resistencia. No agregué otro producto a tu canasta.» | La operación de canasta no ofreció una opción de fruta fresca ni la guardó. No demuestra ausencia de banana en toda la localidad o en todo SEPA. |
| 19:46 | «hola quiero saber a cuanto estan las bananas» | El comparador listó cinco nombres: alimento Nestum con banana, avena con banana y barras sabor banana; pidió marca o presentación. | La consulta informativa mostró coincidencias textuales de derivados, sin precios de banana fresca. |

La ruta de canasta aplica un filtro semántico a candidatos del catálogo; la consulta informativa de precios enumera coincidencias textuales cuando hay ambigüedad. Por eso las dos respuestas difieren. [PR #139](https://github.com/blob1618/luka/pull/139) quedó integrado a `main` el 21/11/2026 para evitar aclaraciones repetidas, pero esta prueba posterior muestra que no resuelve la búsqueda de banana fresca. Queda pendiente una decisión e implementación futura si se desea cubrir productos frescos sin una presentación identificable en SEPA y unificar la relevancia de ambas rutas.
