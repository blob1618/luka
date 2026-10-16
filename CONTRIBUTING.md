# Contributing to LUKA

Guia practica del flujo actual del equipo.

## Flujo minimo

1. Tomar una tarea desde Jira.
2. Desde la extension de Jira en VS Code, crear o tomar la rama asociada al ticket.
   La rama debe incluir la clave del ticket de Jira para que la integracion pueda vincular el trabajo.
3. Desarrollar el cambio en esa rama.
4. Subir los cambios a GitHub.
5. Abrir un Pull Request hacia `main` para la historia de usuario y completar la plantilla de `.github/pull_request_template.md` con la evidencia disponible.
6. Esperar las pruebas automaticas de GitHub Actions cuando apliquen y la revision del PR.
7. Integrar a `main` segun el acuerdo del equipo.
8. Render deploya `main` automaticamente.
9. Probar el cambio real en WhatsApp.
10. Si falla, revisar logs en Render, corregir y volver a subir.
11. Verificar que Jira haya actualizado el estado del ticket. Si no se actualiza automaticamente, moverlo manualmente.

El autor del PR mantiene la descripcion actualizada, incluso si la validacion ocurre despues del merge. La persona designada para validar la historia deja su aceptacion o rechazo de los criterios, con fecha y evidencia, en el PR o en Jira; el autor enlaza ese registro. Quien integra a `main` verifica el despliegue de Render y aporta su fecha y evidencia al autor.

## Importante

- Para las historias de usuario, trabajar en una rama asociada a la tarea de Jira y abrir un PR antes de integrar a `main`.
- `main` es la rama que se despliega y se prueba contra Meta/WhatsApp.
- La configuracion actual de GitHub Actions corre en pushes a cualquier rama y en Pull Requests a `main`.
- Las pruebas reales de WhatsApp dependen del numero configurado en Meta y de la base de datos compartida.
- No subir secretos al repo.
- Si aparece una variable nueva, agregarla a `.env.example`.

La guía técnica de desarrollo (setup, tests y deploy) vive en `docs/development.md`.
