# Flujos conversacionales administrables

Contrato y operación de los mensajes y recorridos administrables. El panel de
`luka_frontend` administra; el backend `luka` valida, publica, resuelve y
renderiza sobre WhatsApp.

## Alcance y principios

La capacidad administrable modifica la presentación de resultados que el backend
ya sabe producir. No reemplaza al dispatcher, no introduce un menú principal y
no permite crear reglas de negocio desde el panel.

1. El usuario puede solicitar cualquier operación soportada en cualquier
   momento mediante lenguaje natural o comandos existentes como `/link`.
2. El dispatcher y los servicios de dominio conservan la autoridad para decidir
   la intención, validar datos, persistir cambios y determinar el resultado.
3. Un mensaje administrable se resuelve después de obtener un evento seguro del
   backend. El contenido nunca convierte un fallo en éxito ni confirma una
   operación antes del commit correspondiente.
4. Las acciones interactivas se seleccionan de una lista cerrada mantenida por
   el backend. El panel no puede registrar nombres de funciones, URLs internas
   ni expresiones ejecutables.
5. El frontend administra mediante la API protegida del backend. No consulta ni
   modifica estas tablas directamente en Supabase.

Flujo de decisión:

```text
mensaje entrante
  -> onboarding y comandos globales
  -> estado de dominio pendiente, si existe
  -> clasificación y normalización de intent
  -> servicio de dominio
  -> resultado seguro
  -> event_key + variables permitidas
  -> versión publicada del mensaje/recorrido
  -> texto, botones o lista para WhatsApp
```

La ausencia de una configuración publicada no altera el resultado de dominio: el
llamador conserva su respuesta segura actual hasta que ese evento tenga una
versión publicada.

Fuera de alcance:

- menú principal obligatorio;
- activación por frases reservadas como "menú" o "ayuda";
- creación de intents u operaciones financieras desde el panel;
- ejecución de Python, SQL, URLs internas o webhooks arbitrarios;
- acceso directo del frontend a las tablas administrables;
- constructor visual sin restricciones para reglas de negocio.

## Recurso administrable

Cada recurso tiene una identidad estable y versiones inmutables publicadas:

- `slug`: identificador administrativo estable y único.
- `name`: nombre legible para el panel.
- `event_key`: evento cerrado emitido por el backend; único por recurso.
- `status`: `active` o `archived`.
- `draft`: versión editable que todavía no afecta conversaciones.
- `published_version`: única versión efectiva para conversaciones nuevas.

Publicar crea una fotografía inmutable. Editar después de publicar genera o
modifica un nuevo borrador.

- Un recurso nuevo nace con un borrador inicial. Ese primer borrador no se
  descarta: si no se va a publicar, se retira el recurso completo.
- Con una versión publicada, el borrador sí se descarta sin afectar lo publicado.
- Publicar una versión nueva retira la anterior.
- Retirar (`archive`) impide iniciar recorridos nuevos y no admite nuevos
  borradores ni publicaciones. Una conversación ya iniciada puede terminar con
  la versión que fijó al comenzar.

Las tablas `public.conversation_flow` y `public.conversation_flow_version` se
crean en `supabase/migrations/20260916174000_add_conversation_flows.sql` y se
describen en `database.md`.

## Definición versionada

```json
{
  "start_node": "confirm",
  "nodes": [
    {
      "id": "confirm",
      "type": "reply_button",
      "body": "Todavía no existe la categoría {category}. ¿Querés crearla?",
      "footer": null,
      "terminal": false,
      "options": [
        {
          "id": "create",
          "title": "Crear categoría",
          "action": "confirm_limit_category",
          "next_node": null
        },
        {
          "id": "cancel",
          "title": "Cancelar",
          "action": "cancel_pending_operation",
          "next_node": null
        }
      ]
    }
  ]
}
```

### Tipos de nodo

- `text`: cuerpo de texto y finalización obligatoria del recorrido configurable.
- `reply_button`: cuerpo y opciones mostradas como botones de respuesta.
- `list`: cuerpo, etiqueta del botón de apertura, secciones y opciones.

