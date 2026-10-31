# ADR 0003 — Una billetera activa por usuario y moneda

> **Status:** Aceptado como diseño (2026-10-31). Implementación pendiente en STK-258.
> Este documento **no ejecuta migraciones ni modifica datos**.

## Contexto

Hoy un usuario puede tener varias billeteras de la misma moneda. El esquema solo impone
unicidad por `(usuario_id, moneda, lower(trim(nombre)))` (`docs/decisions` anteriores y
`supabase/migrations/20261012174855_add_billetera_expansion_stk237.sql`). Como consecuencia,
`WalletService.resolve_selection` devuelve `needs_wallet_selection` cuando encuentra más de
una billetera de la moneda pedida (`app/services/wallet.py`), y el agente pregunta «¿Qué
billetera querés usar?» en cada registro. Es fricción permanente para el usuario y una fuente
de estados ambiguos.

La decisión de producto (STK-258) es **una única billetera activa por usuario y moneda**. Las
existentes se archivan y sus movimientos se reasignan a una superviviente, preservando
trazabilidad individual y permitiendo rollback determinista.

Alcance de monedas: **ARS y USD**, sin ampliar.

## Decisión

### 1. Contrato

- Un usuario tiene **como máximo una billetera activa por moneda**.
- Activa = `billetera.archivada_en IS NULL`.
- La unicidad se garantiza en la base con un índice único parcial:
  `(usuario_id, moneda) WHERE archivada_en IS NULL`.
- `movimientos_financieros.billetera_id` ya es `NOT NULL`
  (`20261012215307_hu_mon_01_wallet_transition_contract.sql`): todo movimiento tiene billetera.
- La FK compuesta `(billetera_id, usuario_id, moneda)` sigue vigente: una reasignación solo es
  válida dentro de la misma moneda y del mismo dueño.

### 2. Creación y nombres

- El nombre sigue siendo libre y único por `(usuario_id, moneda)`; se conserva el índice
  existente.
- Si no se indica nombre, el default es **«Pesos»** para ARS y **«Dólares»** para USD.
- Crear una segunda billetera activa de una moneda ya cubierta **se rechaza sin escribir
  nada**. La respuesta informa el nombre de la billetera activa y ofrece usarla o renombrarla.
- **Renombrar** no existe hoy (`WalletService` no tiene operación de renombre). Como el
  mensaje de rechazo promete esa salida, el alcance incluye:
  - `WalletService.rename_wallet(user, wallet_id, name)` con validación de duplicado;
  - acción de asistente y endpoint del dashboard;
  - pruebas de colisión de nombres.
  Si producto decide diferirlo, el mensaje debe dejar de ofrecer renombrar; no se puede
  prometer una acción inexistente.

### 3. Selección y errores

`resolve_selection` solo considera billeteras activas. Estados:

| Estado | Cuándo | Comportamiento |
|---|---|---|
| `selected` | Hay exactamente una activa para la moneda, o el `wallet_id` explícito es activo y coincide en moneda | Se usa sin preguntar |
| `needs_wallet_creation` | No hay activa para la moneda | Se guía a crearla antes de confirmar. **Nunca** se muestran opciones vacías |
| `needs_wallet_selection` | Defensivo: más de una activa (no debería ocurrir post-migración) | Pregunta con opciones reales |
| `archived_wallet` | El usuario nombró una billetera archivada | Se informa que está archivada, se propone la activa de esa moneda y se muestra el preview con ella; no se escribe sin confirmación |
| `invalid_selection` / `currency_mismatch` | Billetera inexistente, ajena o de otra moneda | Error explícito, sin selector |

El caso `needs_wallet_creation` está mal tratado hoy en
`app/services/assistant_capabilities/movements.py` (líneas 163–170): cualquier estado distinto
de `selected` arma `selection.options` y devuelve la pregunta de selección. Como
`needs_wallet_creation` no trae opciones, el usuario ve «Elegí una de las opciones» con una
lista vacía. El fix forma parte de este alcance:

