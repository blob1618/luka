-- Migracion STK-237: Billetera y relacion compuesta con movimientos_financieros
BEGIN;

-- 1. Crear tabla singular billetera
CREATE TABLE public.billetera (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id UUID NOT NULL REFERENCES public.usuario(id),
    nombre TEXT NOT NULL,
    moneda TEXT NOT NULL,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT billetera_nombre_no_vacio_check CHECK (trim(nombre) <> ''),
    CONSTRAINT billetera_id_usuario_moneda_key UNIQUE (id, usuario_id, moneda)
);

-- Indices de billetera
CREATE UNIQUE INDEX billetera_usuario_moneda_nombre_uidx
    ON public.billetera (usuario_id, moneda, lower(trim(nombre)));

CREATE INDEX billetera_usuario_id_idx
    ON public.billetera (usuario_id);

-- 2. Seguridad RLS y privilegios
ALTER TABLE public.billetera ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.billetera FROM PUBLIC;
REVOKE ALL ON TABLE public.billetera FROM anon;
REVOKE ALL ON TABLE public.billetera FROM authenticated;

-- 3. Bloqueo exclusivo para backfill en movimientos_financieros
LOCK TABLE public.movimientos_financieros IN ACCESS EXCLUSIVE MODE;

-- 4. Columna nullable temporal
ALTER TABLE public.movimientos_financieros ADD COLUMN billetera_id UUID;

-- 5. Snapshot temporal previo al backfill (comparacion sin billetera_id)
CREATE TEMP TABLE _stk237_migration_snapshot ON COMMIT DROP AS
SELECT
    m.id,
    to_jsonb(m) - 'billetera_id' AS snapshot_data
FROM public.movimientos_financieros m;

-- 6. Crear billetera 'Inicial' por pareja EXACTA (usuario_id, moneda) historica
INSERT INTO public.billetera (usuario_id, nombre, moneda)
SELECT DISTINCT
    m.usuario_id,
    'Inicial' AS nombre,
    m.moneda
FROM public.movimientos_financieros m;

-- 7. Backfill de billetera_id asociando a la billetera Inicial correspondiente
UPDATE public.movimientos_financieros m
SET billetera_id = b.id
FROM public.billetera b
WHERE b.usuario_id = m.usuario_id
  AND b.moneda = m.moneda
  AND b.nombre = 'Inicial';

-- 8. Verificacion estricta de preservacion de datos antes de aplicar NOT NULL y FK
DO $$
DECLARE
    v_diff_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_diff_count
    FROM (
        SELECT s.id, s.snapshot_data
        FROM _stk237_migration_snapshot s
        FULL OUTER JOIN (
            SELECT m.id, to_jsonb(m) - 'billetera_id' AS current_data
            FROM public.movimientos_financieros m
        ) c ON s.id = c.id
        WHERE s.id IS NULL
           OR c.id IS NULL
           OR s.snapshot_data IS DISTINCT FROM c.current_data
    ) diff;

    IF v_diff_count > 0 THEN
        RAISE EXCEPTION 'STK-237 Migration validation failed: % mismatched rows detected between snapshot and current movimientos', v_diff_count;
    END IF;

    IF EXISTS (SELECT 1 FROM public.movimientos_financieros WHERE billetera_id IS NULL) THEN
        RAISE EXCEPTION 'STK-237 Migration validation failed: movimientos_financieros with NULL billetera_id found';
    END IF;
END $$;

-- 9. Restricciones finales NOT NULL y Foreign Key compuesta
ALTER TABLE public.movimientos_financieros
    ALTER COLUMN billetera_id SET NOT NULL;

ALTER TABLE public.movimientos_financieros
    ADD CONSTRAINT movimientos_financieros_billetera_fkey
    FOREIGN KEY (billetera_id, usuario_id, moneda)
    REFERENCES public.billetera (id, usuario_id, moneda);

CREATE INDEX movimientos_financieros_billetera_id_idx
    ON public.movimientos_financieros (billetera_id);

COMMIT;
