-- ============================================================================
-- HARNESS PostgreSQL LOCAL DESCARTABLE - STK-237
-- Billetera + billetera_id nullable (fase de expansion) y backfill historico.
--
-- Ejecutar EXCLUSIVAMENTE contra una base PostgreSQL local descartable de Supabase
-- (imagen supabase/postgres 17) migrada exactamente hasta
-- 20260921045538_recurring_confirmation_delivery.sql, es decir SIN las migraciones
-- nuevas de STK-237. Nunca contra Supabase remoto ni produccion.
--
-- Aplica, en orden, los tres archivos nuevos de STK-237: expansion, backfill e
-- indice (cada uno en su propia transaccion, como los aplica Supabase CLI).
--
-- project_id unico: copiar `supabase/` a un directorio desechable y fijar alli un
-- `project_id` propio (por ejemplo stk237harness<uuid corto>) antes de
-- `supabase start`/`db start`; usar ese workdir para `db reset` y `stop`. Asi el
-- harness no toca contenedores de otros worktrees (ej. supabase_db_luka).
--
-- COMANDO EXIGIDO (desde la raiz de este checkout, con el archivo accesible):
--   psql -h 127.0.0.1 -p <puerto> -U postgres -d postgres \
--     -v disposable_confirm=STK237_SYNTHETIC \
--     -f supabase/tests/stk237_billetera.sql
--
-- Cubre: cero movimientos, una moneda, ARS/USD, moneda historica no normalizada,
-- movimientos anulados, igualdad de campos antes/despues, FK de propietario y
-- moneda, billetera_id NULL permitido durante la expansion, escritura del binario
-- anterior posterior a la expansion, idempotencia del backfill, indice en archivo
-- posterior, RLS y permisos de anon/authenticated.
-- ============================================================================

\set ON_ERROR_STOP on

-- 0. Guardia de base descartable: exige confirmacion explicita por variable psql.
\if :{?disposable_confirm}
\else
    \echo 'ERROR: falta -v disposable_confirm=STK237_SYNTHETIC'
    DO $$ BEGIN RAISE EXCEPTION 'STK-237 GUARD: falta disposable_confirm'; END $$;
\endif

SELECT :'disposable_confirm' = 'STK237_SYNTHETIC' AS disposable_ok \gset
\if :disposable_ok
\else
    \echo 'ERROR: disposable_confirm debe ser exactamente STK237_SYNTHETIC'
    DO $$ BEGIN RAISE EXCEPTION 'STK-237 GUARD: disposable_confirm invalido'; END $$;
\endif

-- 1. Guardias de entorno y estado previo esperado.
DO $$
DECLARE
    v_is_super BOOLEAN;
BEGIN
    SELECT rolsuper INTO v_is_super FROM pg_roles WHERE rolname = current_user;
    IF NOT v_is_super AND NOT pg_has_role(current_user, 'postgres', 'MEMBER') THEN
        RAISE EXCEPTION 'STK-237 GUARD: se requiere un rol administrador';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
        HAVING count(*) = 2
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: faltan los roles anon/authenticated de Supabase';
    END IF;

    IF (SELECT count(*) FROM public.movimientos_financieros) > 0 THEN
        RAISE EXCEPTION 'STK-237 GUARD: public.movimientos_financieros debe estar vacia';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'billetera'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: public.billetera ya existe; usar una base sin STK-237';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'movimientos_financieros'
          AND column_name = 'billetera_id'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: billetera_id ya existe; usar una base sin STK-237';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'movimientos_financieros_billetera_id_idx'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: el indice de billetera_id ya existe; usar una base sin STK-237';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'movimientos_financieros'
          AND column_name = 'anulado_en'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: falta anulado_en; migrar hasta 20260921045538';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'supabase_migrations' AND table_name = 'schema_migrations'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: se requiere el historial de migraciones de Supabase local';
    END IF;

    IF (SELECT max(version) FROM supabase_migrations.schema_migrations)
            IS DISTINCT FROM '20260921045538' THEN
        RAISE EXCEPTION 'STK-237 GUARD: se esperaba head 20260921045538';
    END IF;
END $$;

