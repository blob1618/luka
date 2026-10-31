# ADR 0003 — Una billetera por usuario y moneda (limpieza directa)

> **Status:** Aceptado como diseño (2026-10-31, revisado). Implementación pendiente en STK-258.
> Este documento **no ejecuta migraciones ni modifica datos**.
>
> **Revisión:** se descarta la variante con `archivada_en` y tablas de fusión. Nunca debió
> existir más de una billetera por moneda y no hay usuarios productivos que justifiquen
> borrado lógico ni auditoría de fusión. El esquema queda con las columnas actuales.

## Contexto

Hoy un usuario puede tener varias billeteras de la misma moneda. El esquema solo impone
unicidad por `(usuario_id, moneda, lower(trim(nombre)))`
(`supabase/migrations/20261012174855_add_billetera_expansion_stk237.sql`). Por eso
`WalletService.resolve_selection` devuelve `needs_wallet_selection` cuando encuentra más de una
billetera de la moneda y el agente pregunta «¿Qué billetera querés usar?» en cada registro
(`app/services/wallet.py`).

La decisión de producto es **una única billetera por usuario y moneda**. No hay usuarios
reales: es un proyecto académico en preparación. La transición puede ser una limpieza directa,
sin capas de compatibilidad.

Monedas admitidas: **ARS y USD**.

## Decisión

### 1. Esquema

`public.billetera` conserva exactamente sus columnas: `id`, `usuario_id`, `nombre`, `moneda`,
`creado_en`. **No se agrega `archivada_en` ni se crean tablas de fusión.**

La unicidad se garantiza con un índice único directo:

```sql
CREATE UNIQUE INDEX billetera_usuario_moneda_uidx
    ON public.billetera (usuario_id, moneda);
```

Se elimina el índice `billetera_usuario_moneda_nombre_uidx`: con una sola billetera por
moneda, la unicidad de nombre ya no aporta nada. Los nombres pueden repetirse entre monedas
(por ejemplo, dos billeteras llamadas «Principal», una en ARS y otra en USD).

### 2. Limpieza de datos existentes

Para cada `(usuario_id, moneda)` con más de una billetera:

1. Elegir la superviviente:
   - mayor cantidad **total** de movimientos (`count(*)`);
   - si empatan, priorizar una billetera cuyo nombre normalizado **no** sea `'inicial'`
     (la generada por el backfill de STK-237);
   - si siguen empatando, `creado_en ASC, id ASC`.
2. Reasignar todos los movimientos de las demás a la superviviente:
   `UPDATE movimientos_financieros SET billetera_id = superviviente WHERE billetera_id = duplicada`.
   La FK compuesta `(billetera_id, usuario_id, moneda)` sigue válida porque la superviviente
   pertenece al mismo usuario y a la misma moneda.
3. `DELETE FROM public.billetera` físico de las duplicadas sobrantes.
4. Reemplazar el índice por `billetera_usuario_moneda_uidx`.

No se modifican importes, fechas, categorías ni estado de anulación. Los movimientos
anulados también se reasignan para que la FK y el historial sigan consistentes.

### 3. Huérfanos

La FK `movimientos_financieros_billetera_fkey` está validada
(`20261012174901_backfill_billetera_historica_stk237.sql`) y `billetera_id` es `NOT NULL`
(`20261012215307_hu_mon_01_wallet_transition_contract.sql`). Por lo tanto **no puede haber
movimientos huérfanos**: todo movimiento apunta a una billetera existente del mismo dueño y
moneda.

Si la migración encontrara una violación previa (constraint no válida, datos cargados fuera
del backend), **aborta y reporta**; no borra movimientos financieros. La instrucción de
eliminar huérfanos no se implementa porque borrar el registro contable es peor que fallar la
migración. Esta es una desviación explícita a confirmar por producto.

### 4. Rollback

El borrado físico es irreversible. La migración corre en una transacción: si falla, rollback
automático. Si termina bien, el único rollback es **restaurar el backup previo** de
`public.billetera` y `public.movimientos_financieros`.

Antes de ejecutar:

```bash
pg_dump --table=public.billetera --table=public.movimientos_financieros \
        --data-only --column-inserts "$DATABASE_URL" > backup_stk258.sql
```

