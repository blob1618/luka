-- HU-MON-01 - contrato final de billetera_id (PostgreSQL 17 local descartable).
--
-- Preparar una base local sin Compose, enlazada solo a 127.0.0.1, y aplicar la
-- migración de Supabase CLI hasta 20261012181524 (incluye los tres archivos STK-237).
-- Luego ejecutar este mismo archivo por fases, aplicando cada migration file con
-- `supabase migration up --db-url` entre fases:
--
--   psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
--     -v disposable_confirm=STK237_SYNTHETIC -v stage=before \
--     -f supabase/tests/stk44_wallet_transition_contract.sql
--   # aplicar Migración A con Supabase CLI
--   # volver a ejecutar con -v stage=after_a
--   # aplicar Migración B con Supabase CLI
--   # volver a ejecutar con -v stage=after_b
--   # luego consultar WalletService y ejecutar -v stage=cleanup
--
-- Las dos migraciones conservan su propia transacción CLI. Este harness verifica la
-- escritura legacy posterior al backfill inicial, la protección CHECK intermedia,
-- el contrato NOT NULL, las asociaciones/campos y la idempotencia del DML.

\set ON_ERROR_STOP on

\if :{?disposable_confirm}
\else
    \echo 'ERROR: falta -v disposable_confirm=STK237_SYNTHETIC'
    DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: falta disposable_confirm'; END $$;
\endif

SELECT :'disposable_confirm' = 'STK237_SYNTHETIC' AS stk44_disposable_ok \gset
\if :stk44_disposable_ok
\else
    \echo 'ERROR: disposable_confirm debe ser exactamente STK237_SYNTHETIC'
    DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: disposable_confirm invalido'; END $$;
\endif

\if :{?stage}
\else
    \echo 'ERROR: usar -v stage=before|after_a|after_b|cleanup'
    DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: falta stage'; END $$;
\endif

SELECT :'stage' IN ('before', 'after_a', 'after_b', 'cleanup') AS stk44_stage_valid \gset
\if :stk44_stage_valid
\else
    \echo 'ERROR: stage debe ser before, after_a, after_b o cleanup'
    DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: stage invalido'; END $$;
\endif

DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 170000
       OR current_setting('server_version_num')::integer >= 180000 THEN
        RAISE EXCEPTION 'HU-MON-01 GUARD: se requiere PostgreSQL 17';
    END IF;

    IF current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'HU-MON-01 GUARD: se esperaba la base descartable postgres';
    END IF;
END $$;