-- 2. Fixtures deterministas. u3 no tiene movimientos y no debe recibir billetera.
INSERT INTO public.usuario (id, nombre, email, whatsapp_id)
VALUES
    ('11111111-1111-4111-8111-111111111111', 'Usuario Uno STK237', 'u1_stk237@synthetic.local', '5491100000001'),
    ('22222222-2222-4222-8222-222222222222', 'Usuario Dos STK237', 'u2_stk237@synthetic.local', '5491100000002'),
    ('33333333-3333-4333-8333-333333333333', 'Usuario Tres STK237 sin movimientos', 'u3_stk237@synthetic.local', '5491100000003');

INSERT INTO public.categorias (id, usuario_id, nombre)
VALUES ('eeeeeeee-1111-4111-8111-111111111111',
        '11111111-1111-4111-8111-111111111111', 'Categoria sintetica STK237');

INSERT INTO public.movimientos_financieros (
    id, usuario_id, categoria_id, tipo, cantidad, moneda, descripcion,
    fecha_movimiento, origen, whatsapp_message_id, creado_en, actualizado_en, anulado_en
) VALUES
    ('aaaaaaa1-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111',
     'eeeeeeee-1111-4111-8111-111111111111', 'egreso', 12345678901234567890.1234567890, 'ARS',
     'Gasto con precision alta', '2026-10-01', 'whatsapp_text', 'wamid.stk237.1',
     '2026-10-01 10:00:00+00', '2026-10-01 10:00:00+00', NULL),
    ('aaaaaaa2-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111',
     NULL, 'ingreso', 0.0000000001, 'USD', 'Cobro anulado', '2026-10-02', 'manual',
     NULL, '2026-10-02 11:00:00+00', '2026-10-02 11:05:00+00', '2026-10-02 12:00:00+00'),
    ('bbbbbbb1-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222',
     NULL, 'egreso', 500.50, 'ARS', 'Transporte', '2026-10-03', 'whatsapp_text',
     'wamid.stk237.2', '2026-10-03 09:00:00+00', '2026-10-03 09:00:00+00', NULL),
    ('bbbbbbb2-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222',
     NULL, 'ingreso', 999999999999999999999999999999.99999999, '  xts  ',
     'Moneda historica no normalizada', '2026-10-04', 'import', NULL,
     '2026-10-04 15:00:00+00', '2026-10-04 15:00:00+00', NULL);

-- 3. Snapshot de sesion previo a la expansion (sobrevive a los COMMIT/autocommit).
CREATE TEMP TABLE _stk237_before AS
SELECT m.id, to_jsonb(m) AS data_before
FROM public.movimientos_financieros AS m;

-- 4. Migracion de expansion bajo prueba (desde la raiz del checkout).
BEGIN;
\i supabase/migrations/20261012174855_add_billetera_expansion_stk237.sql
COMMIT;

