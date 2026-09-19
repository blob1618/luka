# Arquitectura

Arquitectura actual del backend de LUKA después de STK-35 y límites conocidos del MVP.

## Componentes

- Meta WhatsApp Cloud API: canal de entrada y salida de mensajes.
- FastAPI: recibe webhooks, extrae los datos del mensaje y orquesta el flujo.
- `LLMService`: interpreta texto en lenguaje natural y normaliza el resultado estructurado.
- `FinanceService`: valida reglas de negocio, resuelve usuario y categoría y persiste movimientos.
- `LimitService`: crea, edita, lista y elimina límites mensuales por categoría y moneda.
- `BudgetService`: agrega egresos persistidos y calcula consumo, disponible, porcentaje y exceso.
- PostgreSQL/Supabase: base compartida; `public.movimientos_financieros` es la tabla oficial de movimientos.
- SQLite: base local por defecto para desarrollo y tests.
- Redis: disponible para funciones auxiliares, pero no participa actualmente en el registro ni en la deduplicación de STK-35.
- Render: hosting del backend desplegado desde `main`.

## Flujo implementado por STK-35

```mermaid
sequenceDiagram
    participant U as Usuario de WhatsApp
    participant M as Meta WhatsApp
    participant W as Webhook FastAPI
    participant L as LLMService
    participant F as FinanceService
    participant D as Supabase/PostgreSQL

    U->>M: Envía mensaje de texto
    M->>W: Webhook con teléfono, message_id y texto
    W->>L: Interpreta el mensaje
    L-->>W: intent, movement_type y datos financieros
    alt intent = expense
        W->>F: Solicita registrar movimiento
        F->>D: Busca usuario y duplicado; persiste si es válido
        D-->>F: Resultado de lectura/escritura
        F-->>W: Estado de registro
        W->>M: Respuesta segura del backend
    else intent no registrable
        W->>M: Respuesta sin persistencia financiera
    end
    M-->>U: Entrega respuesta
```

El webhook procesa actualmente mensajes de tipo texto. Extrae `sender_phone`, `whatsapp_message_id` y `text_body`, llama primero a `LLMService` y solo invoca `FinanceService` cuando el resultado usa `intent="expense"`.

## Contrato LLM y routing

- `movement_type="ingreso"` representa una entrada de dinero.
- `movement_type="egreso"` representa una salida de dinero.
- `movement_type=null` representa un mensaje no registrable o ambiguo.
- `transaction_type` se acepta como alias de compatibilidad y se normaliza a `movement_type`.
- Si el proveedor devuelve `intent="expense"` sin ninguno de esos campos, `LLMService` conserva el fallback histórico a `movement_type="egreso"`.
- `intent="expense"` se mantiene por compatibilidad para ingresos y egresos; el tipo real lo determina `movement_type`.
- `greeting`, `out_of_scope`, `reminder`, `budget_query` y `expense_summary` no se guardan en `public.movimientos_financieros`.

El LLM interpreta el mensaje, pero no es autoridad para confirmar persistencia. Para movimientos registrables, el backend descarta cualquier confirmación anticipada incluida en `reply_text` y construye la respuesta a partir del resultado real de `FinanceService`.

## Usuarios y autorización inicial

`FinanceService` exige que `sender_phone` coincida con `public.usuario.whatsapp_id`. Si no existe el usuario, devuelve `user_not_found`, no crea ninguna fila y el backend informa que no pudo registrar el movimiento.

STK-35 no implementa:

- Alta o registro de usuarios.
- Login.
- Vinculación inicial entre WhatsApp y `public.usuario`.
- Validación de consentimiento dentro del flujo de registro de movimientos.

El flujo oficial de alta y vinculación debe resolverse en una historia separada. La arquitectura objetivo puede requerir consentimiento y auditoría, pero no deben presentarse como pasos implementados por STK-35.

## Validación, categorías y persistencia

Para registrar un movimiento, `FinanceService` valida el teléfono, el resultado del LLM, un monto positivo, `movement_type`, moneda y una descripción resoluble. La moneda se normaliza a mayúsculas y usa `ARS` cuando el LLM no envía un valor.

La categoría es opcional:

- Se busca por nombre entre las categorías activas del usuario.
- Solo se asigna `categoria_id` si ya existe una coincidencia.
- No se crean categorías automáticamente.
- Sin coincidencia, el movimiento se guarda con `categoria_id=null`.

Las categorías default y personalizadas quedan pendientes de trabajo específico.

## Estados de registro y respuesta

