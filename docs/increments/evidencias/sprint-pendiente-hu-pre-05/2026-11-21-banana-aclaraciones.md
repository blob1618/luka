# Banana fresca: aclaraciones y búsqueda de catálogo

Fecha: 21/11/2026. Historia: [STK-263 / HU-PRE-05](https://philippem.atlassian.net/browse/STK-263).

La persona usuaria comunicó una prueba en WhatsApp real: «quiero agregar banana a mi canasta» recibió «No puedo distinguir con seguridad»; «banana para comer broo quiero una docena» recibió la misma respuesta; «BANANA de la naturaleza, de las granjas, las ecuatorianas» terminó en «No encontré «banana de la naturaleza, de las granjas, ecuatoriana, para comer» en Resistencia».

El código anterior exigía que todas las palabras de la aclaración aparecieran en la descripción SEPA. Además, el filtro semántico trataba la ausencia de candidatos pertinentes igual que una duda sobre la intención. La corrección busca progresivamente prefijos más generales cuando la descripción completa no aparece, luego evalúa la pertinencia de las presentaciones con el LLM. Si no hay una opción pertinente, informa que no agregó un producto diferente. No codifica reglas particulares para banana.

Verificación local: test de integración con catálogo sintético que recupera «Banana fresca ecuatoriana» a partir de una aclaración larga; test con sólo «Arroz con leche listo» que no lo propone como arroz base ni guarda una canasta. `ruff check .` sin errores y `python -m pytest -v` con 1.373 tests aprobados y dos avisos de dependencias. Son pruebas automatizadas locales, no nueva verificación en WhatsApp real.

Límite: la ingesta y la canasta actuales aceptan sólo productos con EAN. No se inspeccionó Render ni se confirmó que el catálogo publicado para Resistencia contenga banana fresca con EAN. Aceptación humana y despliegue: pendientes.
