# Piloto de ingesta con el archivo SEPA real

- Fecha: 2026-11-20. Ambiente: worktree local aislado de Luka, Python 3.11 y SQLite local. Versión: `0e53e13` de [PR #127](https://github.com/blob1618/luka/pull/127).
- Fuente: [catálogo oficial SEPA/Precios Claros](https://datos.produccion.gob.ar/api/3/action/package_show?id=sepa-precios), archivo diario del 2026-11-19, ZIP de 336.008.466 bytes. El catálogo declara licencia CC-BY-4.0.
- Método: descarga del ZIP oficial completo y ejecución del ingestor con `SEPA_PROVINCES=AR-H` en una base SQLite desechable, fuera del repositorio. El ZIP incluye 18 archivos internos, uno de cero bytes; algunos CSV tienen filas con columnas ausentes. La corrección omite el ZIP vacío y descarta las filas inválidas sin interrumpir la publicación.
- Resultado observado: 43.142 filas de precios de AR-H publicadas; 83.110 filas descartadas durante el procesamiento; archivo SQLite de 22.335.488 bytes. Esta medida no representa el tamaño de PostgreSQL.
- Consulta de control: la búsqueda de «rex traviata» para Resistencia, AR-H, en la fecha 2026-11-19 devolvió dos opciones, «GALLETITAS CRACKERS REX x3, TRAVIATA, 288 gr» y «GALLETITAS CRACKERS SABOR REX, TRAVIATA, 96 gr». Es una desambiguación por nombre/presentación, sin pedir EAN al usuario.
- Verificación de código: `ruff check .` aprobado; `python -m pytest -v` finalizó con 1.363 pruebas aprobadas y 2 warnings de dependencias en 63,12 segundos.
- Límites: la prueba no ejecutó la ingesta en Render ni midió espacio/tiempo en PostgreSQL de Supabase. Tampoco confirma una respuesta real por WhatsApp ni la aceptación humana. Esos hitos siguen pendientes y requieren evidencia propia.
