# Verificación local

- **Fecha y entorno** 2026-11-05, Windows, Python 3.14.7. Cambios del incremento sobre el commit base `93a644a8e716b76a13206c34fc3a2e6084bc39bd`.
- **Regresión reproducible** `python -m pytest --noconftest -q tests/test_increment_evidence.py`. Resultado observado, 6 pruebas aprobadas.
- **Prueba con Git** En un repositorio temporal, ejecutar `python scripts/incremento.py verificar <base> HEAD` sobre tres estados consecutivos. El caso documental devolvió 0. El cambio en `app/` sin ficha devolvió 1. Ese mismo cambio con una ficha y un Markdown nuevo en su carpeta de evidencias devolvió 0.
- **Intento de suite unitaria** El entorno local carecía de `cryptography` y la configuración general de Pytest se detuvo al importar la aplicación. El intento y el lint quedaron registrados en los archivos JSON de esta carpeta.
