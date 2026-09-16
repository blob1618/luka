# Operación de flujos conversacionales

Guía de despliegue, publicación, validación y recuperación para
`HU-FLU-01` / `STK-168`.

## Regla principal

Los flujos configurables personalizan la presentación de un resultado emitido
por el backend. No son un menú principal. Un usuario puede escribir `/link`,
registrar un gasto, consultar movimientos o iniciar cualquier otra operación
soportada en cualquier momento. Cuando llega texto libre, Luka abandona sólo el
recorrido visual pendiente y vuelve a ejecutar el dispatcher general.

## Configuración por entorno

### Backend `luka`

| Variable | Uso |
| --- | --- |
| `FLOW_ADMIN_API_KEY` | Credencial interna que protege la API administrativa. Debe ser larga, aleatoria y coincidir con el frontend. |
| `WHATSAPP_GRAPH_API_VERSION` | Versión de Graph API usada para mensajes salientes. El valor por defecto actual es `v26.0`. |
| `REDIS_URL` | Estado fijado de flujo, versión y nodo para recorridos interactivos. |
| `DATABASE_URL` | Persistencia de flujos y versiones en PostgreSQL/Supabase. |

### Servidor `luka_frontend`

| Variable | Uso |
| --- | --- |
| `LUKA_BACKEND_URL` | URL base del backend, sin ruta administrativa. |
| `FLOW_ADMIN_API_KEY` | Misma credencial interna del backend. Nunca se entrega al navegador. |
| `FLOW_ADMIN_AUTH_USER_IDS` | IDs `auth_user_id` autorizados, separados por comas. Una sesión válida que no esté en esta lista recibe 403. |

No guardar valores reales en Git, Jira, logs ni capturas. El frontend no debe
consultar las tablas de flujos ni ninguna tabla financiera directamente.

## Orden de despliegue

1. Integrar y desplegar primero la rama del backend.
2. Aplicar la migración
   `supabase/migrations/20260916174000_add_conversation_flows.sql` mediante el
   flujo de migraciones del repositorio.
3. Verificar en el entorno remoto que el timestamp `20260916174000` figure como
   aplicado y que existan `public.conversation_flow` y
   `public.conversation_flow_version` con RLS habilitado y sin policies públicas.
4. Configurar `FLOW_ADMIN_API_KEY` y `WHATSAPP_GRAPH_API_VERSION` en el backend.
5. Configurar `LUKA_BACKEND_URL`, la misma `FLOW_ADMIN_API_KEY` y
   `FLOW_ADMIN_AUTH_USER_IDS` en `luka_frontend`.
6. Desplegar el frontend y confirmar que un administrador ve **Flujos**, mientras
   que un usuario autenticado no autorizado recibe 403 y no ve el acceso.

La presencia de la migración en Git no demuestra su aplicación remota. La
verificación debe realizarse después del despliegue con `supabase migration
list` en un checkout vinculado y una consulta de metadatos autorizada.

## Publicar desde el panel

1. Abrir **Flujos** en `luka_frontend`.
2. Crear un flujo o abrir uno existente.
3. Elegir un `event_key`. El backend entrega las variables y acciones válidas;
   el panel no permite escribir acciones arbitrarias.
4. Definir el nodo inicial y los mensajes de texto, botones o listas.
5. Usar **Validar**. Corregir todos los errores antes de continuar.
6. Usar **Guardar borrador**. El borrador todavía no afecta conversaciones.
7. Usar **Publicar**. Las conversaciones nuevas usarán la nueva versión.

Una conversación que ya mostró botones o una lista conserva su versión fijada
en Redis. Publicar otra versión no cambia las opciones que ese usuario ya vio.

Los eventos terminales, incluidos `/link`, onboarding y confirmaciones finales
de movimientos, sólo admiten un nodo de texto. Así no pueden dejar al usuario
atrapado en un recorrido después de recibir el resultado.

## Retiro y recuperación

- **Error en un borrador:** descartarlo. La versión publicada sigue activa.
- **Error detectado después de publicar:** abrir el flujo, corregir sobre la
  versión publicada, guardar un nuevo borrador y publicar una versión superior.
- **Contenido que debe dejar de usarse inmediatamente para conversaciones
  nuevas:** retirar el flujo. El dispatcher conserva su respuesta segura por
  defecto.
- **Backend administrativo no disponible:** el panel muestra el error y no
  escribe directamente en la base.
- **Tabla o definición no disponible en runtime:** Luka conserva la respuesta
  segura del dispatcher cuando puede hacerlo.
- **Redis no disponible o respuesta interactiva vencida:** no se ejecuta la
  acción y se solicita al usuario que vuelva a expresar qué quiere hacer.

## Validación controlada en WhatsApp

Realizar esta prueba únicamente con un número autorizado y un entorno donde la
migración y las variables anteriores estén confirmadas.

### 1. Texto terminal y routing libre

1. Publicar una presentación de texto para `dashboard.link.sent`.
2. Enviar `/link` desde el número controlado.
3. Confirmar que llega un único enlace válido y que no queda interacción
   pendiente.
4. Enviar inmediatamente `hoy gasté 3000 pesos en agua`.
5. Confirmar que el movimiento se guarda una sola vez y recibe su respuesta
   financiera normal/configurada, sin pedir volver a un menú.

### 2. Reply buttons

1. Publicar `category.confirmation_required` con botones cuyas acciones sean
   únicamente las ofrecidas por el panel.
2. Provocar la confirmación de una categoría con un movimiento controlado.
3. Pulsar el botón de confirmación.
4. Confirmar una sola persistencia, una sola respuesta visible y cierre del
   estado pendiente.

### 3. Lista y abandono libre

1. Publicar una nueva versión del mismo evento usando una lista.
2. Iniciar el recorrido y comprobar que la lista llega con títulos y filas.
3. En una ejecución, seleccionar una fila válida y confirmar la transición.
4. En otra ejecución, no seleccionar la lista: enviar `/link` u otra operación
   libre y confirmar que el dispatcher procesa la nueva operación normalmente.

### 4. Seguridad e idempotencia

1. Confirmar en logs el mismo `whatsapp_message_id` durante claim y completed.
2. Ante un reintento de Meta con el mismo ID, comprobar que aparece como
   `duplicate` y que no existe una segunda escritura ni una segunda respuesta.
3. No fabricar ni modificar IDs en producción. La cobertura automatizada
   verifica que un ID desconocido o manipulado no ejecuta acciones.

## Evidencia para Jira

Registrar sin secretos ni datos financieros sensibles:

- fecha, entorno y commits desplegados de ambos repositorios;
- ejecución de CI y cantidad de pruebas aprobadas;
- timestamp de migración confirmado en remoto;
- evento, tipo de mensaje y resultado de cada caso controlado;
- IDs de mensajes parcialmente enmascarados, si hacen falta para correlación;
- confirmación de una única escritura y una única respuesta;
- resultado de `/link` seguido por una operación libre;
- cualquier limitación o paso pendiente.

STK-176 y la historia STK-168 sólo pueden pasar a Done después de adjuntar esa
evidencia real y confirmar el estado remoto de la migración.
