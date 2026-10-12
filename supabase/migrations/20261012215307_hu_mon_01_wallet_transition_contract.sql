-- HU-MON-01 - validar la transición y contraer billetera_id a NOT NULL.
--
-- La migración anterior ya hizo el backfill y dejó un CHECK NOT VALID que rechaza
-- nuevas escrituras nulas. Cada migración Supabase se aplica en su propia transacción.
--
-- En PostgreSQL 17, VALIDATE CONSTRAINT escanea con SHARE UPDATE EXCLUSIVE, compatible
-- con DML ordinario. SET NOT NULL puede omitir un segundo scan porque el CHECK válido
-- demuestra la ausencia de nulos. Mantener SET NOT NULL y DROP CONSTRAINT como
-- sentencias separadas: el CHECK no debe quitarse dentro del mismo ALTER TABLE que
-- aplica NOT NULL.

DO $$
DECLARE
    v_check_validated BOOLEAN;
    v_fk_validated BOOLEAN;
    v_column_not_null BOOLEAN;
BEGIN
    SELECT convalidated
      INTO v_check_validated
      FROM pg_constraint
     WHERE conrelid = 'public.movimientos_financieros'::regclass
       AND conname = 'movimientos_financieros_billetera_not_null_check'
       AND contype = 'c';

    IF v_check_validated IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'HU-MON-01: se esperaba el CHECK de transición NOT VALID';
    END IF;

    SELECT convalidated
      INTO v_fk_validated
      FROM pg_constraint
     WHERE conrelid = 'public.movimientos_financieros'::regclass
       AND conname = 'movimientos_financieros_billetera_fkey'
       AND contype = 'f';

    IF v_fk_validated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'HU-MON-01: la FK compuesta debe estar validada';
    END IF;

    SELECT attnotnull
      INTO v_column_not_null
      FROM pg_attribute
     WHERE attrelid = 'public.movimientos_financieros'::regclass
       AND attname = 'billetera_id'
       AND NOT attisdropped;

    IF v_column_not_null IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'HU-MON-01: billetera_id debe seguir nullable antes de Migración B';
    END IF;
END $$;

ALTER TABLE public.movimientos_financieros
    VALIDATE CONSTRAINT movimientos_financieros_billetera_not_null_check;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.movimientos_financieros AS m
         WHERE m.billetera_id IS NULL
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: quedan movimientos nulos tras validar el CHECK';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.movimientos_financieros AS m
          LEFT JOIN public.billetera AS b ON b.id = m.billetera_id
         WHERE m.billetera_id IS NOT NULL
           AND (b.id IS NULL
             OR b.usuario_id IS DISTINCT FROM m.usuario_id
             OR b.moneda IS DISTINCT FROM m.moneda)
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: billetera inexistente o propietario/moneda incompatibles';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_not_null_check'
           AND contype = 'c'
           AND convalidated
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: el CHECK no quedó validado';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_fkey'
           AND contype = 'f'
           AND convalidated
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: la FK compuesta no quedó validada';
    END IF;
END $$;

ALTER TABLE public.movimientos_financieros
    ALTER COLUMN billetera_id SET NOT NULL;

ALTER TABLE public.movimientos_financieros
    DROP CONSTRAINT movimientos_financieros_billetera_not_null_check;

DO $$
DECLARE
    v_column_not_null BOOLEAN;
BEGIN
    SELECT attnotnull
      INTO v_column_not_null
      FROM pg_attribute
     WHERE attrelid = 'public.movimientos_financieros'::regclass
       AND attname = 'billetera_id'
       AND NOT attisdropped;

    IF v_column_not_null IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'HU-MON-01: billetera_id no quedó NOT NULL';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_not_null_check'
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: el CHECK temporal no fue retirado';
    END IF;
END $$;