-- 5. Post-expansion: esquema, nullable, NOT VALID, RLS/ACL y cero backfill.
DO $$
DECLARE
    v_rls BOOLEAN;
    v_not_null BOOLEAN;
    v_is_nullable TEXT;
    v_wallets INTEGER;
    v_filled INTEGER;
    v_fk_validated BOOLEAN;
    v_public_privs INTEGER;
    r RECORD;
    p RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'billetera'
    ) THEN
        RAISE EXCEPTION 'TEST FAILED: la expansion no creo public.billetera';
    END IF;

    SELECT relrowsecurity INTO v_rls FROM pg_class WHERE oid = 'public.billetera'::regclass;
    IF NOT v_rls THEN
        RAISE EXCEPTION 'TEST FAILED: RLS no esta habilitada en public.billetera';
    END IF;

    SELECT count(*) INTO v_public_privs
    FROM pg_class c,
         aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
    WHERE c.oid = 'public.billetera'::regclass AND a.grantee = 0;
    IF v_public_privs > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: PUBLIC conserva % privilegios en public.billetera', v_public_privs;
    END IF;

    FOR r IN SELECT unnest(ARRAY['anon', 'authenticated']) AS rol LOOP
        FOR p IN SELECT unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS priv LOOP
            IF has_table_privilege(r.rol, 'public.billetera', p.priv) THEN
                RAISE EXCEPTION 'TEST FAILED: % conserva privilegio % en public.billetera', r.rol, p.priv;
            END IF;
        END LOOP;
    END LOOP;

    SELECT a.attnotnull INTO v_not_null
    FROM pg_attribute a
    WHERE a.attrelid = 'public.movimientos_financieros'::regclass
      AND a.attname = 'billetera_id'
      AND NOT a.attisdropped;
    IF v_not_null IS NULL THEN
        RAISE EXCEPTION 'TEST FAILED: la expansion no agrego la columna billetera_id';
    END IF;
    IF v_not_null THEN
        RAISE EXCEPTION 'TEST FAILED: billetera_id quedo NOT NULL y debe seguir nullable';
    END IF;

    SELECT is_nullable INTO v_is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'movimientos_financieros'
      AND column_name = 'billetera_id';
    IF v_is_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION 'TEST FAILED: information_schema reporta billetera_id como %', v_is_nullable;
    END IF;

    SELECT convalidated INTO v_fk_validated
    FROM pg_constraint
    WHERE conrelid = 'public.movimientos_financieros'::regclass
      AND conname = 'movimientos_financieros_billetera_fkey';
    IF v_fk_validated IS NULL THEN
        RAISE EXCEPTION 'TEST FAILED: falta la FK compuesta movimientos_financieros_billetera_fkey';
    END IF;
    IF v_fk_validated THEN
        RAISE EXCEPTION 'TEST FAILED: la FK deberia agregarse NOT VALID en la expansion';
    END IF;

    IF position('billetera_id' IN pg_get_constraintdef(
        (SELECT oid FROM pg_constraint WHERE conname = 'movimientos_financieros_billetera_fkey')
    )) = 0 THEN
        RAISE EXCEPTION 'TEST FAILED: la FK no referencia billetera_id';
    END IF;

    SELECT count(*) INTO v_wallets FROM public.billetera;
    IF v_wallets <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: la expansion no debe backfillear (billeteras=%)', v_wallets;
    END IF;

    SELECT count(*) INTO v_filled
    FROM public.movimientos_financieros WHERE billetera_id IS NOT NULL;
    IF v_filled <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: la expansion no debe completar billetera_id (filas=%)', v_filled;
    END IF;
END $$;

-- 6. Escritura del binario anterior DESPUES de la expansion: sin billetera_id.
--    a) pareja existente (u1, ARS); b) pareja nueva (u2, EUR). Debe aceptarse.
INSERT INTO public.movimientos_financieros (
    id, usuario_id, tipo, cantidad, moneda, descripcion, fecha_movimiento, origen
) VALUES
    ('ccccccc1-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111',
     'egreso', 10.00, 'ARS', 'Escritura legacy post-expansion', '2026-10-05', 'whatsapp_text'),
    ('ccccccc2-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222',
     'ingreso', 20.00, 'EUR', 'Pareja nueva post-expansion', '2026-10-05', 'whatsapp_text');

-- 6.b. Snapshot completo inmediatamente antes del backfill (incluye las escrituras
-- legacy y excluye billetera_id, que es lo unico que el backfill puede modificar).
CREATE TEMP TABLE _stk237_expected AS
SELECT m.id, to_jsonb(m) - 'billetera_id' AS data_before
FROM public.movimientos_financieros AS m;

-- 7. Migracion de backfill historico bajo prueba.
BEGIN;
\i supabase/migrations/20261012174901_backfill_billetera_historica_stk237.sql
COMMIT;

-- 7.b. Migracion del indice de billetera_id, posterior al backfill. Supabase CLI
-- aplica cada archivo en su propia transaccion, asi que el ACCESS EXCLUSIVE tomado
-- por ADD COLUMN en la expansion ya termino cuando se construye este indice.
BEGIN;
\i supabase/migrations/20261012181524_add_movimientos_billetera_id_index_stk237.sql
COMMIT;

DO $$
DECLARE
    v_index INTEGER;
BEGIN
    SELECT count(*) INTO v_index
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = 'movimientos_financieros_billetera_id_idx'
      AND i.indrelid = 'public.movimientos_financieros'::regclass
      AND i.indisvalid
      AND i.indisready
      AND i.indnkeyatts = 1;
    IF v_index <> 1 THEN
        RAISE EXCEPTION 'TEST FAILED: el indice movimientos_financieros_billetera_id_idx no quedo valido';
    END IF;

    IF position(
        'billetera_id'
        IN pg_get_indexdef('public.movimientos_financieros_billetera_id_idx'::regclass)
    ) = 0 THEN
        RAISE EXCEPTION 'TEST FAILED: el indice no esta definido sobre billetera_id';
    END IF;
