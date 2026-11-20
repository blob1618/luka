# Seguimiento de HU-PRE-04: búsqueda ambigua sin pedir códigos

- Fecha: 2026-11-19. Rama: `codex/stk-263-canasta-por-nombre`; PR #125.
- Cambio: una consulta por nombre con varias coincidencias muestra descripciones y pide marca o presentación. El mensaje ya no exige EAN; el comparador sigue usando el identificador de producto internamente para evitar mezclar presentaciones.
- Verificación local: prueba de comparación ambigua actualizada; `ruff check .` sin hallazgos y `python -m pytest -q` con 1.361 pruebas aprobadas, dos avisos de dependencias. Los datos son contractuales, no una carga real de SEPA.
- Pendiente: catálogo real, despliegue, prueba en WhatsApp y aceptación explícita de la persona designada.