```python
if selection.status == 'needs_wallet_creation':
    return IncompleteOperation(missing_fields=(FieldRequest(
        field='wallet_ref', reason='missing',
        question=f"No tenés una billetera en {selection.currency}. ¿La creo?"),))
if selection.status == 'needs_wallet_selection':
    ...  # opciones reales, solo legado
if selection.status != 'selected':
    raise ValueError('No pude usar esa billetera; revisá la moneda y la billetera indicadas')
```

`movement_management.py` (líneas 124–131) ya distingue ambos estados y se toma como referencia.

### 4. Billeteras archivadas

Quedan en la base con `archivada_en` por trazabilidad y rollback, pero **se ocultan** de:

- listados operativos de billeteras (`WalletService.list_wallets`);
- saldos y patrimonio (`get_balances_own_user`);
- selectores del dashboard y del asistente;
- contexto de entidades que ve el modelo (`app/services/assistant_context.py`).

No se borran ni se fusionan filas de billetera. No se reescriben movimientos anulados.

### 5. Cuentas existentes: archivar y reasignar

Para cada `(usuario_id, moneda)` con más de una billetera activa se elige una superviviente:

1. Mayor cantidad de **movimientos activos** (`anulado_en IS NULL`).
2. Si empatan, mayor cantidad de **movimientos totales**.
3. Si siguen empatando, se prioriza una billetera **creada o nombrada por el usuario** sobre
   la generada por el backfill de STK-237 (identificada por `lower(btrim(nombre)) = 'inicial'`).
4. Último desempate: `creado_en ASC, id ASC`.

Las demás se archivan, sus movimientos se reasignan a la superviviente y se registra el mapeo
individual por movimiento. La reasignación conserva moneda y dueño, por lo que la FK compuesta
sigue válida; no cambia importe, fecha, categoría ni estado de anulación. Los movimientos
anulados también se reasignan, para que la FK y el historial sigan siendo consistentes.

### 6. Trazabilidad y rollback

`billetera_fusion (origen_id, destino_id, ejecutada_en)` no alcanza para revertir: al mover
`movimientos_financieros.billetera_id` se pierde qué movimientos estaban originalmente en
`origen_id` y cuáles ya estaban en `destino_id`.

Se agrega **mapeo por movimiento**:

```sql
CREATE TABLE public.billetera_fusion_movimiento (
    movimiento_id uuid PRIMARY KEY REFERENCES public.movimientos_financieros(id),
    billetera_origen_id uuid NOT NULL REFERENCES public.billetera(id),
    billetera_destino_id uuid NOT NULL REFERENCES public.billetera(id),
    ejecutada_en timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billetera_fusion_movimiento_distinta CHECK (billetera_origen_id <> billetera_destino_id)
);
CREATE INDEX billetera_fusion_movimiento_destino_idx
    ON public.billetera_fusion_movimiento (billetera_destino_id);
```

`billetera_fusion` (nivel billetera) se conserva para auditar billeteras sin movimientos y
para el rollback de `archivada_en`. El rollback de movimientos usa exclusivamente
`billetera_fusion_movimiento`; no depende de backups externos.

### 7. Receipts y snapshots

- `MovementSnapshot` incluye `wallet_id` (`app/services/assistant_contracts.py`), y
  `snapshot()` lo toma del movimiento (`app/services/movement_commands.py`). Después del
  backfill, todo movimiento reasignado queda con el `wallet_id` de la superviviente; las
  lecturas y validaciones posteriores usan ese valor.
- `assistant_write_receipts.result` guarda el snapshot del momento de la confirmación. **No se
  reescribe**: es evidencia histórica y conserva la billetera original. La presentación actual
  se proyecta en lectura con `movement_presentation`, sin alterar el JSON persistido.
- `validate_dependencies` debe exigir además `archivada_en IS NULL`: un borrador pendiente en
  Redis que apunte a una billetera archivada falla con un mensaje claro («La billetera ya no
  está disponible») en lugar de escribir sobre una archivada, y el usuario vuelve a preparar.
- Las migraciones se ejecutan en ventana de mantenimiento; los borradores pendientes pueden
  invalidarse y eso es aceptable. Los recibos ya confirmados no se tocan.

### 8. STK-246: alcance separado