END $$;

-- 8. Post-backfill: preservacion, catalogo exacto, vinculo y validacion de la FK.
DO $$
DECLARE
    v_diff INTEGER;
    v_nulls INTEGER;
    v_bad_joins INTEGER;
    v_wallets INTEGER;
    v_u3 INTEGER;
    v_pairs INTEGER;
    v_normalized_currency INTEGER;
    v_fk_validated BOOLEAN;
BEGIN
    -- 8.a. Igualdad exacta de campos financieros. (i) respecto del snapshot previo a
    -- la expansion, restringido a esas filas; (ii) respecto del snapshot completo
    -- inmediatamente anterior al backfill.
    SELECT count(*) INTO v_diff
    FROM (
        SELECT id, data_before FROM _stk237_before
        EXCEPT
        SELECT m.id, to_jsonb(m) - 'billetera_id'
        FROM public.movimientos_financieros AS m
        WHERE m.id IN (SELECT id FROM _stk237_before)
    ) pre_post;
    IF v_diff > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % filas cambiaron respecto del snapshot previo a la expansion', v_diff;
    END IF;

    SELECT count(*) INTO v_diff
    FROM (
        SELECT m.id, to_jsonb(m) - 'billetera_id'
        FROM public.movimientos_financieros AS m
        WHERE m.id IN (SELECT id FROM _stk237_before)
        EXCEPT
        SELECT id, data_before FROM _stk237_before
    ) post_pre;
    IF v_diff > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % filas del snapshot previo aparecen cambiadas o ausentes', v_diff;
    END IF;

    SELECT count(*) INTO v_diff
    FROM (
        SELECT id, data_before FROM _stk237_expected
        EXCEPT
        SELECT m.id, to_jsonb(m) - 'billetera_id'
        FROM public.movimientos_financieros AS m
    ) expected_actual;
    IF v_diff > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % filas difieren del snapshot inmediatamente previo al backfill', v_diff;
    END IF;

    SELECT count(*) INTO v_diff
    FROM (
        SELECT m.id, to_jsonb(m) - 'billetera_id'
        FROM public.movimientos_financieros AS m
        EXCEPT
        SELECT id, data_before FROM _stk237_expected
    ) actual_expected;
    IF v_diff > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % filas aparecieron o cambiaron respecto del snapshot previo al backfill', v_diff;
    END IF;

    -- 8.b. El backfill inicial completa las filas de su snapshot.
    SELECT count(*) INTO v_nulls
    FROM public.movimientos_financieros WHERE billetera_id IS NULL;
    IF v_nulls <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: quedan % movimientos sin billetera tras el backfill', v_nulls;
    END IF;

    -- 8.c. Cada movimiento apunta a una billetera del mismo propietario y moneda.
    SELECT count(*) INTO v_bad_joins
    FROM public.movimientos_financieros AS m
    JOIN public.billetera AS b ON b.id = m.billetera_id
    WHERE b.usuario_id IS DISTINCT FROM m.usuario_id
       OR b.moneda IS DISTINCT FROM m.moneda
       OR lower(btrim(b.nombre)) <> 'inicial';
    IF v_bad_joins > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % movimientos apuntan a billetera incompatible', v_bad_joins;
    END IF;

    -- 8.d. Una billetera por pareja exacta con movimientos; ARS y USD separadas.
    SELECT count(*) INTO v_pairs
    FROM (
        SELECT usuario_id, moneda FROM public.movimientos_financieros GROUP BY usuario_id, moneda
        EXCEPT
        SELECT usuario_id, moneda FROM public.billetera
    ) missing;
    IF v_pairs > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % parejas historicas sin billetera', v_pairs;
    END IF;

    SELECT count(*) INTO v_pairs
    FROM (
        SELECT usuario_id, moneda FROM public.billetera GROUP BY usuario_id, moneda
        EXCEPT
        SELECT usuario_id, moneda FROM public.movimientos_financieros GROUP BY usuario_id, moneda
    ) spurious;
    IF v_pairs > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % billeteras sin pareja historica', v_pairs;
    END IF;

    SELECT count(*) INTO v_wallets FROM public.billetera;
    IF v_wallets <> 5 THEN
        RAISE EXCEPTION 'TEST FAILED: billeteras esperadas 5 (u1 ARS/USD, u2 ARS/xts/EUR), encontradas %', v_wallets;
    END IF;

    -- 8.e. Usuario sin movimientos no recibe billetera.
    SELECT count(*) INTO v_u3
    FROM public.billetera WHERE usuario_id = '33333333-3333-4333-8333-333333333333';
    IF v_u3 <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: usuario sin movimientos recibio % billeteras', v_u3;
    END IF;

    -- 8.f. La moneda historica se conserva exacta, sin normalizar.
    SELECT count(*) INTO v_normalized_currency
    FROM public.billetera
    WHERE btrim(lower(moneda)) = 'xts' AND moneda <> '  xts  ';
    IF v_normalized_currency <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: la moneda historica se normalizo en el backfill';
    END IF;

    IF (SELECT count(*) FROM public.billetera WHERE moneda = '  xts  ') <> 1 THEN
        RAISE EXCEPTION 'TEST FAILED: la pareja con moneda historica no normalizada no se conservo exacta';
    END IF;

    -- 8.g. La FK compuesta quedo validada.
    SELECT convalidated INTO v_fk_validated
    FROM pg_constraint
    WHERE conrelid = 'public.movimientos_financieros'::regclass
      AND conname = 'movimientos_financieros_billetera_fkey';
    IF v_fk_validated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'TEST FAILED: la FK compuesta no quedo validada tras el backfill';
    END IF;