No se agregan tablas de auditoría para evitar el costo permanente de una decisión que no
tiene usuarios que la necesiten. El backup es el mecanismo de rollback y queda documentado en
el runbook de la migración.

### 5. Backend

#### `WalletService.create_wallet`

- Si no se indica nombre (o está vacío), usar **«Pesos»** para ARS y **«Dólares»** para USD.
- Si el usuario ya tiene una billetera en esa moneda, rechazar sin escribir:
  `status='wallet_already_exists'`, con `wallet_id`, `name` y `currency` de la existente.
  El mensaje informa cuál es y ofrece usarla o renombrarla.
- El estado `name_already_exists` deja de existir como caso separado: con una billetera por
  moneda, un nombre repetido en la misma moneda es la misma billetera.

#### `WalletService.resolve_selection`

- Para una moneda, el usuario tiene 0 o 1 billetera. Se elimina la rama de ambigüedad y el
  campo `options` de `WalletSelectionResult`.
- Estados resultantes: `selected`, `needs_wallet_creation`, `invalid_selection`,
  `currency_mismatch`, `invalid_data`, `user_not_found`.
- Con `wallet_id` explícito: se valida dueño y moneda; si la billetera no existe, error
  explícito.
- Sin `wallet_id`: 0 billeteras → `needs_wallet_creation`; 1 → `selected`.

#### `movements.py` (líneas 163–170)

Hoy cualquier estado distinto de `selected` arma `selection.options` y devuelve la pregunta de
selección; con `needs_wallet_creation` las opciones están vacías y el usuario ve «Elegí una de
las opciones» sin opciones. Se corrige con una rama explícita, igual que
`movement_management.py` (líneas 124–131):

```python
if selection.status == 'needs_wallet_creation':
    return IncompleteOperation(missing_fields=(FieldRequest(
        field='wallet_ref', reason='missing',
        question=f"No tenés una billetera en {selection.currency}. ¿La creo?"),))
if selection.status != 'selected':
    raise ValueError('No pude usar esa billetera; revisá la moneda y la billetera indicadas')
```

Se elimina la rama de `needs_wallet_selection` en `movements.py` y en
`movement_management.py`.

#### Renombrar billetera

No existe hoy. Se agrega:

- `WalletService.rename_wallet(user_id, wallet_id, name)` con dueño, nombre no vacío y
  longitud acotada.
- Capacidad de asistente `rename_wallet` (`WalletRenameDraft` con `wallet_ref` y `name`), con
  preview y confirmación como el resto de las mutaciones.
- Si el dashboard necesita renombrar, endpoint del backend correspondiente; el alcance mínimo
  es servicio + asistente.

#### `entities.py`

`WalletDraft.name` pasa a ser opcional (default resuelto por el servicio). Se agrega la
operación de renombre; `create_wallet` informa el rechazo con la billetera actual.

### 6. Receipts y snapshots

- `MovementSnapshot` incluye `wallet_id` (`app/services/assistant_contracts.py`) y
  `snapshot()` lo toma del movimiento (`app/services/movement_commands.py`). Después de la
  limpieza, todo movimiento reasignado apunta a la superviviente; las lecturas y validaciones
  posteriores usan ese valor.
- `assistant_write_receipts.result` guarda el snapshot del momento de la confirmación. **No se
  reescribe**: es evidencia histórica y conserva la billetera original.
- `validate_dependencies` no necesita cambios adicionales: al borrarse la billetera duplicada,
  un borrador pendiente que la referencie falla con «La billetera ya no está disponible», y el
  usuario vuelve a preparar. Los borradores pendientes pueden invalidarse durante la ventana
  de mantenimiento; los recibos confirmados no se tocan.

### 7. STK-246

STK-246 **no se cierra como duplicada**. Cubre la resolución cuando el usuario nombra su
billetera:

1. Nombre de la única billetera de esa moneda → usarla, sin preguntar.
2. Nombre de una billetera inexistente o medio de pago («Mercado Pago») → preguntar con una
   frase concreta que nombre la billetera real («Tu billetera de pesos es “Pesos”; ¿la uso?”),
   nunca un selector vacío.
3. Si el usuario insiste en otra billetera, el camino es crearla o renombrar la existente.

Este ADR elimina la ambigüedad estructural; STK-246 conserva su tarjeta para el lenguaje
natural y sus pruebas.