STK-246 **no se cierra como duplicada**. Cubre que el usuario nombre explícitamente su
billetera y el agente no vuelva a preguntar, y los casos de nombre desconocido o medio de pago
que no coincide con la única billetera activa:

1. Nombre de billetera activa → resolver por nombre, sin selector.
2. Nombre de billetera archivada → explicar y proponer la activa; preview para confirmar.
3. Nombre desconocido o medio de pago («Mercado Pago») que no es una billetera → preguntar
   con una frase concreta que nombre la activa («Tu billetera de pesos es “Pesos”; ¿la uso?”),
   nunca un selector vacío.
4. Si el usuario insiste en otra billetera, el camino es crearla (o renombrar), no duplicar.

Este ADR define los estados y mensajes; STK-246 conserva su tarjeta para la resolución de
lenguaje natural y sus pruebas.

## Migración (diseño, no ejecutada)

### Paso 1 — Expansión

```sql
ALTER TABLE public.billetera ADD COLUMN IF NOT EXISTS archivada_en timestamptz;
-- tablas de mapeo (ver arriba), sin tocar datos
```

### Paso 2 — Backfill idempotente

Para cada `(usuario_id, moneda)` con más de una activa:

1. Elegir superviviente con el orden del punto 5.
2. Insertar `billetera_fusion(origen, destino)`.
3. Insertar `billetera_fusion_movimiento` para cada movimiento con `billetera_id = origen`.
4. `UPDATE movimientos_financieros SET billetera_id = destino WHERE billetera_id = origen`.
5. `UPDATE billetera SET archivada_en = now() WHERE id = origen`.

Propiedades: `ON CONFLICT DO NOTHING` en el mapeo, filtros `archivada_en IS NULL`, y el
`UPDATE` es idempotente. Se puede correr dos veces sin duplicar ni perder filas.

### Paso 3 — Restricción

```sql
CREATE UNIQUE INDEX billetera_usuario_moneda_activa_uidx
    ON public.billetera (usuario_id, moneda)
    WHERE archivada_en IS NULL;
```

El modelo de SQLAlchemy debe declarar el índice con `postgresql_where` y `sqlite_where` para
que las pruebas locales también lo validen.

### Validaciones previas a la restricción

- Cero pares `(usuario_id, moneda)` con más de una activa.
- Conteos y sumas por moneda iguales antes y después: movimientos totales, activos, anulados,
  ingresos y egresos.
- Toda fila de `billetera_fusion_movimiento` apunta a un movimiento que hoy referencia a
  `billetera_destino_id`.
- Toda billetera archivada tiene una fila en `billetera_fusion` con destino activo.
- Ninguna FK compuesta rota.
- La cantidad de filas mapeadas coincide con la cantidad de movimientos reasignados.

### Rollback determinista

```sql
DROP INDEX IF EXISTS billetera_usuario_moneda_activa_uidx;

UPDATE public.movimientos_financieros m
SET billetera_id = f.billetera_origen_id
FROM public.billetera_fusion_movimiento f
WHERE m.id = f.movimiento_id
  AND m.billetera_id = f.billetera_destino_id;  -- no pisa ediciones posteriores

UPDATE public.billetera b
SET archivada_en = NULL
FROM public.billetera_fusion f
WHERE b.id = f.origen_id;
```

Las tablas de mapeo se conservan como auditoría; si producto pide limpiarlas, se hace en un
paso separado y explícito. El rollback deja el estado anterior: varias activas por moneda
(esperado), movimientos con su billetera original y la restricción eliminada.

## Consecuencias

**Positivas**

- Registrar deja de preguntar billetera: menos fricción y menos estados ambiguos.
- Saldos y patrimonio por moneda son deterministas.
- La unicidad queda garantizada por la base, no por una convención del servicio.
- El rollback es auditable y no depende de backups.

**Costos y riesgos**

- Migración de datos con reasignación; requiere ventana y validaciones.
- Borradores pendientes que apunten a billeteras archivadas se invalidan (mensaje claro).
- Hay que revisar **todas** las lecturas de billetera para filtrar archivadas; una consulta
  olvidada mostraría una billetera que ya no se puede usar.