| Estado | Efecto |
| --- | --- |
| `registered` | La escritura terminó correctamente y el backend confirma el ingreso o egreso. |
| `duplicate` | No se inserta una segunda fila; es un reintento/reenvío de Meta de un mensaje ya procesado y se suprime la respuesta visible (la confirmación ya se envió en la primera entrega). |
| `user_not_found` | No existe un usuario vinculado y no se persiste el movimiento. |
| `invalid_data` | Faltan datos válidos y el backend solicita reformular o completar el mensaje. |
| `persistence_error` | La escritura falló y no se confirma el registro. |
| `not_a_movement` | No se identificó un movimiento financiero registrable. |

La deduplicación consulta `whatsapp_message_id` antes de insertar y el ORM/migración declaran un índice único parcial. Aun así, la aplicación real de ese índice debe verificarse en Supabase. Cuando Meta reenvía un mensaje ya persistido, no se duplica la fila y el reintento se procesa en silencio: la respuesta visible se suprime porque la confirmación ya se envió en la primera entrega.

## Control de presupuesto (HU-PRE-01 / STK-47)

El flujo de presupuesto es completamente backend-driven:

```text
Alta/edición de límite -> LimitService -> limite_categoria
Registro de egreso -> FinanceService -> movimientos_financieros -> BudgetService -> alerta inmediata
Consulta budget_query -> BudgetService -> respuesta calculada
```

Las cifras de presupuesto nunca provienen del texto generado por el LLM. El LLM solo clasifica la intención y extrae categoría, mes, año y moneda; el backend identifica al usuario por `whatsapp_id`, consulta datos persistidos y construye la respuesta.

Solo los egresos categorizados de la misma moneda y dentro del período consumen el límite. Al superar el tope, la confirmación del movimiento incluye una alerta con total consumido y exceso. Las consultas pueden pedir una categoría o listar todos los presupuestos del período. Crear un límite sobre una categoría canónica inexistente requiere confirmación explícita antes de crear la categoría.

No se ejecuta un job periódico de alertas: la alerta se evalúa inmediatamente después de una persistencia exitosa. Esto evita duplicados de notificación y mantiene al scheduler reservado para recordatorios.

## Recordatorio proactivo diario (STK-60)

`check_proactive_prompts` corre cada 5 minutos y, sólo durante la hora local `PROACTIVE_PROMPT_HOUR`, envía un mensaje libre preguntando por gastos no registrados a los usuarios con `proactivo_habilitado`, ventana de WhatsApp de 24h abierta y sin movimientos vigentes con fecha de hoy. `proactivo_ultimo_envio` se reclama antes de enviar (máximo un aviso por día) y se libera si el envío falla. Los intents `disable_proactive_reminders` / `enable_proactive_reminders` actualizan la preferencia desde el chat.

## Dashboard, Magic Link y consultas

El repositorio no contiene un frontend implementado. Para Release 1, el acceso financiero debe continuar mediado por backend:

```text
WhatsApp -> Backend -> Supabase
Dashboard -> Backend -> Supabase
```

El acceso seguro al micrositio/dashboard mediante Magic Link está relacionado con STK-54 y requiere coordinación backend + frontend. STK-35 no implementa Magic Link, login ni endpoints de dashboard.

STK-128, correspondiente a la consulta de movimientos, también queda fuera de STK-35.

## Limitaciones y backlog técnico

- Verificar en Supabase los índices productivos de `public.usuario`, categorías y movimientos, incluido el índice único parcial de `whatsapp_message_id`.
- Medir la latencia por etapa; durante pruebas manuales se observó una latencia aproximada de 5–10 segundos entre LLM, base de datos, API de WhatsApp y Render, pendiente de medición formal por etapa.
- Investigar typing indicator y mark as read en WhatsApp Business API.
- Implementar observabilidad del recorrido webhook -> LLM -> DB -> respuesta.
- Definir el flujo oficial de alta y vinculación de usuarios.
- Incorporar rate limiting y protección ante abuso de tokens.
- Reducir llamadas innecesarias al LLM mediante validación temprana de usuarios y duplicados.
- Evaluar un pre-router para saludos y mensajes claramente fuera de alcance.

Estas mejoras son trabajo futuro. En particular, el flujo actual llama al LLM antes de validar si el usuario existe o si el `whatsapp_message_id` ya fue procesado.

## Pruebas reales

Las pruebas reales de integración dependen del número configurado en Meta, el webhook público, la base de datos del entorno y un usuario previamente vinculado. El flujo automatizado también está cubierto con SQLite temporal para verificar webhook -> `FinanceService` real -> persistencia sin usar Supabase.

Deploy actual:

```text
https://luka-f2nb.onrender.com
```
