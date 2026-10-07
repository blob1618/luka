-- ============================================================================
-- ARNES DE PRUEBAS LOCAL DESECHABLE: STK-237 (Billetera y FK Compuesta)
-- Ejecutar exclusivamente en base PostgreSQL LOCAL DESECHABLE Supabase
-- migrada exactamente hasta 20260921045538 (sin aplicar aun 20261006234640).
--
-- COMANDO EXIGIDO:
-- psql -h 127.0.0.1 -p 54322 -U postgres -d postgres -v disposable_confirm=STK237_SYNTHETIC -f supabase/tests/stk237_wallets.sql
-- ============================================================================

\set ON_ERROR_STOP on

-- 0. Verificacion de confirmacion de base desechable via variable psql
\if :{?disposable_confirm}
\else
    \echo 'ERROR: El arnes requiere confirmacion explicita via -v disposable_confirm=STK237_SYNTHETIC'
    \echo 'Comando: psql -h 127.0.0.1 -p 54322 -U postgres -d postgres -v disposable_confirm=STK237_SYNTHETIC -f supabase/tests/stk237_wallets.sql'
    DO $$ BEGIN RAISE EXCEPTION 'STK-237 GUARD: Missing disposable_confirm'; END $$;
\endif

-- psql does not interpolate variables inside dollar-quoted DO bodies.
SELECT :'disposable_confirm' = 'STK237_SYNTHETIC' AS disposable_ok \gset
\if :disposable_ok
\else
    \echo 'ERROR: disposable_confirm must equal STK237_SYNTHETIC'
    DO $$ BEGIN RAISE EXCEPTION 'STK-237 GUARD: Invalid disposable_confirm'; END $$;
\endif

-- 1. Guardias de entorno y rol administrador antes de insertar datos
DO $$
DECLARE
    v_is_super BOOLEAN;
BEGIN
    SELECT rolsuper INTO v_is_super FROM pg_roles WHERE rolname = current_user;
    IF NOT v_is_super AND NOT pg_has_role(current_user, 'postgres', 'MEMBER') THEN
        RAISE EXCEPTION 'STK-237 GUARD: Se requiere ejecutar como rol admin o postgres';
    END IF;

    -- La base debe estar vacia de movimientos previos
    IF (SELECT COUNT(*) FROM public.movimientos_financieros) > 0 THEN
        RAISE EXCEPTION 'STK-237 GUARD: public.movimientos_financieros debe estar vacia para este arnes sintetico';
    END IF;

    -- La tabla billetera no debe existir aun
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'billetera'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: La tabla public.billetera ya existe. El arnes requiere base migrada solo hasta 20260921045538';
    END IF;

    -- La tabla movimientos_financieros y columna anulado_en deben existir
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'movimientos_financieros'
          AND column_name = 'anulado_en'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: Columna anulado_en no existe en movimientos_financieros. Falta migracion base';
    END IF;

    -- La columna billetera_id no debe existir aun
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'movimientos_financieros'
          AND column_name = 'billetera_id'
    ) THEN
        RAISE EXCEPTION 'STK-237 GUARD: La columna billetera_id ya existe en movimientos_financieros';
    END IF;

    -- Require the exact prior migration head, before changing fixture data.
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'supabase_migrations' AND table_name = 'schema_migrations'
    ) THEN
        IF (SELECT max(version) FROM supabase_migrations.schema_migrations)
                IS DISTINCT FROM '20260921045538' THEN
            RAISE EXCEPTION 'STK-237 GUARD: Expected migration head 20260921045538';
        END IF;
    ELSE
        RAISE EXCEPTION 'STK-237 GUARD: Supabase local migration history is required';
    END IF;
END $$;

-- 2. Insertar fixtures deterministas (3 usuarios sinteticos: u1, u2 y u3 sin movimientos)
INSERT INTO public.usuario (id, nombre, email)
VALUES
    ('11111111-1111-4111-8111-111111111111', 'Usuario Uno STK237', 'u1_stk237@synthetic.local'),
    ('22222222-2222-4222-8222-222222222222', 'Usuario Dos STK237', 'u2_stk237@synthetic.local'),
    ('33333333-3333-4333-8333-333333333333', 'Usuario Tres STK237 (Sin Movs)', 'u3_stk237@synthetic.local');

