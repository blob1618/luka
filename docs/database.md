# Database

Documento minimo para ubicar la base de datos actual y el diseno objetivo de LUKA.

## Fuentes usadas

- Codigo actual: `app/models/database.py`.
- Guia existente: `SUPABASE_SETUP.md`.
- PDF de referencia: `Flujo de datos y Script DB.pdf`.
- Schema actual exportado de Supabase: `database/schema_supabase_actual.sql`.
- ADR vigente: `docs/decisions/0001-mvp-db-contract.md`.

## Contrato DB MVP vigente

Supabase actual contiene tablas mezcladas de distintos modelos. El contrato oficial para Release 1 queda definido en `docs/decisions/0001-mvp-db-contract.md`.

Tablas oficiales del contrato MVP:

- `public.usuario`
- `public.categorias`
- `public.movimientos_financieros`
- `public.limite_categoria`
- `public.recordatorio`
- `public.evento`
- `public.acuerdo_version`
- `public.acuerdo_aceptado`

`public.movimientos_financieros` es la entidad central para ingresos y egresos del flujo nuevo. `public.usuario` debe tener `whatsapp_id` para mapear el número recibido desde WhatsApp con `usuario.id`.

Tablas legacy/no usadas para nuevas features:

- `public.usuarios`
- `public.presupuestos`
- `public.recordatorios`
- `public.limites_gasto`
- `public.versiones_consentimiento`
- `public.consentimientos_usuario`
- `public.gastos`

Las tablas legacy no se borran todavía, pero las nuevas features no deben depender de ellas. Todo cambio de schema debe versionarse en GitHub antes de considerarse parte del contrato técnico.

## Estado actual en el repo

El código actual usa SQLAlchemy y toma la conexión desde `DATABASE_URL`.

- Local por defecto: `sqlite:///./luka.db`.
- Producción/entornos compartidos: PostgreSQL en Supabase.
- Modelos actuales: `User`, `Expense`, `Budget`, `Reminder`.

Tablas actuales según `app/models/database.py`:

- `usuarios`
- `gastos`
- `presupuestos`
- `recordatorios`

Comando actual para crear tablas desde los modelos:

```powershell
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

No hay migraciones versionadas en GitHub. El schema actual copiado desde Supabase queda versionado como referencia en `database/schema_supabase_actual.sql`; no debe ejecutarse como migración.

## Diseno previo de referencia

El PDF `Flujo de datos y Script DB.pdf` propuso un modelo PostgreSQL/Supabase mas completo, con eventos auditables y estado proyectado. Ese material queda como referencia historica; el contrato vigente para Release 1 es el ADR 0001.

Tablas propuestas:

- `usuarios`
- `versiones_consentimiento`
- `consentimientos_usuario`
- `categorias`
- `limites_gasto`
- `recordatorios`
- `eventos`

Enums propuestos:

- `estado_usuario_enum`
- `tipo_evento_enum`

Tambien propone indices, triggers y funciones para registrar eventos y actualizar timestamps.

## Regla previa del PDF

El modelo del PDF separaba:

- Eventos: historico auditable e inmutable.
- Proyecciones: estado actual optimizado para consultas.

Regla operativa propuesta por el PDF:

1. Recibir request.
2. Validar reglas de negocio.
3. Generar evento.
4. Persistir evento.
5. Actualizar proyeccion.

Esta regla no reemplaza el contrato MVP vigente. Para Release 1, `public.evento` queda para auditoria/trazabilidad y `public.movimientos_financieros` es la fuente principal de movimientos.

## Release 1

Para Release 1, la fuente de decisión del contrato DB MVP es `docs/decisions/0001-mvp-db-contract.md`. El schema real de Supabase todavía necesita una migración posterior para alinearse completamente con ese contrato.

Release 1 objetivo deberia usar la base para:

- Identificar usuarios registrados.
- Bloquear interacciones de usuarios no registrados.
- Validar consentimiento vigente antes de guardar datos financieros.
- Registrar eventos relevantes.
- Guardar estado actual en tablas proyectadas.
- Soportar categorias, limites y recordatorios si entran en alcance.

## Diferencia importante

Hay una brecha entre el codigo actual, Supabase y el diseno del PDF:

- El codigo actual ya usa tablas en espanol: `usuarios`, `gastos`, `presupuestos`, `recordatorios`.
- Supabase actual contiene tablas oficiales y tablas legacy de modelos previos.
- El contrato MVP agrega `movimientos_financieros` como entidad central para ingresos y egresos.

Antes de desarrollar tickets que toquen persistencia, el equipo debe seguir el contrato MVP versionado y preparar migraciones posteriores para alinear Supabase y los modelos.

## Diagrama actual del codigo

Diagrama Mermaid basado en `app/models/database.py`:

```mermaid
erDiagram
    usuarios {
        int id PK
        string whatsapp_id UK
        datetime creado_en
    }

    gastos {
        int id PK
        int usuario_id FK
        float monto
        string categoria
        string descripcion
        datetime creado_en
    }

    presupuestos {
        int id PK
        int usuario_id FK
        string categoria
        float monto_limite
    }

    recordatorios {
        int id PK
        int usuario_id FK
        string titulo
        datetime fecha_vencimiento
        int activo
    }

    usuarios ||--o{ gastos : tiene
    usuarios ||--o{ presupuestos : tiene
    usuarios ||--o{ recordatorios : tiene
```

Este diagrama describe los modelos actuales del codigo, no el contrato DB MVP decidido. Si Supabase pasa a ser la fuente real del esquema, conviene exportar el schema SQL y regenerar el diagrama desde ese schema.

## Versionado recomendado

Regla minima:

1. Versionar en GitHub todo cambio de schema antes de usarlo desde backend o frontend.
2. Mantener actualizado `database/schema_supabase_actual.sql` cuando se vuelva a exportar el schema real.
3. Actualizar `docs/database.md` y el ADR correspondiente cuando cambie el contrato.

Cuando el equipo necesite historial de cambios, pasar a migraciones:

```text
supabase/migrations/<timestamp>_<descripcion>.sql
```

No puedo confirmar si hay cambios hechos directamente en Supabase porque no hay acceso real a ese proyecto desde el repo. Si existen, hoy no estan representados en GitHub.

## Pendiente de validar

- Versionar el script SQL real dentro del repo.
- Confirmar proyecto/URL final de Supabase.
- Definir si se agregan migraciones, por ejemplo Alembic.
- Definir RLS/politicas de acceso en Supabase.
- Preparar la migracion que agregue `public.movimientos_financieros` y `usuario.whatsapp_id`.
- Definir reglas concretas de auditoria para `public.evento`.
- Definir si Redis se usa para rate limiting, deduplicacion o cache de usuario.