## Migración (diseño, no ejecutada)

Archivo propuesto: `supabase/migrations/20261031_stk258_unique_wallet_per_currency.sql`.

```sql
BEGIN;

-- Snapshot de control
CREATE TEMP TABLE movimientos_antes AS
SELECT usuario_id, moneda,
       count(*) AS total,
       count(*) FILTER (WHERE anulado_en IS NULL) AS activos,
       coalesce(sum(cantidad) FILTER (WHERE tipo = 'ingreso'), 0) AS ingresos,
       coalesce(sum(cantidad) FILTER (WHERE tipo = 'egreso'), 0) AS egresos
FROM public.movimientos_financieros
GROUP BY usuario_id, moneda;

-- Superviviente por (usuario_id, moneda)
CREATE TEMP TABLE billetera_superviviente AS
SELECT usuario_id, moneda, id AS survivor_id
FROM (
    SELECT b.id, b.usuario_id, b.moneda,
           row_number() OVER (
               PARTITION BY b.usuario_id, b.moneda
               ORDER BY (
                   SELECT count(*) FROM public.movimientos_financieros m
                   WHERE m.billetera_id = b.id
               ) DESC,
               (lower(btrim(b.nombre)) = 'inicial') ASC,
               b.creado_en ASC,
               b.id ASC
           ) AS rn
    FROM public.billetera b
) ranked
WHERE rn = 1;

-- Reasignar movimientos de las duplicadas
UPDATE public.movimientos_financieros m
SET billetera_id = s.survivor_id
FROM public.billetera_superviviente s
WHERE m.usuario_id = s.usuario_id
  AND m.moneda = s.moneda
  AND m.billetera_id <> s.survivor_id
  AND m.billetera_id IN (
      SELECT b.id FROM public.billetera b
      WHERE b.usuario_id = s.usuario_id AND b.moneda = s.moneda
  );

-- Borrar duplicadas
DELETE FROM public.billetera b
USING public.billetera_superviviente s
WHERE b.usuario_id = s.usuario_id
  AND b.moneda = s.moneda
  AND b.id <> s.survivor_id;

-- Restricción final
DROP INDEX IF EXISTS public.billetera_usuario_moneda_nombre_uidx;
CREATE UNIQUE INDEX billetera_usuario_moneda_uidx
    ON public.billetera (usuario_id, moneda);

-- Validaciones
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.billetera
        GROUP BY usuario_id, moneda HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'STK-258: quedaron billeteras duplicadas';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM movimientos_antes a
        FULL JOIN (
            SELECT usuario_id, moneda, count(*) AS total,
                   count(*) FILTER (WHERE anulado_en IS NULL) AS activos,
                   coalesce(sum(cantidad) FILTER (WHERE tipo = 'ingreso'), 0) AS ingresos,
                   coalesce(sum(cantidad) FILTER (WHERE tipo = 'egreso'), 0) AS egresos
            FROM public.movimientos_financieros GROUP BY usuario_id, moneda
        ) d USING (usuario_id, moneda)
        WHERE a.total IS DISTINCT FROM d.total
           OR a.activos IS DISTINCT FROM d.activos
           OR a.ingresos IS DISTINCT FROM d.ingresos
           OR a.egresos IS DISTINCT FROM d.egresos
    ) THEN
        RAISE EXCEPTION 'STK-258: cambiaron los totales de movimientos';
    END IF;
END $$;

COMMIT;
```

Notas de ejecución:

- Corre en una transacción; cualquier fallo revierte todo (incluido el `DELETE`).
- `UPDATE`/`DELETE` toman locks de fila; con el volumen actual es inmediato. Para una base
  grande se batchea por `(usuario_id, moneda)`, pero no es el caso.
- `DROP INDEX` y `CREATE UNIQUE INDEX` no son `CONCURRENTLY` porque van dentro de la
  transacción; bloquean `billetera` unos instantes, aceptable con mantenimiento.
- Idempotencia: si se vuelve a correr, no hay duplicados y el `CREATE INDEX IF NOT EXISTS`
  no falla. La migración se ejecuta una vez igual.

### Backup obligatorio

Antes de ejecutar, `pg_dump` de `billetera` y `movimientos_financieros` (ver sección 4). Sin
backup no se ejecuta: el `DELETE` es físico y no hay tabla de mapeo para reconstruirlo.

