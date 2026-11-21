# Ofertas de canasta a pedido, separadas de la alerta presupuestaria

Fecha: 21/11/2026. Historia: [STK-263 / HU-PRE-05](https://philippem.atlassian.net/browse/STK-263).

Decisión comunicada por la persona usuaria: consultar ofertas y recibir una alerta al cruzar el 80 % son funcionalidades distintas. La consulta de ofertas debe estar disponible a pedido; la alerta presupuestaria no debe ofrecerla automáticamente.

La [historia en Jira](https://philippem.atlassian.net/browse/STK-263) se actualizó con el objetivo a pedido, criterios CA1–CA5 y la eliminación de HU-PRE-02 como dependencia. La edición de criterios no equivale a aceptación humana del resultado.

Cambio: la alerta del 80 % conserva su mensaje financiero y deja de agregar el botón de ofertas. Al mostrar una canasta guardada aparecen dos acciones independientes: «Comparar precios» y «Consultar ofertas». El pedido de ofertas por texto sigue disponible sin límite presupuestario. Ambas consultas son lecturas: no registran gastos ni cambian límites.

Verificación automatizada local: `tests/test_shopping_basket.py` cubre una alerta con canasta guardada que no adjunta ofertas, la consulta de ofertas sin ningún límite, los dos botones de la canasta y la ausencia de mutaciones. La prueba de ausencia de promociones sigue vigente. `ruff check .` no encontró problemas; `python -m pytest -v` aprobó 1.373 pruebas, con dos avisos de dependencias. Pendientes: prueba humana en WhatsApp y despliegue. No se inspeccionó Render por pedido de la persona usuaria.
