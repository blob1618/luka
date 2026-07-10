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

## Contrato oficial

- `public.usuario`: usuarios oficiales del sistema; debe permitir mapear WhatsApp mediante `whatsapp_id`.
- `public.categorias`: categorías oficiales para clasificar movimientos financieros.
- `public.movimientos_financieros`: entidad central del MVP para ingresos y egresos.
- `public.limite_categoria`: límites mensuales por categoría.
- `public.recordatorio`: recordatorios financieros del usuario.
- `public.evento`: auditoría y trazabilidad de acciones relevantes.
- `public.acuerdo_version`: versiones de acuerdos o consentimientos.
- `public.acuerdo_aceptado`: aceptación de acuerdos por usuario.

## Tablas legacy

Estas tablas existen o pueden existir por modelos anteriores. No se borran en este ticket, pero no deben usarse para nuevas features:

- `public.usuarios`
- `public.presupuestos`
- `public.recordatorios`
- `public.limites_gasto`
- `public.versiones_consentimiento`
- `public.consentimientos_usuario`
- `public.gastos`

`public.gastos` puede contener datos o haber sido parte de una versión previa, pero no es la entidad central del flujo nuevo.

## Consecuencias

- Backend y frontend deben adaptarse al contrato oficial del MVP.
- Supabase debe recibir una migración posterior para alinear el schema real con este contrato.
- STK-35 debe persistir movimientos en `public.movimientos_financieros` cuando exista la migración correspondiente.
- `public.evento` debe usarse para auditoría/trazabilidad, no como tabla principal de movimientos.
- Las nuevas features no deben depender de las tablas legacy.

## Alcance

Este ticket solo documenta y versiona el contrato DB MVP y el schema actual de Supabase. No implementa migraciones, no ejecuta SQL, no modifica lógica backend, no cambia modelos SQLAlchemy y no borra tablas.
