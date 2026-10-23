# ADR 0002 — Dashboard servido por BFF (Astro/Workers)

## Estado

Accepted, 2026-10-23.

## Contexto

ADR 0001 ya exigía frontend → backend para el MVP; no autorizaba Data API directa. La migración a Astro/Workers usa Hyperdrive y el Worker conecta como `postgres`. RLS no protege al Worker. La Data API pública tenía grants en tres tablas. ADR 0002 registra el “as-built”, no revierte una decisión de lectura directa.

## Decisión

El dashboard se sirve por BFF (Astro/Workers). El backend Python es dueño de schema, migraciones, cotizaciones y WhatsApp. El navegador no llama a la Data API de Supabase. Toda query del BFF filtra por `usuario_id`. RLS queda como defensa en profundidad, no como aislamiento efectivo del Worker.

## Consecuencias

- Nuevo dato del dashboard = endpoint BFF, no REST directo.
- Tests cross-user obligatorios.
- `anon`/`authenticated` revocados en `public`.
- `service_role` solo server-side.

## Deuda diferida

| Deuda | Motivo | Disparador para pagarla |
|---|---|---|
| Rol de mínimo privilegio del Worker | Requiere rotar credenciales de Hyperdrive y grants por tabla; el Worker ya filtra por usuario | Antes de multi-tenant o auditoría de seguridad |
| Cache headers `private, no-store` | Cloudflare no cachea SSR por defecto; no hay leak probado | Si se agrega CDN/cache delante del Worker |
| Encoding de `flowId` admin | Ruta admin con API key; riesgo bajo | Si el admin se expone a más usuarios |
| `billetera_id` nullable en ORM | La DB ya es `NOT NULL`; cambiarlo puede romper tests SQLite | Al unificar modelos/migraciones |
| Migrar el dashboard al backend | Duplica el BFF y no es necesario para STK-45 | Si se unifica el frontend o hay requisito de compliance |

## Alternativas

- Mover todo al backend: descartada por alcance.
- Data API directa: rechazada por seguridad/ADR.

## Supersede

Supersede ADR 0001 como baseline del contrato/RLS; reafirma el camino de lectura BFF que ADR 0001 ya exigía.