END $$;

-- 9. Idempotencia: re-ejecutar el backfill no duplica billeteras ni rompe la FK.
BEGIN;
\i supabase/migrations/20261012174901_backfill_billetera_historica_stk237.sql
COMMIT;

DO $$
DECLARE
    v_wallets INTEGER;
BEGIN
    SELECT count(*) INTO v_wallets FROM public.billetera;
    IF v_wallets <> 5 THEN
        RAISE EXCEPTION 'TEST FAILED: el backfill no es idempotente (billeteras=%)', v_wallets;
    END IF;
END $$;

-- 10. Restricciones e integridad: todo dentro de una transaccion revertida.
BEGIN;

-- 10.a. Segunda billetera valida en la misma moneda y movimiento asociado.
DO $$
DECLARE
    v_wallet_id UUID := '44444444-4444-4444-8444-444444444444';
BEGIN
    INSERT INTO public.billetera (id, usuario_id, nombre, moneda)
    VALUES (v_wallet_id, '11111111-1111-4111-8111-111111111111', 'Efectivo', 'ARS');

    INSERT INTO public.movimientos_financieros (
        id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
    ) VALUES (
        'cccccccc-4444-4444-8444-444444444444', '11111111-1111-4111-8111-111111111111',
        v_wallet_id, 'egreso', 75.00, 'ARS', '2026-10-06', 'manual'
    );
END $$;

-- 10.b. Nombre vacio rechazado.
DO $$
BEGIN
    BEGIN
        INSERT INTO public.billetera (usuario_id, nombre, moneda)
        VALUES ('11111111-1111-4111-8111-111111111111', '   ', 'ARS');
        RAISE EXCEPTION 'TEST FAILED: nombre en blanco debio fallar';
    EXCEPTION
        WHEN check_violation THEN
    END;
END $$;

-- 10.c. Nombre normalizado duplicado por usuario y moneda rechazado.
DO $$
BEGIN
    BEGIN
        INSERT INTO public.billetera (usuario_id, nombre, moneda)
        VALUES ('11111111-1111-4111-8111-111111111111', '  EFECTIVO  ', 'ARS');
        RAISE EXCEPTION 'TEST FAILED: nombre normalizado duplicado debio fallar';
    EXCEPTION
        WHEN unique_violation THEN
    END;
END $$;

-- 10.d. Movimiento de u2 con billetera ARS de u1 rechazado.
DO $$
DECLARE
    v_foreign_wallet UUID;
