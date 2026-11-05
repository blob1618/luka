# Documentación de incrementos

El agente que desarrolla el cambio sigue `AGENTS.md`. El autor revisa los datos y las evidencias antes de incluirlos en el PR. Cada HU tiene una ficha, incluso si requiere varios PR. Usá una ficha técnica cuando el trabajo carezca de HU.

## Al empezar

Buscá la HU o tarea en esta carpeta. Si ya existe una ficha, reutilizá su nombre y carpeta de evidencias. Si todavía falta, desde la raíz y con el entorno de Python activo ejecutá, por ejemplo:

```bash
python scripts/incremento.py iniciar sprint-11-hu-reg-03 --titulo "Registrar compras con una foto" --jira "STK-123"
```

El comando genera `docs/increments/sprint-11-hu-reg-03.md` desde la [plantilla](plantilla-incremento.md) con los datos de inicio y un enlace a la carpeta de evidencias. Para un incremento técnico usá `sprint-11-tecnico-<tema>`. Si el sprint aún no está confirmado, usá `sprint-pendiente-...` y completá el número en la ficha cuando se confirme. El comando conserva cualquier ficha existente.

## Durante el desarrollo

Completá la ficha con el alcance, las decisiones, los avances y los resultados que puedas respaldar. Ejecutá los controles aplicables a través del script:

```bash
python scripts/incremento.py prueba sprint-11-hu-reg-03 unit
python scripts/incremento.py prueba sprint-11-hu-reg-03 lint
```

Los controles disponibles son `unit`, `webhook`, `suite` y `lint`. Cada ejecución deja un JSON nuevo, incluso si falla, en `docs/increments/evidencias/<ficha>/`. El registro incluye fecha UTC, comando, Python, duración, código de salida, commit base y presencia de cambios locales. Si había cambios locales, usá la ejecución de CI como evidencia del SHA publicado.

Para una demo, prueba manual o decisión, guardá un archivo Markdown corto en esa carpeta. Incluí fecha, ambiente y versión, criterio, pasos, resultado observado y enlace al registro original si existe. Los archivos versionados deben contener datos ficticios o anonimizados. La salida de los comandos queda en la terminal. Mantené las capturas con datos personales, los adjuntos reales, los secretos y los logs crudos fuera del repositorio.

## En el PR y al cierre

Incluí el código, la ficha actualizada y sus evidencias en la misma rama y PR. Enlazá la ficha desde la descripción. La comprobación **Verificar ficha y evidencias** pide una ficha nueva o actualizada y un registro nuevo en su carpeta para los PR hacia `main` que afecten el código, prompts, configuración de ejecución, migraciones o simulador. Los PR de documentación pueden pasar sin ficha. El check verifica la estructura y el vínculo, y el revisor evalúa el contenido.

El autor incorpora los enlaces a CI, despliegue y aceptación a medida que se conocen. Quien integra verifica Render. La persona designada deja su conclusión explícita de aceptación en Jira o en el PR. Si la evidencia aparece después del merge, enlazala en el PR original y actualizá la ficha con un PR documental. Usá «Pendiente» cuando falte una comprobación y completá el traslado a la planificación con las evidencias disponibles.