-- Movimientos sinteticos: ARS, USD, espacios ('  XTS  '), moneda vacia (''), precision arbitraria, activo/anulado
INSERT INTO public.movimientos_financieros (
    id, usuario_id, tipo, cantidad, moneda, descripcion, fecha_movimiento, origen, whatsapp_message_id, creado_en, actualizado_en, anulado_en
) VALUES
    ('aaaaaaa1-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111', 'egreso', 12345678901234567890.1234567890, 'ARS', 'Gasto super', '2026-10-01', 'whatsapp_text', 'wamid.stk237.1', '2026-10-01 10:00:00+00', '2026-10-01 10:00:00+00', NULL),
    ('aaaaaaa2-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111', 'ingreso', 0.0000000001, 'USD', 'Cobro crypto', '2026-10-02', 'manual', NULL, '2026-10-02 11:00:00+00', '2026-10-02 11:05:00+00', '2026-10-02 12:00:00+00'),
    ('bbbbbbb1-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222', 'egreso', 500.50, 'ARS', 'Transporte', '2026-10-03', 'whatsapp_text', 'wamid.stk237.2', '2026-10-03 09:00:00+00', '2026-10-03 09:00:00+00', NULL),
    ('bbbbbbb2-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222', 'ingreso', 999999999999999999999999999999.99999999, '  XTS  ', NULL, '2026-10-04', 'import', NULL, '2026-10-04 15:00:00+00', '2026-10-04 15:00:00+00', NULL),
    ('bbbbbbb3-2222-4222-8222-222222222222', '22222222-2222-4222-8222-222222222222', 'egreso', 50.25, '', 'Moneda vacia historica', '2026-10-04', 'manual', NULL, '2026-10-04 16:00:00+00', '2026-10-04 16:00:00+00', '2026-10-04 17:00:00+00');

-- Deterministic, owned category: cover non-null and null fields before snapshot.
INSERT INTO public.categorias (id, usuario_id, nombre)
VALUES ('eeeeeeee-1111-4111-8111-111111111111',
        '11111111-1111-4111-8111-111111111111', 'Categoria sintetica STK237');
UPDATE public.movimientos_financieros
SET categoria_id = 'eeeeeeee-1111-4111-8111-111111111111'
WHERE id = 'aaaaaaa1-1111-4111-8111-111111111111';

-- 3. Snapshot temporal en sesion psql (sobrevive COMMIT de la migracion)
CREATE TEMP TABLE _stk237_arnes_snapshot AS
SELECT
    m.id,
    to_jsonb(m) AS data_before
FROM public.movimientos_financieros m;

-- 4. Ejecutar la migracion bajo prueba desde la raiz del checkout
\i supabase/migrations/20261006234640_add_wallets_stk237.sql

-- 5. Verificacion de preservacion estricta post-migracion (EXCEPT bidireccional)
DO $$
DECLARE
    v_diff_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_diff_count
    FROM (
        SELECT id, data_before FROM _stk237_arnes_snapshot
        EXCEPT
        SELECT id, to_jsonb(m) - 'billetera_id' FROM public.movimientos_financieros m
    ) diff_a;
    IF v_diff_count > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % diferencias en snapshot pre vs post (EXCEPT pre-post)', v_diff_count;
    END IF;

    SELECT COUNT(*) INTO v_diff_count
    FROM (
        SELECT id, to_jsonb(m) - 'billetera_id' FROM public.movimientos_financieros m
        EXCEPT
        SELECT id, data_before FROM _stk237_arnes_snapshot
    ) diff_b;
    IF v_diff_count > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % diferencias en snapshot post vs pre (EXCEPT post-pre)', v_diff_count;
    END IF;
END $$;

-- 6. Verificacion de catalogo, join exacto y no creacion espuria para usuario sin movimientos
DO $$
DECLARE
    v_u3_count INTEGER;
    v_total_wallets INTEGER;
    v_bad_joins INTEGER;
    v_initial_diff INTEGER;
BEGIN
    -- Cada movimiento debe estar unido con la billetera exacta de su usuario y moneda
    SELECT COUNT(*) INTO v_bad_joins
    FROM public.movimientos_financieros m
    JOIN public.billetera b ON b.id = m.billetera_id
    WHERE b.usuario_id <> m.usuario_id
       OR b.moneda <> m.moneda
       OR b.nombre <> 'Inicial';

    IF v_bad_joins > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: % movimientos con join a billetera incompatible', v_bad_joins;
    END IF;

    -- Exactamente una billetera Inicial por pareja (usuario_id, moneda) con movimientos
    SELECT COUNT(*) INTO v_initial_diff
    FROM (
        SELECT usuario_id, moneda FROM public.movimientos_financieros GROUP BY usuario_id, moneda
        EXCEPT
        SELECT usuario_id, moneda FROM public.billetera WHERE nombre = 'Inicial'
    ) diff_pairs;

    IF v_initial_diff > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: Hay parejas (usuario_id, moneda) sin billetera Inicial';
    END IF;

    -- Usuario 3 sin movimientos no debe tener ninguna billetera
    SELECT COUNT(*) INTO v_u3_count
    FROM public.billetera
    WHERE usuario_id = '33333333-3333-4333-8333-333333333333';
    IF v_u3_count <> 0 THEN
        RAISE EXCEPTION 'TEST FAILED: Usuario 3 sin movimientos recibio billetera espuria: %', v_u3_count;
    END IF;

    -- Total de parejas esperadas: U1(ARS, USD)=2; U2(ARS, '  XTS  ', '')=3 => total 5
    SELECT COUNT(*) INTO v_total_wallets FROM public.billetera;
    IF v_total_wallets <> 5 THEN
        RAISE EXCEPTION 'TEST FAILED: Total de billeteras esperado 5, encontrado %', v_total_wallets;
    END IF;
