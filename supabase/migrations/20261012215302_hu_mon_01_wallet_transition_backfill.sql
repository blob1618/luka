-- HU-MON-01 - backfill de transición y protección de escrituras legacy.
--
-- Dependencias: expansión, backfill histórico e índice de billetera_id de STK-237.
-- Ejecutar tras pausar y drenar los escritores legacy y mantener cerradas las
-- escrituras financieras durante el corte. Cada archivo Supabase se ejecuta en una
-- transacción propia; no agregar BEGIN/COMMIT manuales.
--
-- El DML modifica solo billetera_id IS NULL, incluye movimientos anulados y deja
-- intactos asociaciones explícitas y campos históricos. El backfill es re-ejecutable
-- antes del contrato: ON CONFLICT evita duplicados y UPDATE solo toca filas nulas.
--
-- La validación de datos ocurre antes de agregar el CHECK. ADD CHECK NOT VALID no
-- escanea la tabla; la transacción termina tras un DDL breve. El CHECK comienza a
-- rechazar nuevas escrituras nulas al commit, aunque movimientos_financieros todavía
-- no esté marcado NOT NULL. La validación completa pertenece a la siguiente migración.

DO $$
DECLARE
    v_fk_validated BOOLEAN;
    v_column_not_null BOOLEAN;
BEGIN
    SELECT convalidated
      INTO v_fk_validated
      FROM pg_constraint
     WHERE conrelid = 'public.movimientos_financieros'::regclass
       AND conname = 'movimientos_financieros_billetera_fkey'
       AND contype = 'f';

    IF v_fk_validated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'HU-MON-01: la FK compuesta de billetera debe existir y estar validada';
    END IF;

    SELECT attnotnull
      INTO v_column_not_null
      FROM pg_attribute
     WHERE attrelid = 'public.movimientos_financieros'::regclass
       AND attname = 'billetera_id'
       AND NOT attisdropped;

    IF v_column_not_null IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'HU-MON-01: billetera_id debe seguir nullable antes del contrato';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'public.movimientos_financieros'::regclass
           AND conname = 'movimientos_financieros_billetera_not_null_check'
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: el CHECK de transición ya existe';
    END IF;
END $$;

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
    IF EXISTS (
        SELECT 1
          FROM public.movimientos_financieros AS m
         WHERE m.billetera_id IS NULL
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: quedan movimientos sin billetera tras el backfill de transición';
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
           AND conname = 'movimientos_financieros_billetera_fkey'
           AND contype = 'f'
           AND convalidated
    ) THEN
        RAISE EXCEPTION 'HU-MON-01: la FK compuesta dejó de estar validada';
    END IF;
END $$;

ALTER TABLE public.movimientos_financieros
    ADD CONSTRAINT movimientos_financieros_billetera_not_null_check
    CHECK (billetera_id IS NOT NULL) NOT VALID;

DO $$
DECLARE
    v_check_validated BOOLEAN;
BEGIN
    SELECT convalidated
      INTO v_check_validated
      FROM pg_constraint
     WHERE conrelid = 'public.movimientos_financieros'::regclass
       AND conname = 'movimientos_financieros_billetera_not_null_check'
       AND contype = 'c';

    IF v_check_validated IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'HU-MON-01: el CHECK de transición debe quedar NOT VALID';
    END IF;
END $$;
