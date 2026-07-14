# Contributing to LUKA

Guia practica del flujo actual del equipo.

## Flujo minimo

1. Tomar una tarea desde Jira.
2. Desde la extension de Jira en VS Code, crear o tomar la rama asociada al ticket.
   La rama debe incluir la clave del ticket de Jira para que la integracion pueda vincular el trabajo.
3. Desarrollar el cambio en esa rama.
4. Subir los cambios a GitHub.
5. Esperar las pruebas automaticas de GitHub Actions cuando apliquen.
6. Integrar a `main` segun el acuerdo del equipo.
7. Render deploya `main` automaticamente.
8. Probar el cambio real en WhatsApp.
9. Si falla, revisar logs en Render, corregir y volver a subir.
10. Verificar que Jira haya actualizado el estado del ticket. Si no se actualiza automaticamente, moverlo manualmente.

## Importante

- Por ahora no se usan Pull Requests como paso obligatorio.
- `main` es la rama que se despliega y se prueba contra Meta/WhatsApp.
- La configuracion actual de GitHub Actions corre en pushes a cualquier rama y en Pull Requests a `main`.
- Las pruebas reales de WhatsApp dependen del numero configurado en Meta y de la base de datos compartida.
- No subir secretos al repo.
- Si aparece una variable nueva, agregarla a `.env.example`.
