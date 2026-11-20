# Verificación local de HU-PRE-05

- Fecha: 2026-11-19; ambiente: worktree local de Luka, Python 3.11; versión probada: `a6e58ce`.
- Criterios: CA1–CA4 de STK-263, en el alcance automatizado.
- Pasos: `ruff check .` y `python -m pytest -v` con pruebas aisladas; canasta y precios sintéticos.
- Resultado observado: lint correcto y 1.358 pruebas aprobadas, con dos warnings preexistentes de Starlette/AnyIO. Los tests específicos de `tests/test_shopping_basket.py` cubren canasta confirmada, consulta a pedido, botón de alerta y ausencia de ofertas.
- Límites: no hay prueba de carga real de SEPA, migración aplicada, entrega por WhatsApp real ni aceptación humana. La ejecución original quedó en la terminal local; [CI publicado](https://github.com/blob1618/luka/actions/runs/44649171510) aprobó backend y migraciones. Una ejecución anterior falló al ocupar otro trabajo el puerto 54322 del runner, no por la migración.
