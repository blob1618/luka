# Verificación local de HU-PRE-04

- Fecha: 2026-11-19; ambiente: worktree local de Luka, Python 3.11; versión probada: `a6e58ce` (incluye HU-PRE-04 y la rama dependiente HU-PRE-05).
- Criterios: CA1–CA5 de STK-262, en el alcance automatizado.
- Pasos: `ruff check .` y `python -m pytest -v` en el worktree con pruebas aisladas; datos de SEPA representados por ZIP/CSV contractuales pequeños. No hubo descarga ni carga del ZIP completo.
- Resultado observado: lint correcto y 1.358 pruebas aprobadas; dos warnings preexistentes de Starlette/AnyIO. La consulta de producto desde el flujo WhatsApp simulado devuelve lista y no registra movimiento. Los tests prueban producto, canasta, caché, fallo de carga, búsqueda corta y ausencia de resultados.
- Límites: no hay prueba de carga real, capacidad de base, licencia, migración aplicada, entrega por WhatsApp real ni aceptación humana. La ejecución original quedó en la terminal local; [CI publicado](https://github.com/blob1618/luka/actions/runs/44648812473) verifica el SHA del comparador.
