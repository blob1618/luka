-- STK-186: Entidad de candidatos de gastos recurrentes detectados e índice de optimización
--
-- Rollback seguro (ejecutar manualmente en caso de reversión):
-- DROP TABLE IF EXISTS public.candidato_gasto_recurrente;
-- DROP INDEX IF EXISTS public.movimientos_financieros_egresos_activos_fecha_idx;
--
-- Nota operativa: La validación real con PostgreSQL / EXPLAIN queda pendiente de
-- ejecución en un entorno PostgreSQL/CI autorizado dado que Supabase CLI no se
-- encuentra disponible localmente en este checkout. No acceder a producción.

CREATE TABLE IF NOT EXISTS public.candidato_gasto_recurrente (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    usuario_id uuid NOT NULL,
    patron_hash text NOT NULL,
    descripcion_normalizada text NOT NULL,
    categoria_id uuid,
    moneda text DEFAULT 'ARS'::text NOT NULL,
    concepto text NOT NULL,
    monto_estimado numeric(18, 2),
    dia_estimado integer NOT NULL,
    proxima_fecha_estimada date NOT NULL,
    estado text DEFAULT 'pendiente'::text NOT NULL,
    ultima_fecha_movimiento date NOT NULL,
    evidencia_movimiento_ids jsonb NOT NULL,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_en timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT candidato_gasto_recurrente_pkey PRIMARY KEY (id),
    CONSTRAINT candidato_gasto_recurrente_usuario_id_fkey
        FOREIGN KEY (usuario_id) REFERENCES public.usuario(id) ON DELETE CASCADE,
    CONSTRAINT candidato_gasto_recurrente_categoria_id_fkey
        FOREIGN KEY (categoria_id) REFERENCES public.categorias(id) ON DELETE SET NULL,
    CONSTRAINT candidato_gasto_recurrente_usuario_patron_key UNIQUE (usuario_id, patron_hash),
    CONSTRAINT candidato_gasto_recurrente_dia_check CHECK (dia_estimado BETWEEN 1 AND 31),
    CONSTRAINT candidato_gasto_recurrente_monto_check CHECK (monto_estimado IS NULL OR monto_estimado > 0),
    CONSTRAINT candidato_gasto_recurrente_moneda_check CHECK (length(moneda) = 3 AND moneda = upper(moneda)),
    CONSTRAINT candidato_gasto_recurrente_estado_check
        CHECK (estado = ANY (ARRAY['pendiente'::text, 'descartado'::text, 'convertido'::text, 'invalidado'::text])),
    CONSTRAINT candidato_gasto_recurrente_patron_hash_len_check CHECK (length(patron_hash) = 64),
    CONSTRAINT candidato_gasto_recurrente_desc_no_vacio_check CHECK (btrim(descripcion_normalizada) <> ''::text),
    CONSTRAINT candidato_gasto_recurrente_concepto_no_vacio_check CHECK (btrim(concepto) <> ''::text)
);

CREATE INDEX IF NOT EXISTS candidato_gasto_recurrente_usuario_estado_idx
    ON public.candidato_gasto_recurrente (usuario_id, estado);

CREATE INDEX IF NOT EXISTS candidato_gasto_recurrente_proxima_fecha_idx
    ON public.candidato_gasto_recurrente (proxima_fecha_estimada);

CREATE INDEX IF NOT EXISTS movimientos_financieros_egresos_activos_fecha_idx
    ON public.movimientos_financieros (fecha_movimiento, usuario_id)
    WHERE tipo = 'egreso' AND anulado_en IS NULL;

ALTER TABLE public.candidato_gasto_recurrente ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.candidato_gasto_recurrente FROM anon, authenticated;