SELECT :'stage' = 'before' AS stk44_before \gset
\if :stk44_before
    SELECT max(version) = '20261012181524' AS stk44_version_ok
      FROM supabase_migrations.schema_migrations \gset
    \if :stk44_version_ok
    \else
        DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: se esperaba el head STK-237 20261012181524'; END $$;
    \endif

    BEGIN;

    DO $$
    BEGIN
        IF (SELECT count(*) FROM public.movimientos_financieros) <> 0 THEN
            RAISE EXCEPTION 'HU-MON-01 GUARD: movimientos_financieros debe estar vacia';
        END IF;

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'movimientos_financieros'
               AND column_name = 'billetera_id'
               AND is_nullable <> 'YES'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 GUARD: billetera_id debe permitir NULL en expansion';
        END IF;
    END $$;

    INSERT INTO public.usuario (id, nombre, email, whatsapp_id)
    VALUES (
        'e0440000-0000-4000-8000-000000000001',
        'HU-MON-01 transition synthetic',
        'hu-mon-01-transition@synthetic.local',
        '5491111000001'
    );

    INSERT INTO public.billetera (id, usuario_id, nombre, moneda, creado_en)
    VALUES (
        'e0440000-0000-4000-8000-000000000002',
        'e0440000-0000-4000-8000-000000000001',
        'Efectivo',
        'ARS',
        '2026-10-01 08:00:00+00'
    );

    -- Primera escritura legacy tras STK-237: anulada, pero debe quedar asociada.
    INSERT INTO public.movimientos_financieros (
        id, usuario_id, billetera_id, tipo, cantidad, moneda, descripcion,
        fecha_movimiento, origen, creado_en, actualizado_en, anulado_en
    ) VALUES (
        'e0440000-0000-4000-8000-000000000003',
        'e0440000-0000-4000-8000-000000000001', NULL, 'ingreso', 500.00, 'USD',
        'preflight cancelado para idempotencia', '2026-10-01', 'manual',
        '2026-10-01 09:00:00+00', '2026-10-01 09:05:00+00', '2026-10-01 09:10:00+00'
    );

    -- Reproducir dos veces las sentencias de datos de Migración A antes del CHECK.
    -- La segunda pasada no debe duplicar billeteras ni reasociar filas.
    INSERT INTO public.billetera (usuario_id, nombre, moneda)
    SELECT DISTINCT m.usuario_id, 'Inicial', m.moneda
      FROM public.movimientos_financieros AS m
     WHERE m.billetera_id IS NULL
    ON CONFLICT DO NOTHING;

    UPDATE public.movimientos_financieros AS m
       SET billetera_id = b.id
      FROM public.billetera AS b
     WHERE m.billetera_id IS NULL
       AND b.usuario_id = m.usuario_id
       AND b.moneda = m.moneda
       AND lower(btrim(b.nombre)) = 'inicial';

    INSERT INTO public.billetera (usuario_id, nombre, moneda)
    SELECT DISTINCT m.usuario_id, 'Inicial', m.moneda
      FROM public.movimientos_financieros AS m
     WHERE m.billetera_id IS NULL
    ON CONFLICT DO NOTHING;

    UPDATE public.movimientos_financieros AS m
       SET billetera_id = b.id
      FROM public.billetera AS b
     WHERE m.billetera_id IS NULL
       AND b.usuario_id = m.usuario_id
       AND b.moneda = m.moneda
       AND lower(btrim(b.nombre)) = 'inicial';

    DO $$
    BEGIN
        IF (SELECT count(*)
              FROM public.billetera
             WHERE usuario_id = 'e0440000-0000-4000-8000-000000000001'
               AND moneda = 'USD'
               AND lower(btrim(nombre)) = 'inicial') <> 1 THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: backfill repetido duplico Inicial USD';
        END IF;

        IF NOT EXISTS (
            SELECT 1
              FROM public.movimientos_financieros AS m
              JOIN public.billetera AS b ON b.id = m.billetera_id
             WHERE m.id = 'e0440000-0000-4000-8000-000000000003'
               AND b.usuario_id = m.usuario_id
               AND b.moneda = m.moneda
               AND lower(btrim(b.nombre)) = 'inicial'
               AND m.descripcion = 'preflight cancelado para idempotencia'
               AND m.creado_en = '2026-10-01 09:00:00+00'
               AND m.actualizado_en = '2026-10-01 09:05:00+00'
               AND m.anulado_en = '2026-10-01 09:10:00+00'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: backfill repetido no preservo el movimiento anulado';
        END IF;
    END $$;

    -- Escrituras legacy tardías: deben quedar recogidas por Migración A.
    INSERT INTO public.movimientos_financieros (
        id, usuario_id, billetera_id, categoria_id, tipo, cantidad, moneda, descripcion,
        fecha_movimiento, origen, whatsapp_message_id, creado_en, actualizado_en, anulado_en
    ) VALUES
        ('e0440000-0000-4000-8000-000000000004',
         'e0440000-0000-4000-8000-000000000001', NULL, NULL, 'egreso', 125.50, 'ARS',
         'legacy ARS activo', '2026-10-02', 'manual', 'stk44-legacy-ars-active',
         '2026-10-02 10:00:00+00', '2026-10-02 10:00:00+00', NULL),
        ('e0440000-0000-4000-8000-000000000005',
         'e0440000-0000-4000-8000-000000000001', NULL, NULL, 'ingreso', 999.00, 'ARS',
         'legacy ARS anulado', '2026-10-03', 'manual', 'stk44-legacy-ars-cancelled',
         '2026-10-03 11:00:00+00', '2026-10-03 11:15:00+00', '2026-10-03 12:00:00+00'),
        ('e0440000-0000-4000-8000-000000000006',
         'e0440000-0000-4000-8000-000000000001', NULL, NULL, 'ingreso', 22.25, 'USD',
         'legacy USD activo', '2026-10-04', 'import', 'stk44-legacy-usd-active',
         '2026-10-04 14:00:00+00', '2026-10-04 14:00:00+00', NULL),
        ('e0440000-0000-4000-8000-000000000007',
         'e0440000-0000-4000-8000-000000000001',
         'e0440000-0000-4000-8000-000000000002', NULL, 'egreso', 5.00, 'ARS',
         'asociacion explicita preservada', '2026-10-05', 'manual', 'stk44-explicit-ars',
         '2026-10-05 15:00:00+00', '2026-10-05 15:10:00+00', NULL);

    COMMIT;

    SELECT 'HU-MON-01 BEFORE: fixtures tardias insertadas; historial inicial ya aplicado.' AS resultado;
\endif

SELECT :'stage' = 'after_a' AS stk44_after_a \gset
\if :stk44_after_a
    SELECT max(version) = '20261012215302' AS stk44_version_ok
      FROM supabase_migrations.schema_migrations \gset
    \if :stk44_version_ok
    \else
        DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: se esperaba Migración A 20261012215302'; END $$;
    \endif

    DO $$
    DECLARE
        v_not_null BOOLEAN;
        v_check_validated BOOLEAN;
        v_fk_validated BOOLEAN;
    BEGIN
        SELECT attnotnull INTO v_not_null
          FROM pg_attribute
         WHERE attrelid = 'public.movimientos_financieros'::regclass
           AND attname = 'billetera_id'
           AND NOT attisdropped;
        IF v_not_null IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: Migración A aplicó NOT NULL prematuramente';
        END IF;

        SELECT convalidated INTO v_check_validated
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_not_null_check'
           AND contype = 'c';
        IF v_check_validated IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: el CHECK debe quedar NOT VALID tras Migración A';
        END IF;

        SELECT convalidated INTO v_fk_validated
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_fkey'
           AND contype = 'f';
        IF v_fk_validated IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: la FK compuesta dejó de estar validada';
        END IF;

        IF EXISTS (SELECT 1 FROM public.movimientos_financieros WHERE billetera_id IS NULL) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: quedan billetera_id NULL tras Migración A';
        END IF;

        IF (SELECT count(*)
              FROM public.billetera
             WHERE usuario_id = 'e0440000-0000-4000-8000-000000000001'
               AND lower(btrim(nombre)) = 'inicial') <> 2 THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: se esperaba una Inicial por moneda exacta';
        END IF;

        IF EXISTS (
            SELECT 1
              FROM public.movimientos_financieros AS m
              LEFT JOIN public.billetera AS b ON b.id = m.billetera_id
             WHERE m.usuario_id = 'e0440000-0000-4000-8000-000000000001'
               AND (b.id IS NULL OR b.usuario_id IS DISTINCT FROM m.usuario_id
                    OR b.moneda IS DISTINCT FROM m.moneda)
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: hay billetera inexistente o moneda/propietario incorrectos';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.movimientos_financieros AS m
            JOIN public.billetera AS b ON b.id = m.billetera_id
            WHERE m.id = 'e0440000-0000-4000-8000-000000000004'
              AND m.tipo = 'egreso' AND m.cantidad = 125.50 AND m.moneda = 'ARS'
              AND m.descripcion = 'legacy ARS activo'
              AND m.fecha_movimiento = '2026-10-02' AND m.origen = 'manual'
              AND m.whatsapp_message_id = 'stk44-legacy-ars-active'
              AND m.creado_en = '2026-10-02 10:00:00+00'
              AND m.actualizado_en = '2026-10-02 10:00:00+00'
              AND m.anulado_en IS NULL AND b.nombre = 'Inicial' AND b.moneda = 'ARS'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: escritura ARS activa no se preservo/asocio';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.movimientos_financieros AS m
            JOIN public.billetera AS b ON b.id = m.billetera_id
            WHERE m.id = 'e0440000-0000-4000-8000-000000000005'
              AND m.tipo = 'ingreso' AND m.cantidad = 999.00 AND m.moneda = 'ARS'
              AND m.descripcion = 'legacy ARS anulado'
              AND m.fecha_movimiento = '2026-10-03' AND m.origen = 'manual'
              AND m.whatsapp_message_id = 'stk44-legacy-ars-cancelled'
              AND m.creado_en = '2026-10-03 11:00:00+00'
              AND m.actualizado_en = '2026-10-03 11:15:00+00'
              AND m.anulado_en = '2026-10-03 12:00:00+00'
              AND b.nombre = 'Inicial' AND b.moneda = 'ARS'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: movimiento anulado no se preservo/asocio';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.movimientos_financieros AS m
            JOIN public.billetera AS b ON b.id = m.billetera_id
            WHERE m.id = 'e0440000-0000-4000-8000-000000000006'
              AND m.tipo = 'ingreso' AND m.cantidad = 22.25 AND m.moneda = 'USD'
              AND m.descripcion = 'legacy USD activo'
              AND m.fecha_movimiento = '2026-10-04' AND m.origen = 'import'
              AND m.whatsapp_message_id = 'stk44-legacy-usd-active'
              AND m.creado_en = '2026-10-04 14:00:00+00'
              AND m.actualizado_en = '2026-10-04 14:00:00+00'
              AND m.anulado_en IS NULL AND b.nombre = 'Inicial' AND b.moneda = 'USD'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: escritura USD activa no se preservo/asocio';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.movimientos_financieros AS m
            WHERE m.id = 'e0440000-0000-4000-8000-000000000007'
              AND m.billetera_id = 'e0440000-0000-4000-8000-000000000002'
              AND m.tipo = 'egreso' AND m.cantidad = 5.00 AND m.moneda = 'ARS'
              AND m.descripcion = 'asociacion explicita preservada'
              AND m.fecha_movimiento = '2026-10-05' AND m.origen = 'manual'
              AND m.whatsapp_message_id = 'stk44-explicit-ars'
              AND m.creado_en = '2026-10-05 15:00:00+00'
              AND m.actualizado_en = '2026-10-05 15:10:00+00'
              AND m.anulado_en IS NULL
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: movimiento con asociacion explicita fue modificado';
        END IF;
    END $$;

    DO $$
    BEGIN
        BEGIN
            INSERT INTO public.movimientos_financieros (
                id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
            ) VALUES (
                'e0440000-0000-4000-8000-0000000000f1',
                'e0440000-0000-4000-8000-000000000001', NULL,
                'egreso', 1.00, 'ARS', '2026-10-06', 'manual'
            );
            RAISE EXCEPTION 'HU-MON-01 TEST: Migración A debía rechazar NULL';
        EXCEPTION WHEN check_violation THEN
            NULL;
        END;
    END $$;

    SELECT 'HU-MON-01 AFTER_A: transición, preservación y protección CHECK correctas.' AS resultado;
\endif

SELECT :'stage' = 'after_b' AS stk44_after_b \gset
\if :stk44_after_b
    SELECT max(version) = '20261012215307' AS stk44_version_ok
      FROM supabase_migrations.schema_migrations \gset
    \if :stk44_version_ok
    \else
        DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: se esperaba Migración B 20261012215307'; END $$;
    \endif

    DO $$
    DECLARE
        v_not_null BOOLEAN;
        v_fk_validated BOOLEAN;
    BEGIN
        SELECT attnotnull INTO v_not_null
          FROM pg_attribute
         WHERE attrelid = 'public.movimientos_financieros'::regclass
           AND attname = 'billetera_id'
           AND NOT attisdropped;
        IF v_not_null IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: billetera_id no quedó NOT NULL';
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid = 'public.movimientos_financieros'::regclass
               AND conname = 'movimientos_financieros_billetera_not_null_check'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: CHECK auxiliar no fue retirado';
        END IF;

        SELECT convalidated INTO v_fk_validated
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_fkey'
           AND contype = 'f';
        IF v_fk_validated IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: la FK compuesta no quedó validada';
        END IF;

        IF EXISTS (SELECT 1 FROM public.movimientos_financieros WHERE billetera_id IS NULL) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: quedaron movimientos nulos';
        END IF;

        IF EXISTS (
            SELECT 1
              FROM public.movimientos_financieros AS m
              LEFT JOIN public.billetera AS b ON b.id = m.billetera_id
             WHERE m.usuario_id = 'e0440000-0000-4000-8000-000000000001'
               AND (b.id IS NULL OR b.usuario_id IS DISTINCT FROM m.usuario_id
                    OR b.moneda IS DISTINCT FROM m.moneda)
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: billetera final con propiedad/moneda incorrectas';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.movimientos_financieros
             WHERE id = 'e0440000-0000-4000-8000-000000000007'
               AND billetera_id = 'e0440000-0000-4000-8000-000000000002'
               AND cantidad = 5.00 AND moneda = 'ARS'
               AND descripcion = 'asociacion explicita preservada'
               AND creado_en = '2026-10-05 15:00:00+00'
               AND actualizado_en = '2026-10-05 15:10:00+00'
        ) THEN
            RAISE EXCEPTION 'HU-MON-01 TEST: asociacion explicita/campos cambiaron en Migración B';
        END IF;
    END $$;

    DO $$
    BEGIN
        BEGIN
            INSERT INTO public.movimientos_financieros (
                id, usuario_id, billetera_id, tipo, cantidad, moneda, fecha_movimiento, origen
            ) VALUES (
                'e0440000-0000-4000-8000-0000000000f2',
                'e0440000-0000-4000-8000-000000000001', NULL,
                'egreso', 1.00, 'ARS', '2026-10-06', 'manual'
            );
            RAISE EXCEPTION 'HU-MON-01 TEST: Migración B debía rechazar NULL';
        EXCEPTION WHEN not_null_violation THEN
            NULL;
        END;
    END $$;

    SELECT 'HU-MON-01 AFTER_B: NOT NULL, FK y preservación correctos.' AS resultado;
\endif

SELECT :'stage' = 'cleanup' AS stk44_cleanup \gset
\if :stk44_cleanup
    SELECT max(version) = '20261012215307' AS stk44_version_ok
      FROM supabase_migrations.schema_migrations \gset
    \if :stk44_version_ok
    \else
        DO $$ BEGIN RAISE EXCEPTION 'HU-MON-01 GUARD: cleanup exige el contrato final'; END $$;
    \endif

    DELETE FROM public.movimientos_financieros
     WHERE usuario_id = 'e0440000-0000-4000-8000-000000000001';
    DELETE FROM public.billetera
     WHERE usuario_id = 'e0440000-0000-4000-8000-000000000001';
    DELETE FROM public.usuario
     WHERE id = 'e0440000-0000-4000-8000-000000000001';

    SELECT 'HU-MON-01 CLEANUP: fixtures sinteticas retiradas.' AS resultado;
\endif