END $$;

-- 7. Pruebas de restricciones de integridad (SAVEPOINTS y excepciones controladas)
BEGIN;

-- 7.a. Insercion valida de segunda billetera en misma moneda y movimiento en ella
DO $$
DECLARE
    v_new_wallet_id UUID := '44444444-4444-4444-8444-444444444444';
    v_new_mov_id UUID := 'cccccccc-4444-4444-8444-444444444444';
BEGIN
    INSERT INTO public.billetera (id, usuario_id, nombre, moneda)
    VALUES (v_new_wallet_id, '11111111-1111-4111-8111-111111111111', 'Efectivo', 'ARS');

    INSERT INTO public.movimientos_financieros (
        id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
    ) VALUES (
        v_new_mov_id, '11111111-1111-4111-8111-111111111111', v_new_wallet_id, 'egreso', 75.00, 'ARS', '2026-10-05', 'manual'
    );
END $$;

-- 7.b. Rechazo de nombre vacio o solo espacios
DO $$
BEGIN
    BEGIN
        INSERT INTO public.billetera (usuario_id, nombre, moneda)
        VALUES ('11111111-1111-4111-8111-111111111111', '   ', 'ARS');
        RAISE EXCEPTION 'TEST FAILED: Insercion de nombre solo espacios debio fallar';
    EXCEPTION
        WHEN check_violation THEN
            -- Esperado por billetera_nombre_no_vacio_check
    END;
END $$;

-- 7.c. Rechazo de nombre duplicado normalizado lower(trim(nombre)) para mismo usuario y moneda
DO $$
BEGIN
    BEGIN
        INSERT INTO public.billetera (usuario_id, nombre, moneda)
        VALUES ('11111111-1111-4111-8111-111111111111', '  efectivo  ', 'ARS');
        RAISE EXCEPTION 'TEST FAILED: Insercion de nombre duplicado normalizado debio fallar';
    EXCEPTION
        WHEN unique_violation THEN
            -- Esperado por billetera_usuario_moneda_nombre_uidx
    END;
END $$;

-- 7.d. Rechazo de foreign owner (movimiento de usuario 1 con billetera de usuario 2)
DO $$
DECLARE
    v_w_u2 UUID;
BEGIN
    SELECT id INTO v_w_u2 FROM public.billetera WHERE usuario_id = '22222222-2222-4222-8222-222222222222' AND moneda = 'ARS' LIMIT 1;
    BEGIN
        INSERT INTO public.movimientos_financieros (
            id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
        ) VALUES (
            gen_random_uuid(), '11111111-1111-4111-8111-111111111111', v_w_u2, 'egreso', 50.00, 'ARS', '2026-10-05', 'manual'
        );
        RAISE EXCEPTION 'TEST FAILED: Foreign owner debio fallar por FK compuesta';
    EXCEPTION
        WHEN foreign_key_violation THEN
            -- Esperado por movimientos_financieros_billetera_fkey
    END;
END $$;

-- 7.e. Rechazo de foreign currency (movimiento en ARS con billetera en USD)
DO $$
DECLARE
    v_w_usd UUID;
BEGIN
    SELECT id INTO v_w_usd FROM public.billetera WHERE usuario_id = '11111111-1111-4111-8111-111111111111' AND moneda = 'USD' LIMIT 1;
    BEGIN
        INSERT INTO public.movimientos_financieros (
            id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
        ) VALUES (
            gen_random_uuid(), '11111111-1111-4111-8111-111111111111', v_w_usd, 'egreso', 50.00, 'ARS', '2026-10-05', 'manual'
        );
        RAISE EXCEPTION 'TEST FAILED: Foreign currency debio fallar por FK compuesta';
    EXCEPTION
        WHEN foreign_key_violation THEN
            -- Esperado por movimientos_financieros_billetera_fkey
    END;
