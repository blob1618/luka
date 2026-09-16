# Contrato de mensajes y recorridos conversacionales

Este documento define el alcance técnico de `HU-FLU-01` / `STK-168`.
La capacidad administrable modifica la presentación de resultados que el backend
ya sabe producir. No reemplaza el dispatcher, no introduce un menú principal y
no permite crear reglas de negocio desde el panel.

## Principios

1. El usuario puede solicitar cualquier operación soportada en cualquier
   momento mediante lenguaje natural o comandos existentes como `/link`.
2. El dispatcher y los servicios de dominio conservan la autoridad para decidir
   la intención, validar datos, persistir cambios y determinar el resultado.
3. Un mensaje administrable se resuelve después de obtener un evento seguro del
   backend. El contenido nunca convierte un fallo en éxito ni confirma una
   operación antes del commit correspondiente.
4. Las acciones interactivas se seleccionan de una lista cerrada mantenida por
   el backend. El panel no puede registrar nombres de funciones, URLs internas ni
   expresiones ejecutables.
5. El frontend administra mediante la API protegida del backend. No consulta ni
   modifica estas tablas directamente en Supabase.

## Flujo de decisión

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

La ausencia de una configuración publicada no altera el resultado de dominio.
Durante la adopción incremental, el llamador conserva su respuesta segura
actual hasta que ese evento tenga una versión publicada.

## Recurso administrable

Cada recurso tiene una identidad estable y versiones inmutables publicadas:

- `slug`: identificador administrativo estable.
- `name`: nombre legible para el panel.
- `event_key`: evento cerrado emitido por el backend.
- `status`: `active` o `archived`.
- `draft`: versión editable que todavía no afecta conversaciones.
- `published_version`: única versión efectiva para conversaciones nuevas.

Publicar crea una fotografía inmutable. Editar después de publicar genera o
modifica un nuevo borrador. Retirar el recurso impide iniciar recorridos nuevos,
pero una conversación ya iniciada puede terminar con la versión que fijó al
comenzar.

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

Una opción puede navegar a `next_node` o emitir una `action` permitida, pero no
ambas cosas. Un nodo terminal no puede declarar opciones.

## Validación obligatoria

Antes de publicar, el backend comprueba:

- `start_node` existe;
- los identificadores de nodo y opción son únicos;
- todas las transiciones apuntan a nodos existentes;
- todos los nodos son alcanzables desde el inicial;
- existe al menos una salida terminal;
- las variables usadas están permitidas para el `event_key`;
- las acciones están permitidas para el `event_key`;
- textos, encabezados, pies, botones, secciones y filas respetan el contrato
  vigente de WhatsApp Cloud API;
- la definición no contiene funciones, código, consultas, URLs internas ni
  instrucciones de persistencia.

## Eventos iniciales

El registro de eventos crece únicamente mediante código revisado. La primera
versión contempla estos grupos:

| Grupo | Eventos representativos | Autoridad del resultado |
| --- | --- | --- |
| Onboarding | `onboarding.invitation`, `onboarding.error` | `OnboardingService` |
| Dashboard | `dashboard.link.sent`, `dashboard.link.not_eligible`, `dashboard.link.error` | `DashboardLinkService` |
| Movimientos | `movement.registered`, `movement.invalid_data`, `movement.persistence_error`, `movement.category_hint` | `FinanceService` |
| Categorías | `category.confirmation_required`, `category.changed`, `category.deleted`, `category.not_found` | `FinanceService` |
| Recordatorios | `reminder.missing_concept`, `reminder.missing_day`, `reminder.created`, `reminder.duplicate`, `reminder.updated`, `reminder.paused`, `reminder.activated`, `reminder.deleted` | `ReminderService` |
| Límites | `limit.missing_data`, `limit.year_confirmation`, `limit.category_confirmation`, `limit.created`, `limit.updated`, `limit.listed`, `limit.deleted`, `limit.month_selection` | `LimitService` |
| Consultas | `budget.result`, `movements.query_result` | `BudgetService` / `FinanceService` |
| Conversación | `conversation.context_lost`, `conversation.cancelled` | Dispatcher |

Cada evento declara en código su lista de variables y acciones. Ejemplos:

| Evento | Variables | Acciones permitidas |
| --- | --- | --- |
| `dashboard.link.sent` | `login_url`, `ttl_minutes` | ninguna |
| `movement.registered` | `movement_type`, `description`, `amount`, `currency` | ninguna |
| `movement.category_hint` | ninguna | `request_category_change` |
| `category.confirmation_required` | `category` | `confirm_category`, `reject_category`, `cancel_pending_operation` |
| `reminder.missing_day` | `concept` | `cancel_pending_operation` |
| `limit.category_confirmation` | `category` | `confirm_limit_category`, `reject_limit`, `cancel_pending_operation` |

Las URLs sólo pueden ingresar mediante variables tipadas producidas por el
backend; no se admiten URLs libres dentro de acciones.

## Respuestas interactivas

El identificador enviado a WhatsApp referencia versión, nodo y opción. Al
recibir `button_reply` o `list_reply`, el backend verifica:

1. que exista un estado interactivo pendiente para ese usuario;
2. que la versión coincida con la fijada al iniciar;
3. que el nodo actual y la opción existan en esa versión;
4. que la acción siga permitida para el evento;
5. que el `message_id` entrante no haya sido procesado.

El texto visible de la opción no se usa como autoridad.

## Estado y enrutamiento libre

El estado dinámico en Redis contiene únicamente:

- recurso y versión;
- evento;
- nodo actual;
- contexto de variables ya validado;
- vencimiento.

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

## Fuera de alcance

- menú principal obligatorio;
- activación por frases reservadas como “menú” o “ayuda”;
- creación de intents u operaciones financieras desde el panel;
- ejecución de Python, SQL, URLs internas o webhooks arbitrarios;
- acceso directo del frontend a las tablas administrables;
- constructor visual sin restricciones para reglas de negocio.
