# Ficha de incremento — Registro versionado de evidencias

[Evidencias versionadas](evidencias/sprint-pendiente-tecnico-evidencias/)

## 1. Alcance y compromiso

Trabajo técnico transversal para que los agentes documenten cada incremento durante su desarrollo y versionen la evidencia en el mismo PR. La solicitud se registró el 2026-11-05. Release, sprint, Jira y planificación de capacidad quedan pendientes de asignación por el equipo. No aplica estimación en SP de HU.

## 2. Implementación y operación

- `AGENTS.md` centraliza el flujo. Copilot, Claude Code y Gemini CLI reciben instrucciones de repositorio que apuntan al mismo archivo.
- `scripts/incremento.py` genera una ficha sin sobrescribir una existente y registra cada ejecución de controles locales como JSON separado. Conserva el código de salida y deja los logs en la terminal.
- GitHub Actions comprueba en los PR con cambios del sistema que haya una ficha actualizada y evidencia nueva asociada. El check usa permisos de lectura.
- El primer control unitario local quedó registrado. El entorno de Python 3.14 carecía de `cryptography` para cargar la configuración general de pruebas. Las regresiones específicas se ejecutaron sin esa configuración. CI ejecuta la suite completa con Python 3.11.

## 3. Verificación técnica y aceptación

| Caso | Comprobación |
| --- | --- |
| Inicialización repetida | La prueba verifica la generación y la conservación de una ficha ya editada. |
| Control fallido | La prueba verifica el registro JSON y la propagación del código de salida. |
| Trazabilidad del PR | La regresión cubre un PR documental, un cambio sin evidencia, una evidencia vieja modificada y una ficha con evidencia nueva. |
| Resultados locales | [Seis regresiones y prueba del check con Git](evidencias/sprint-pendiente-tecnico-evidencias/2026-11-05-verificacion-local.md). Ruff y el intento unitario completo figuran en el registro automático. |
| CI, merge y Render | Pendiente de verificación en el PR. |
| Validación humana | Pendiente de una conclusión explícita del equipo. |

## 4. Seguimiento durante el sprint

El 2026-11-05 se implementó el flujo, se ejercitó el registro ante fallos y se prepararon los controles de regresión. Burndown compartido pendiente de vincular si el equipo asigna el trabajo a un sprint.

## 5. Review, cierre y retrospectiva

Pendiente de revisión por el equipo. La ficha queda disponible como ejemplo para futuros incrementos.

## 6. Traslado a la planificación

Pendiente de la decisión del equipo sobre dónde registrar este habilitador técnico.