## Impacto por capa

| Capa | Cambio |
|---|---|
| Esquema | Índice único `(usuario_id, moneda)`; se elimina el índice por nombre; sin columnas ni tablas nuevas |
| Datos | Reasignación de movimientos y `DELETE` de billeteras duplicadas; backup previo |
| `WalletService` | `create_wallet` rechaza duplicado con la billetera actual y aplica defaults; `resolve_selection` sin ambigüedad; `rename_wallet` nuevo |
| `movements.py` | Rama explícita para `needs_wallet_creation`; se elimina el selector vacío |
| `movement_management.py` | Se elimina la rama de selección múltiple |
| `entities.py` | `WalletDraft.name` opcional; capacidad `rename_wallet` |
| `finance.py` | Sin cambios de contrato: sigue usando `resolve_selection` |
| `validate_dependencies` | Sin cambios: la billetera borrada produce «La billetera ya no está disponible» |
| Recibos | Sin cambios de esquema ni reescritura; snapshot histórico inmutable |
| Dashboard | Sin cambios obligatorios; si expone alta/renombre, ajustar mensajes |
| Pruebas | Servicio, selección, contrato de renombre, migración y E2E de staging |

## Plan de pruebas

1. **Servicio**: crear ARS con nombre vacío → «Pesos»; crear USD vacío → «Dólares»; segunda ARS
   → `wallet_already_exists` sin escribir y con la billetera actual; renombrar propia; rechazar
   ajena, vacía o demasiado larga.
2. **Selección**: 0 billeteras → `needs_wallet_creation`; 1 → `selected`; `wallet_id` ajeno o de
   otra moneda → error; no existe `needs_wallet_selection`.
3. **Comandos**: registrar sin billetera informa la creación (sin opciones vacías); registrar
   con la única billetera no pregunta; renombrar y volver a registrar usa el nombre nuevo.
4. **Migración**: duplicados resueltos; totales, activos, ingresos y egresos iguales; sin
   movimientos apuntando a billeteras borradas; índice único presente; correr dos veces no
   falla.
5. **Rollback**: restaurar backup en una base de prueba y verificar que reaparecen las
   billeteras y los movimientos originales.
6. **E2E/staging**: la conversación de humo deja de recibir la pregunta de billetera; el
   dashboard lista una sola billetera por moneda.

## Alcance revisable de STK-258

### Incluye

- Migración de limpieza (reasignación + `DELETE` + índice único) con validaciones y backup.
- `create_wallet` con rechazo, defaults y mensaje de billetera actual.
- `resolve_selection` sin ambigüedad y sin `options`.
- Fix de `needs_wallet_creation` en `movements.py`; limpieza en `movement_management.py`.
- `rename_wallet` en servicio y capacidad de asistente.
- Pruebas y documentación.

### No incluye

- `archivada_en`, `billetera_fusion` ni borrado lógico.
- Monedas distintas de ARS/USD.
- Reescritura de recibos históricos.
- Cierre de STK-246 (se implementa coordinado).
- Endpoint de renombre en dashboard si no existe hoy (se evalúa aparte).

### Criterios de aceptación

- No se puede crear una segunda billetera por moneda; el rechazo no escribe y nombra la actual.
- La base no tiene pares `(usuario_id, moneda)` duplicados y el índice único existe.
- Ningún movimiento apunta a una billetera borrada; totales y conteos no cambian.
- Ningún registro pregunta billetera cuando hay una sola.
- `needs_wallet_creation` informa la falta de billetera y guía a crearla, sin opciones vacías.
- Renombrar funciona con confirmación y no permite nombres inválidos.
- Suite completa y E2E de staging en verde.

### Decisiones resueltas (2026-10-31)

1. **Rollback solo por backup:** aceptado. El `DELETE` es irreversible y la migración exige
   backup previo verificado y ventana de mantenimiento.
2. **Huérfanos:** la migración aborta y reporta; no borra movimientos financieros.
3. **Renombrar:** servicio y capacidad de asistente en esta entrega; el dashboard solo si ya
   expone la operación.
4. **Mismo nombre en ARS y USD:** permitido; la unicidad es por moneda.
5. **Ventana y responsable del backup:** se definen antes de ejecutar la migración; sin backup
   verificado no se corre.