Reglas:

- Una opción navega a `next_node` o emite una `action` permitida, pero no ambas.
- Un nodo terminal no declara opciones.
- Los eventos marcados como `terminal_only` (por ejemplo `/link`, onboarding y
  el cierre de movimientos) admiten un único nodo `text`, para no dejar al
  usuario atrapado en un recorrido después del resultado.

## Validación obligatoria antes de publicar

Antes de publicar, el backend comprueba:

- `event_key` habilitado en el registro cerrado del backend;
- `start_node` existe;
- identificadores de nodo y opción únicos;
- todas las transiciones apuntan a nodos existentes;
- todos los nodos son alcanzables desde el inicial;
- todas las ramas finalizan en un nodo de texto terminal o en una acción;
- las variables usadas están permitidas para el `event_key` y son
  identificadores simples, sin formato ni conversión;
- las acciones están permitidas para el `event_key`;
- textos, encabezados, pies, botones, secciones y filas respetan el contrato
  vigente de WhatsApp Cloud API (longitudes y cantidades máximas);
- la definición no contiene funciones, código, consultas, URLs internas ni
  instrucciones de persistencia.

## Eventos iniciales

El registro de eventos crece únicamente mediante código revisado. La primera
versión contempla estos grupos:

| Grupo | Eventos representativos | Autoridad del resultado |
| --- | --- | --- |
| Onboarding | `onboarding.invitation`, `onboarding.error` | `OnboardingService` |
| Dashboard | `dashboard.link.sent`, `dashboard.link.not_eligible`, `dashboard.link.error` | `DashboardLinkService` |
| Movimientos | `movement.registered`, `movement.updated`, `movement.annulled`, `movement.invalid_data`, `movement.persistence_error` | `FinanceService` |
| Categorías | `category.confirmation_required`, `category.deleted`, `category.not_found` | `FinanceService` |
| Recordatorios | `reminder.missing_concept`, `reminder.missing_day`, `reminder.created`, `reminder.duplicate`, `reminder.updated`, `reminder.paused`, `reminder.activated`, `reminder.deleted` | `ReminderService` |
| Límites | `limit.missing_data`, `limit.year_confirmation`, `limit.category_confirmation`, `limit.created`, `limit.updated`, `limit.listed`, `limit.deleted`, `limit.bulk_deleted`, `limit.month_selection` | `LimitService` |
| Consultas | `budget.result`, `movements.query_result`, `budget.compensation_proposed` | `BudgetService` / `FinanceService` / `BudgetCompensationService` |
| Conversación | `conversation.context_lost`, `conversation.cancelled` | Dispatcher |

Cada evento declara en código su lista de variables y acciones. Ejemplos:

| Evento | Variables | Acciones permitidas |
| --- | --- | --- |
| `dashboard.link.sent` | `login_url`, `ttl_minutes` | ninguna |
| `movement.registered` | `movement_id`, `movement_type`, `description`, `amount`, `currency`, `category` | `request_category_change` |
| `movement.updated` | `description`, `amount`, `currency`, `category` | ninguna |
| `movement.annulled` | `description`, `amount`, `currency` | ninguna |
| `limit.bulk_deleted` | `category`, `periods`, `count` | ninguna |
| `category.confirmation_required` | `category` | `confirm_category`, `reject_category`, `cancel_pending_operation` |
| `reminder.missing_day` | `concept` | `cancel_pending_operation` |
| `limit.category_confirmation` | `category` | `confirm_limit_category`, `reject_limit`, `cancel_pending_operation` |
| `budget.compensation_proposed` | `summary` | `confirm_compensation`, `reject_compensation`, `cancel_pending_operation` |

Las URLs sólo pueden ingresar mediante variables tipadas producidas por el
backend; no se admiten URLs libres dentro de textos ni acciones.

## Respuestas interactivas

El identificador enviado a WhatsApp es un token opaco derivado de la versión
publicada, el nodo y la opción. Al recibir `button_reply` o `list_reply`, el
backend verifica:

