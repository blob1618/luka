# ADR 0001 — Contrato DB MVP

## Estado

Aceptado / vigente para Release 1.

## Contexto

El backend y Supabase quedaron desalineados: el código actual y la base remota contienen tablas que responden a distintos modelos de datos. Supabase tiene tablas oficiales nuevas junto con tablas legacy de versiones previas, por lo que backend, frontend y agentes necesitan una fuente técnica única para Release 1.

Release 1 busca validar el flujo punta a punta de LUKA:

```text
WhatsApp text -> interpretar movimiento financiero -> guardar en base -> confirmar al usuario -> visualizar en dashboard
```

## Decisión

El MVP usará `public.movimientos_financieros` como entidad central para registrar movimientos financieros. Esta tabla debe soportar ingresos y egresos y será la fuente principal para el flujo nuevo y el dashboard.

`public.usuario` será la tabla oficial de usuarios. El contrato requiere que tenga `whatsapp_id` para mapear el identificador recibido desde WhatsApp con `usuario.id`.

`public.evento` queda reservado para auditoría y trazabilidad. No será la fuente principal de movimientos financieros.

## Decisión complementaria: acceso a datos y RLS

Para Release 1, el acceso a datos financieros será mediado por el backend.

El frontend no debe consultar Supabase directamente para leer o escribir información financiera sensible. El dashboard web debe consumir endpoints del backend, y el backend será responsable de aplicar las reglas de autorización, filtrar por usuario y proteger el acceso a los datos.

La tabla `public.movimientos_financieros` tiene Row Level Security habilitado. No se definen policies públicas para roles `anon` o `authenticated` en esta etapa, porque el proyecto todavía no tiene resuelta una estrategia completa de Supabase Auth ni el mapeo `auth.uid()` -> `public.usuario.id`.

La estrategia para Release 1 queda definida así:

- WhatsApp se comunica con el backend.
- El backend identifica al usuario mediante `public.usuario.whatsapp_id`.
- El backend lee y escribe en Supabase usando la conexión de servidor configurada en `DATABASE_URL`.
- El frontend del dashboard consulta al backend, no a Supabase directamente.
- El backend nunca debe devolver ni modificar datos financieros sin filtrar por el usuario correspondiente.
- El backend no debe aceptar `usuario_id` arbitrarios enviados por el cliente como criterio de autorización.

Si en una release futura el frontend consulta Supabase directamente, deberán definirse policies RLS específicas basadas en una estrategia formal de autenticación y autorización.

## Contrato oficial

- `public.usuario`: usuarios oficiales del sistema; debe permitir mapear WhatsApp mediante `whatsapp_id`.
- `public.categorias`: categorías oficiales para clasificar movimientos financieros.
- `public.movimientos_financieros`: entidad central del MVP para ingresos y egresos.
- `public.limite_categoria`: límites mensuales por categoría.
- `public.recordatorio`: recordatorios financieros del usuario.
- `public.evento`: auditoría y trazabilidad de acciones relevantes.
- `public.acuerdo_version`: versiones de acuerdos o consentimientos.
- `public.acuerdo_aceptado`: aceptación de acuerdos por usuario.


## Consecuencias

- Backend y frontend deben adaptarse al contrato oficial del MVP.
- El contrato se implementa mediante migraciones versionadas en `database/migrations/`, comenzando por `001_mvp_movimientos_financieros.sql`.
- STK-35 debe persistir movimientos en `public.movimientos_financieros` cuando exista la migración correspondiente.
- `public.evento` debe usarse para auditoría/trazabilidad, no como tabla principal de movimientos.
- El dashboard de Release 1 debe obtener datos financieros mediante endpoints del backend.
- No se crearán policies RLS públicas para `public.movimientos_financieros` mientras no exista una estrategia formal de Supabase Auth.
- La autorización de acceso a movimientos financieros queda inicialmente centralizada en el backend.
- STK-54 debe alinearse con esta decisión: frontend -> backend -> Supabase, no frontend -> Supabase directo.

## Alcance

Este ticket solo documenta y versiona el contrato DB MVP y el schema actual de Supabase. No implementa migraciones, no ejecuta SQL, no modifica lógica backend, no cambia modelos SQLAlchemy y no borra tablas.