END $$;

-- 7.f. Rechazo de billetera_id nulo
DO $$
BEGIN
    BEGIN
        INSERT INTO public.movimientos_financieros (
            id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
        ) VALUES (
            gen_random_uuid(), '11111111-1111-4111-8111-111111111111', NULL, 'egreso', 50.00, 'ARS', '2026-10-05', 'manual'
        );
        RAISE EXCEPTION 'TEST FAILED: billetera_id NULL debio fallar por restriccion NOT NULL';
    EXCEPTION
        WHEN not_null_violation THEN
            -- Esperado por NOT NULL
    END;
END $$;

ROLLBACK;

-- 8. Verificacion de RLS y ACL en public.billetera
DO $$
DECLARE
    v_rls BOOLEAN;
    v_public_privs INTEGER;
    r RECORD;
    p RECORD;
BEGIN
    SELECT relrowsecurity INTO v_rls
    FROM pg_class
    WHERE oid = 'public.billetera'::regclass;

    IF NOT v_rls THEN
        RAISE EXCEPTION 'TEST FAILED: RLS no esta habilitada en public.billetera';
    END IF;

    -- Verificar que PUBLIC (grantee = 0) no tenga ningun privilegio en relacl
    SELECT COUNT(*) INTO v_public_privs
    FROM pg_class c,
         aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
    WHERE c.oid = 'public.billetera'::regclass
      AND a.grantee = 0;

    IF v_public_privs > 0 THEN
        RAISE EXCEPTION 'TEST FAILED: PUBLIC (grantee=0) conserva % privilegios en relacl', v_public_privs;
    END IF;

    -- Verificar has_table_privilege para anon y authenticated en SELECT, INSERT, UPDATE, DELETE
    FOR r IN (SELECT unnest(ARRAY['anon', 'authenticated']) AS rol) LOOP
        FOR p IN (SELECT unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS priv) LOOP
            IF has_table_privilege(r.rol, 'public.billetera', p.priv) THEN
                RAISE EXCEPTION 'TEST FAILED: % conserva privilegio % en public.billetera', r.rol, p.priv;
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- 9. Verificacion con SET LOCAL ROLE de denegacion efectiva (SELECT, INSERT, UPDATE, DELETE)
BEGIN;
DO $$
DECLARE
    v_roles TEXT[] := ARRAY['anon', 'authenticated'];
    v_role TEXT;
BEGIN
    FOREACH v_role IN ARRAY v_roles LOOP
        -- SELECT
        EXECUTE format('SET LOCAL ROLE %I', v_role);
        BEGIN
            PERFORM * FROM public.billetera;
            RAISE EXCEPTION 'TEST FAILED: % pudo ejecutar SELECT en public.billetera', v_role;
        EXCEPTION
            WHEN insufficient_privilege THEN
        END;
        RESET ROLE;

        -- INSERT
        EXECUTE format('SET LOCAL ROLE %I', v_role);
        BEGIN
            INSERT INTO public.billetera (id, usuario_id, nombre, moneda)
            VALUES ('ffffffff-1111-4111-8111-111111111111', '11111111-1111-4111-8111-111111111111', 'Test Valido', 'ARS');
            RAISE EXCEPTION 'TEST FAILED: % pudo ejecutar INSERT en public.billetera', v_role;
        EXCEPTION
            WHEN insufficient_privilege THEN
        END;
        RESET ROLE;

        -- UPDATE
        EXECUTE format('SET LOCAL ROLE %I', v_role);
        BEGIN
            UPDATE public.billetera SET nombre = 'Modificado' WHERE id = 'ffffffff-1111-4111-8111-111111111111';
            RAISE EXCEPTION 'TEST FAILED: % pudo ejecutar UPDATE en public.billetera', v_role;
        EXCEPTION
            WHEN insufficient_privilege THEN
        END;
        RESET ROLE;

        -- DELETE
        EXECUTE format('SET LOCAL ROLE %I', v_role);
        BEGIN
            DELETE FROM public.billetera WHERE id = 'ffffffff-1111-4111-8111-111111111111';
            RAISE EXCEPTION 'TEST FAILED: % pudo ejecutar DELETE en public.billetera', v_role;
        EXCEPTION
            WHEN insufficient_privilege THEN
        END;
        RESET ROLE;
    END LOOP;
END $$;
ROLLBACK;

SELECT 'STK-237 HARNESS: Todas las aserciones completadas exitosamente en base PostgreSQL desechable.' AS resultado;
