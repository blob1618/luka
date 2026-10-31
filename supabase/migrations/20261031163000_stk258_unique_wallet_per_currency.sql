-- STK-258 - Una billetera por usuario y moneda.
--
-- Limpieza directa: elige una superviviente por (usuario_id, moneda), reasigna los
-- movimientos de las duplicadas y las borra. No agrega columnas ni tablas de auditoria.
-- Requiere backup previo de public.billetera y public.movimientos_financieros:
-- el DELETE es fisico y la migracion no guarda mapeo por movimiento.
--
-- La FK compuesta (billetera_id, usuario_id, moneda) se mantiene valida porque la
-- superviviente pertenece al mismo usuario y a la misma moneda.

BEGIN;

CREATE TEMP TABLE movimientos_antes ON COMMIT DROP AS
SELECT usuario_id, moneda,
       count(*) AS total,
       count(*) FILTER (WHERE anulado_en IS NULL) AS activos,
       coalesce(sum(cantidad) FILTER (WHERE tipo = 'ingreso'), 0) AS ingresos,
       coalesce(sum(cantidad) FILTER (WHERE tipo = 'egreso'), 0) AS egresos
FROM public.movimientos_financieros
GROUP BY usuario_id, moneda;

-- Un movimiento sin billetera o con moneda/usuario distinto no puede existir con las
-- constraints vigentes. Si aparece, se aborta: no se borran registros contables.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.movimientos_financieros m
        LEFT JOIN public.billetera b ON b.id = m.billetera_id
        WHERE b.id IS NULL
           OR b.usuario_id <> m.usuario_id
           OR b.moneda <> m.moneda
    ) THEN
        RAISE EXCEPTION 'STK-258: hay movimientos sin billetera valida; abortar sin borrar datos';
    END IF;
END $$;

CREATE TEMP TABLE billetera_superviviente ON COMMIT DROP AS
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

DELETE FROM public.billetera b
USING public.billetera_superviviente s
WHERE b.usuario_id = s.usuario_id
  AND b.moneda = s.moneda
  AND b.id <> s.survivor_id;

-- El indice por nombre deja de tener sentido: con una billetera por moneda, los
-- nombres pueden repetirse entre ARS y USD sin ambiguedad.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.billetera
        GROUP BY usuario_id, moneda HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'STK-258: quedaron billeteras duplicadas';
    END IF;
END $$;

DROP INDEX IF EXISTS public.billetera_usuario_moneda_nombre_uidx;
CREATE UNIQUE INDEX IF NOT EXISTS billetera_usuario_moneda_uidx
    ON public.billetera (usuario_id, moneda);

DO $$
BEGIN
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
