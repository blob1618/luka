-- STK-237 - Backfill historico inicial de billeteras.
--
-- Alcance temporal: fase de EXPANSION. Este archivo crea una billetera 'Inicial'
-- por pareja historica exacta (usuario_id, moneda) que tenga movimientos sin
-- billetera y completa billetera_id en esas filas. NO aplica NOT NULL, no borra la
-- FK NOT VALID y no asume que el backfill quede completo para siempre: el binario
-- anterior puede seguir insertando movimientos con billetera_id NULL despues de
-- esta migracion.
--
-- Idempotente y re-ejecutable: solo considera movimientos con billetera_id NULL y
-- usa ON CONFLICT DO NOTHING. El corte coordinado STK-240 reutiliza esta misma
-- logica como backfill transitorio de las escrituras posteriores a la expansion y
-- recien despues aplica el contrato final (NOT NULL + validacion de huerfanos).
--
-- Locks: no hay ACCESS EXCLUSIVE ni bloqueo exclusivo global. INSERT y UPDATE toman
-- RowExclusiveLock sobre movimientos_financieros y billetera, compatible con otras
-- sesiones que toman RowExclusiveLock sobre filas distintas; las lecturas no se
-- bloquean (MVCC). El UPDATE mantiene row locks sobre las filas que modifica: DML
-- concurrente sobre otras filas puede continuar y una escritura concurrente sobre una
-- fila ya modificada por el backfill puede esperar. VALIDATE CONSTRAINT toma SHARE
-- UPDATE EXCLUSIVE sobre la tabla referenciante (compatible con DML) mas ROW SHARE
-- sobre la referenciada. Por eso el backfill puede correr en linea mientras el
-- binario anterior sigue operando, sin bloqueo exclusivo global. El indice de
-- billetera_id se construye despues, en
-- 20261012181524_add_movimientos_billetera_id_index_stk237.sql.
--
-- Criterio historico: se incluyen movimientos anulados (no se filtra anulado_en) y
-- el UPDATE modifica unicamente billetera_id: importe, moneda, fecha, categoria,
-- tipo, anulacion y origen conservan su valor. Usuarios sin movimientos no generan
-- billeteras.

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

-- Cierra la validacion de la FK compuesta solo despues de completar las filas.
-- Las filas nuevas con billetera_id NULL siguen permitidas por MATCH SIMPLE.
ALTER TABLE public.movimientos_financieros
    VALIDATE CONSTRAINT movimientos_financieros_billetera_fkey;
