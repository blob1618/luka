-- STK-237 - Indice de movimientos_financieros.billetera_id.
--
-- Archivo propio y posterior al backfill a proposito. Supabase CLI aplica cada
-- archivo de migracion en una sola transaccion; si este CREATE INDEX viviera en el
-- archivo de expansion, el ACCESS EXCLUSIVE tomado por ADD COLUMN se conservaria
-- durante toda la construccion del indice y bloquearia tambien las lecturas. Al
-- separarlo, la transaccion de expansion solo contiene DDL breve de metadatos y este
-- archivo construye el indice despues del backfill, sobre la columna ya poblada.
--
-- Fase: EXPANSION. CREATE INDEX normal: toma SHARE sobre movimientos_financieros y
-- bloquea escrituras (no lecturas) mientras construye. No aplica NOT NULL ni
-- contraccion; el contrato final pertenece al corte STK-240.

CREATE INDEX IF NOT EXISTS movimientos_financieros_billetera_id_idx
    ON public.movimientos_financieros (billetera_id);
