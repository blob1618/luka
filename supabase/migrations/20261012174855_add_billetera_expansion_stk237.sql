-- STK-237 - Expansion de esquema: billetera y vinculo nullable con movimientos.
--
-- Fase: EXPANSION. El binario anterior (main en 528ff2a) sigue insertando
-- movimientos sin billetera_id, por eso la columna queda NULL y la FK compuesta se
-- agrega NOT VALID. Este archivo NO aplica NOT NULL ni elimina la compatibilidad:
-- el contrato final y el backfill transitorio de escrituras posteriores a la
-- expansion pertenecen al corte coordinado STK-240.
--
-- Perfil de locks (tabla grande). Supabase CLI aplica cada archivo de migracion en
-- una sola transaccion, por eso un lock se conserva hasta el final del archivo:
--   * CREATE TABLE / CREATE INDEX sobre public.billetera: tabla nueva y vacia; no
--     afecta a movimientos_financieros.
--   * ALTER TABLE ... ADD COLUMN billetera_id uuid: ACCESS EXCLUSIVE (bloquea lecturas
--     y escrituras de movimientos_financieros) durante esta transaccion de expansion.
--     Es solo metadatos: sin DEFAULT y sin rewrite de tabla en PostgreSQL 11+. Este
--     archivo no ejecuta DML ni construye indices sobre movimientos_financieros, de
--     modo que la transaccion de expansion es breve.
--   * ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID: SHARE ROW EXCLUSIVE sobre la tabla
--     referenciante y la referenciada, solo metadatos. NOT VALID omite el escaneo de
--     validacion, asi que no hay scan de movimientos_financieros. La validacion
--     posterior usa SHARE UPDATE EXCLUSIVE sobre la referenciante y ROW SHARE sobre la
--     referenciada (ver migracion de backfill).
-- El backfill y la validacion viven en
-- 20261012174901_backfill_billetera_historica_stk237.sql; el indice de billetera_id
-- vive en 20261012181524_add_movimientos_billetera_id_index_stk237.sql. Son archivos
-- separados para que esta transaccion de expansion termine y libere ACCESS EXCLUSIVE
-- antes de la carga de datos y antes de construir el indice.

CREATE TABLE IF NOT EXISTS public.billetera (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    usuario_id uuid NOT NULL,
    nombre text NOT NULL,
    moneda text NOT NULL,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT billetera_pkey PRIMARY KEY (id),
    CONSTRAINT billetera_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id),
    CONSTRAINT billetera_nombre_no_vacio_check CHECK (btrim(nombre) <> ''),
    -- Clave redundante respecto de la PK, necesaria como destino de la FK compuesta
    -- que garantiza propietario + moneda en el movimiento.
    CONSTRAINT billetera_id_usuario_moneda_key UNIQUE (id, usuario_id, moneda)
);

-- Un usuario no puede repetir el nombre normalizado de billetera dentro de la misma
-- moneda. Deliberadamente no se restringe el formato de moneda: el backfill de
-- STK-237 debe conservar la pareja historica exacta (usuario_id, moneda), y la
-- normalizacion de moneda corresponde a STK-238.
CREATE UNIQUE INDEX IF NOT EXISTS billetera_usuario_moneda_nombre_uidx
    ON public.billetera (usuario_id, moneda, lower(btrim(nombre)));

CREATE INDEX IF NOT EXISTS billetera_usuario_id_idx
    ON public.billetera (usuario_id);

ALTER TABLE public.billetera ENABLE ROW LEVEL SECURITY;

-- El backend usa un rol con BYPASSRLS. No se agregan policies ni se apoya en
-- auth.uid(): el contrato financiero se resuelve en el backend via public.usuario.
REVOKE ALL ON TABLE public.billetera FROM anon, authenticated;

ALTER TABLE public.movimientos_financieros
    ADD COLUMN IF NOT EXISTS billetera_id uuid;

-- Integridad propietario + moneda: un movimiento solo puede apuntar a una billetera
-- del mismo usuario_id y la misma moneda. Con MATCH SIMPLE, billetera_id NULL no se
-- valida, que es el estado permitido durante la expansion.
ALTER TABLE public.movimientos_financieros
    ADD CONSTRAINT movimientos_financieros_billetera_fkey
    FOREIGN KEY (billetera_id, usuario_id, moneda)
    REFERENCES public.billetera (id, usuario_id, moneda)
    NOT VALID;