BEGIN
    SELECT id INTO v_foreign_wallet
    FROM public.billetera
    WHERE usuario_id = '11111111-1111-4111-8111-111111111111' AND moneda = 'ARS'
    LIMIT 1;
    BEGIN
        INSERT INTO public.movimientos_financieros (
            id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
        ) VALUES (
            'ddddddd1-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222',
            v_foreign_wallet, 'egreso', 50.00, 'ARS', '2026-10-06', 'manual'
        );
        RAISE EXCEPTION 'TEST FAILED: billetera de otro usuario debio fallar';
    EXCEPTION
        WHEN foreign_key_violation THEN
    END;
END $$;

-- 10.e. Movimiento ARS de u1 con billetera USD de u1 rechazado.
DO $$
DECLARE
    v_usd_wallet UUID;
BEGIN
    SELECT id INTO v_usd_wallet
    FROM public.billetera
    WHERE usuario_id = '11111111-1111-4111-8111-111111111111' AND moneda = 'USD'
    LIMIT 1;
    BEGIN
        INSERT INTO public.movimientos_financieros (
            id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
        ) VALUES (
            'ddddddd2-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111',
            v_usd_wallet, 'egreso', 50.00, 'ARS', '2026-10-06', 'manual'
        );
        RAISE EXCEPTION 'TEST FAILED: billetera de otra moneda debio fallar';
    EXCEPTION
        WHEN foreign_key_violation THEN
    END;
END $$;

-- 10.f. billetera_id NULL sigue permitido durante la expansion (sin completitud asumida).
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    INSERT INTO public.movimientos_financieros (
        id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
    ) VALUES (
        'ddddddd3-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111',
        NULL, 'egreso', 50.00, 'ARS', '2026-10-06', 'manual'
    );
    SELECT count(*) INTO v_count
    FROM public.movimientos_financieros
    WHERE id = 'ddddddd3-1111-4111-8111-111111111111' AND billetera_id IS NULL;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'TEST FAILED: una escritura sin billetera no quedo registrada';
    END IF;
END $$;

ROLLBACK;

-- 11. Denegacion efectiva para anon/authenticated (SELECT/INSERT/UPDATE/DELETE).
BEGIN;
DO $$
DECLARE
    v_role TEXT;
    v_priv TEXT;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
            EXECUTE format('SET LOCAL ROLE %I', v_role);
            BEGIN
                IF v_priv = 'SELECT' THEN
                    PERFORM * FROM public.billetera;
                ELSIF v_priv = 'INSERT' THEN
                    INSERT INTO public.billetera (usuario_id, nombre, moneda)
                    VALUES ('11111111-1111-4111-8111-111111111111', 'Prohibida', 'ARS');
                ELSIF v_priv = 'UPDATE' THEN
                    UPDATE public.billetera SET nombre = 'Prohibida';
                ELSE
                    DELETE FROM public.billetera;
                END IF;
                RAISE EXCEPTION 'TEST FAILED: % pudo ejecutar % en public.billetera', v_role, v_priv;
            EXCEPTION
                WHEN insufficient_privilege THEN
            END;
            RESET ROLE;
        END LOOP;
    END LOOP;
END $$;
ROLLBACK;

-- 12. Backfill transaccional con cero movimientos: sin parejas historicas no debe
-- crear ninguna billetera. Solo borra las filas sinteticas de esta base descartable
-- y revierte todo al final.
BEGIN;
DELETE FROM public.movimientos_financieros;
DELETE FROM public.billetera;

\i supabase/migrations/20261012174901_backfill_billetera_historica_stk237.sql

DO $$
DECLARE
    v_wallets INTEGER;
    v_movements INTEGER;
BEGIN
    SELECT count(*) INTO v_wallets FROM public.billetera;
    SELECT count(*) INTO v_movements FROM public.movimientos_financieros;
    IF v_movements <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: el escenario de cero movimientos no quedo vacio (filas=%)', v_movements;
    END IF;
    IF v_wallets <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: con cero movimientos el backfill creo % billeteras', v_wallets;
    END IF;
END $$;
ROLLBACK;

SELECT 'STK-237 HARNESS: todas las aserciones pasaron en base PostgreSQL local descartable.' AS resultado;