- Renombrar entra al alcance para que el rechazo de duplicado tenga una salida real.
- El desempate por «Inicial» usa el nombre como heurística; si en el futuro se renombra, el
  criterio debe migrar a una marca explícita (columna `origen`) o congelarse antes.

## Impacto por capa

| Capa | Cambio |
|---|---|
| Esquema | `archivada_en`, `billetera_fusion`, `billetera_fusion_movimiento`, índice único parcial |
| `WalletService` | `create_wallet` rechaza duplicado activo; defaults de nombre; `rename_wallet`; `list_wallets` solo activas |
| `resolve_selection` | Filtra archivadas; estados `archived_wallet`, `needs_wallet_creation` explícito; sin opciones vacías |
| `finance.py` | Registro y actualización usan la resolución nueva; rechazan archivadas |
| `movements.py` | Rama explícita para `needs_wallet_creation`; nunca arma opciones vacías |
| `movement_management.py` | Alineado a los mismos estados y mensajes |
| `movement_commands.validate_dependencies` | Exige billetera activa; borradores viejos fallan claro |
| `assistant_context.py` | Solo billeteras activas en el contexto del modelo |
| Dashboard | Oculta archivadas; alta con rechazo; renombrar |
| Recibos | Sin cambios de esquema ni reescritura; snapshot histórico inmutable |
| Pruebas | Servicio, selección, migración idempotente, rollback, comandos, E2E y staging |

## Plan de pruebas

1. **Servicio**: crear segunda activa → rechazo sin escritura; defaults «Pesos»/«Dólares»;
   renombrar con colisión; listar solo activas.
2. **Selección**: una activa → `selected`; sin activas → `needs_wallet_creation`; archivada
   explícita → `archived_wallet`; nombre desconocido → pregunta con la activa, sin opciones
   vacías.
3. **Migración**: correr dos veces (idempotencia); duplicados resueltos; conteos y sumas
   preservados; mapeo completo; rollback restaura la billetera original de cada movimiento.
4. **Comandos**: editar/anular después del backfill usa la superviviente; borrador con
   billetera archivada se rechaza.
5. **E2E**: registrar con una sola billetera no pregunta; nombrar la billetera resuelve;
   dashboard no lista archivadas.
6. **Staging**: la conversación de humo deja de recibir la pregunta de billetera.

## Alcance revisable de STK-258

### Incluye

- Este ADR.
- Migraciones: expansión, backfill y restricción, con validaciones y rollback.
- Contrato de creación/nombres/renombre y rechazo de duplicados.
- Resolución de billetera y estados nuevos.
- Filtrado de archivadas en servicio, contexto, saldos, patrimonio y dashboard.
- Fix de `needs_wallet_creation` en `movements.py`.
- Pruebas y documentación de usuario.

### No incluye

- Monedas distintas de ARS/USD.
- Fusión entre monedas o conversión de saldos.
- Reescritura de recibos históricos.
- Cierre de STK-246 (se implementa coordinado, conserva su tarjeta).
- Borrado físico de billeteras o de las tablas de mapeo.

### Criterios de aceptación

- No se puede crear una segunda billetera activa por moneda; el rechazo no escribe y nombra
  la activa.
- Ningún registro pregunta billetera cuando hay una sola activa.
- Las archivadas no aparecen en listados, saldos, selectores ni contexto del modelo.
- La migración deja cero pares activos duplicados y conserva conteos, sumas y moneda.
- El rollback revierte exactamente los movimientos mapeados sin tocar los posteriores.
- Un borrador que apunta a una billetera archivada falla con mensaje claro.
- La suite completa y el E2E de staging quedan en verde.

### Decisiones abiertas

1. ¿Renombrar entra en esta entrega o se difiere y se ajusta el mensaje?
2. ¿Se conserva `billetera_fusion` a nivel billetera (recomendado, para billeteras sin
   movimientos) o alcanza solo el mapeo por movimiento?
3. ¿Se acepta invalidar borradores pendientes durante la migración?
4. ¿La heurística «Inicial» es suficiente o se agrega una marca explícita de origen?
5. ¿Las archivadas quedan accesibles en una vista de auditoría o solo por SQL?
