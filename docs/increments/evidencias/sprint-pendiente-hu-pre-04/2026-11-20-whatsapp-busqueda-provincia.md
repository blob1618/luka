# HU-PRE-04 — Búsqueda por nombre en Resistencia, 20/11/2026

## Rechazo observado en WhatsApp

La persona usuaria aportó una conversación real del 20/11/2026, 14:03–14:05: pidió «Rex Traviata» para Resistencia, Chaco; Luka respondió que no lo encontraba. Tampoco encontró «galletitas Traviata» ni «arroz», y la consulta de arroz informó «sin precio encontrado en esa localidad». Esto rechaza la búsqueda por nombre en ese recorrido; la carga de precios por sí sola no demuestra que la HU funcione por WhatsApp.

## Diagnóstico y corrección

- El snapshot productivo del 19/11 contiene 43.142 precios AR-H. La inspección de la tabla de producción ya había mostrado 24 filas de Traviata con localidad «Resistencia».
- Una copia local del mismo archivo contiene 27 filas de Traviata y 537 de arroz si la localidad se compara sin distinguir mayúsculas. Con provincia literal «Chaco», la consulta existente devuelve cero, porque SEPA guarda «AR-H».
- La búsqueda ahora convierte «Chaco» a «AR-H» antes de filtrar, tanto para consultas como para propuestas de canasta. La canasta guarda el código normalizado, para que la elección y la consulta posterior usen el mismo lugar.
- Si una búsqueda por nombre tiene productos cuya descripción comienza con la palabra buscada, prioriza ese conjunto. Así «arroz» ofrece presentaciones de arroz antes que alfajores de arroz; «Rex Traviata» sigue encontrando las descripciones que contienen ambas palabras.

## Verificación

- Tests de integración con localidad Resistencia y provincia «Chaco»: «Rex Traviata» encuentra el producto, «arroz» no presenta alfajores, y una canasta de arroz ofrece opciones por nombre, no persiste antes de confirmar y guarda la provincia como «AR-H» tras la confirmación sintética.
- Copia SQLite del archivo real: «rex traviata» devuelve dos presentaciones y «arroz» devuelve primero cinco descripciones que comienzan por «ARROZ». La copia local no prueba la consulta PostgreSQL productiva.
- `ruff check .`: sin hallazgos. `python -m pytest -q -m unit`: 203 aprobadas. `python -m pytest -v`: 1.372 aprobadas y dos advertencias de dependencias, en la rama de corrección.
- Despliegue de Render y nueva verificación por WhatsApp real: pendientes al redactar este registro.

La conclusión humana de aceptación sigue pendiente. La conversación citada arriba es evidencia de fallo, no de aceptación.