1. que exista un estado interactivo pendiente para ese usuario;
2. que la versión coincida con la fijada al iniciar;
3. que el nodo actual y la opción existan en esa versión;
4. que el tipo de respuesta coincida con el tipo de nodo (`button_reply` con
   `reply_button`, `list_reply` con `list`);
5. que la acción siga permitida para el evento;
6. que el `message_id` entrante no haya sido procesado (deduplicación del
   webhook; ver `architecture.md`).

El texto visible de la opción no se usa como autoridad. Ante cualquier
verificación fallida se responde un error seguro y no se ejecuta la acción.

## Estado y enrutamiento libre

El estado dinámico en Redis (`conversation_flow:<whatsapp_id>`) contiene
únicamente:

- `flow_id` y `version_id`;
- `event_key`;
- `node_id` actual;
- `variables` ya validadas para el evento;
- vencimiento (TTL de 30 minutos).

Reglas de convivencia:

1. `/link` y toda respuesta terminal finalizan sin crear estado dinámico.
2. Una selección interactiva válida continúa el recorrido fijado.
3. `cancelar` limpia el estado dinámico.
4. Un mensaje de texto libre distinto limpia sólo el recorrido dinámico y vuelve
   al dispatcher global. No se obliga al usuario a terminar un menú.
5. Los estados transaccionales existentes conservan sus propias reglas y nunca
   pueden ser fabricados por una definición administrable.
6. Si Redis no está disponible, no se ejecuta una acción interactiva que
   requiera estado. Se devuelve un error seguro y no se confirma éxito.

El dispatcher general, la deduplicación técnica por `whatsapp_message_id` y los
estados multi-turno se documentan en `architecture.md`; acá sólo se describe su
convivencia con los flujos administrables.

## División entre repositorios

### `luka`

- modelos y migraciones;
- registro cerrado de eventos, variables y acciones;
- validación y publicación;
- API administrativa protegida;
- resolución y render de versiones publicadas;
- estado Redis;
- adaptación de mensajes WhatsApp e idempotencia.

### `luka_frontend`

- autorización de administradores identificables;
- listado y detalle;
- editor de borradores;
- visualización de errores de validación;
- publicación y retiro mediante la API de `luka`;
- distinción visible entre borrador y versión publicada.

El secreto de servicio usado para llamar la API del backend permanece en el
servidor de `luka_frontend` y nunca se entrega al navegador.

## Configuración por entorno

### Backend `luka`

| Variable | Uso |
| --- | --- |
| `FLOW_ADMIN_API_KEY` | Credencial interna Bearer que protege `/admin/conversation-flows` y `/admin/conversation-flows/contracts`. Sin valor configurado la API responde 503; con credencial inválida, 401. Debe coincidir con el frontend. |
| `WHATSAPP_GRAPH_API_VERSION` | Versión de Graph API usada para mensajes salientes. El valor por defecto actual es `v26.0`. |
| `REDIS_URL` | Estado fijado de flujo, versión y nodo para recorridos interactivos. |
| `DATABASE_URL` | Persistencia de flujos y versiones en PostgreSQL/Supabase. |

### Servidor `luka_frontend`

| Variable | Uso |
| --- | --- |
| `LUKA_BACKEND_URL` | URL base del backend, sin ruta administrativa. |
| `FLOW_ADMIN_API_KEY` | Misma credencial interna del backend. Nunca se entrega al navegador. |
| `FLOW_ADMIN_AUTH_USER_IDS` | IDs `auth_user_id` autorizados, separados por comas. Una sesión válida que no esté en esta lista recibe 403. |

No guardar valores reales en Git, logs ni capturas. El frontend no debe
consultar las tablas de flujos ni ninguna tabla financiera directamente. La
tabla general de variables de entorno del backend vive en `development.md`.

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

Una conversación que ya mostró botones o una lista conserva su versión fijada en
Redis. Publicar otra versión no cambia las opciones que ese usuario ya vio.

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
