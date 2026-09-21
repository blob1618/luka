-- STK-187: Confirmación, preferencias y entrega idempotente por WhatsApp de gastos recurrentes
--
-- Rollback seguro (ejecutar manualmente en caso de reversión):
-- 1. Dropear el constraint canónico nuevo de candidato
-- ALTER TABLE public.candidato_gasto_recurrente DROP CONSTRAINT IF EXISTS candidato_gasto_recurrente_estado_check;
-- 2. Restaurar estados legacy en candidato_gasto_recurrente
-- UPDATE public.candidato_gasto_recurrente SET estado = 'convertido' WHERE estado IN ('aceptado', 'pausado', 'desactivado');
-- UPDATE public.candidato_gasto_recurrente SET estado = 'descartado' WHERE estado = 'rechazado';
-- 3. Restaurar constraint original de STK-186
-- ALTER TABLE public.candidato_gasto_recurrente ADD CONSTRAINT candidato_gasto_recurrente_estado_check
--     CHECK (estado = ANY (ARRAY['pendiente'::text, 'descartado'::text, 'convertido'::text, 'invalidado'::text]));
-- 4. Dropear columnas y constraints agregados en candidato_gasto_recurrente
-- ALTER TABLE public.candidato_gasto_recurrente DROP CONSTRAINT IF EXISTS candidato_gasto_recurrente_propuesta_conteo_check;
-- ALTER TABLE public.candidato_gasto_recurrente DROP COLUMN IF EXISTS propuesta_en;
-- ALTER TABLE public.candidato_gasto_recurrente DROP COLUMN IF EXISTS propuesta_conteo;
-- ALTER TABLE public.candidato_gasto_recurrente DROP COLUMN IF EXISTS decision_en;
-- ALTER TABLE public.candidato_gasto_recurrente DROP COLUMN IF EXISTS decision_origen;
-- 5. Dropear tablas agregadas
-- DROP TABLE IF EXISTS public.aviso_recordatorio;
-- DROP TABLE IF EXISTS public.cron_job_claim;
-- 6. Dropear columnas y constraints agregados en recordatorio
-- ALTER TABLE public.recordatorio DROP CONSTRAINT IF EXISTS recordatorio_candidato_id_fkey;
-- ALTER TABLE public.recordatorio DROP CONSTRAINT IF EXISTS recordatorio_candidato_id_key;
-- ALTER TABLE public.recordatorio DROP CONSTRAINT IF EXISTS recordatorio_dias_anticipacion_check;
-- ALTER TABLE public.recordatorio DROP CONSTRAINT IF EXISTS recordatorio_origen_check;
-- ALTER TABLE public.recordatorio DROP COLUMN IF EXISTS candidato_id;
-- ALTER TABLE public.recordatorio DROP COLUMN IF EXISTS origen;
-- ALTER TABLE public.recordatorio DROP COLUMN IF EXISTS dias_anticipacion;

-- 1. Ampliación de candidato_gasto_recurrente
ALTER TABLE public.candidato_gasto_recurrente
    ADD COLUMN IF NOT EXISTS propuesta_en timestamp with time zone,
    ADD COLUMN IF NOT EXISTS propuesta_conteo integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS decision_en timestamp with time zone,
    ADD COLUMN IF NOT EXISTS decision_origen text;

-- Eliminación del check legacy antes del backfill
ALTER TABLE public.candidato_gasto_recurrente
    DROP CONSTRAINT IF EXISTS candidato_gasto_recurrente_estado_check;

-- Backfill de datos existentes
UPDATE public.candidato_gasto_recurrente SET estado = 'aceptado' WHERE estado = 'convertido';
UPDATE public.candidato_gasto_recurrente SET estado = 'rechazado' WHERE estado = 'descartado';

-- Aplicación del nuevo check canónico
ALTER TABLE public.candidato_gasto_recurrente
    ADD CONSTRAINT candidato_gasto_recurrente_estado_check
    CHECK (estado = ANY (ARRAY[
        'pendiente'::text, 'aceptado'::text, 'rechazado'::text,
        'pausado'::text, 'desactivado'::text, 'invalidado'::text
    ]));

ALTER TABLE public.candidato_gasto_recurrente
    ADD CONSTRAINT candidato_gasto_recurrente_propuesta_conteo_check
    CHECK (propuesta_conteo >= 0);

-- 2. Ampliación de recordatorio (vínculo canónico sin columna redundante patron_hash)
ALTER TABLE public.recordatorio
    ADD COLUMN IF NOT EXISTS dias_anticipacion integer DEFAULT 1 NOT NULL,
    ADD COLUMN IF NOT EXISTS origen text DEFAULT 'manual'::text NOT NULL,
    ADD COLUMN IF NOT EXISTS candidato_id uuid;

ALTER TABLE public.recordatorio
    DROP CONSTRAINT IF EXISTS recordatorio_candidato_id_key;
ALTER TABLE public.recordatorio
    ADD CONSTRAINT recordatorio_candidato_id_key UNIQUE (candidato_id);

ALTER TABLE public.recordatorio
    DROP CONSTRAINT IF EXISTS recordatorio_candidato_id_fkey;
ALTER TABLE public.recordatorio
    ADD CONSTRAINT recordatorio_candidato_id_fkey
    FOREIGN KEY (candidato_id) REFERENCES public.candidato_gasto_recurrente(id) ON DELETE SET NULL;

ALTER TABLE public.recordatorio
    ADD CONSTRAINT recordatorio_dias_anticipacion_check
    CHECK (dias_anticipacion BETWEEN 1 AND 30);

ALTER TABLE public.recordatorio
    ADD CONSTRAINT recordatorio_origen_check
    CHECK (origen = ANY (ARRAY['manual'::text, 'recurrente_inteligente'::text]));

UPDATE public.recordatorio SET dias_anticipacion = 1, origen = 'manual' WHERE dias_anticipacion IS NULL OR origen IS NULL;

-- 3. Tabla aviso_recordatorio (entrega idempotente y desacoplada por período)
CREATE TABLE IF NOT EXISTS public.aviso_recordatorio (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    usuario_id uuid NOT NULL,
    patron_hash text NOT NULL,
    periodo text NOT NULL,
    recordatorio_id uuid NOT NULL,
    estado text DEFAULT 'pendiente'::text NOT NULL,
    motivo_supresion text,
    intentos integer DEFAULT 0 NOT NULL,
    max_intentos integer DEFAULT 3 NOT NULL,
    es_reintentable boolean DEFAULT false NOT NULL,
    reintentar_en timestamp with time zone,
    ultimo_intento_en timestamp with time zone,
    enviado_en timestamp with time zone,
    whatsapp_message_id text,
    error_detalle text,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_en timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT aviso_recordatorio_pkey PRIMARY KEY (id),
    CONSTRAINT aviso_recordatorio_usuario_id_fkey
        FOREIGN KEY (usuario_id) REFERENCES public.usuario(id) ON DELETE CASCADE,
    CONSTRAINT aviso_recordatorio_recordatorio_id_fkey
        FOREIGN KEY (recordatorio_id) REFERENCES public.recordatorio(id) ON DELETE CASCADE,
    CONSTRAINT aviso_recordatorio_usuario_patron_periodo_key
        UNIQUE (usuario_id, patron_hash, periodo),
    CONSTRAINT aviso_recordatorio_estado_check
        CHECK (estado = ANY (ARRAY[
            'pendiente'::text, 'sending'::text,
            'sent'::text, 'failed'::text, 'unknown'::text, 'suprimido'::text
        ])),
    CONSTRAINT aviso_recordatorio_periodo_check
        CHECK (periodo ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT aviso_recordatorio_intentos_check
        CHECK (intentos >= 0 AND intentos <= max_intentos)
);

CREATE TABLE IF NOT EXISTS public.cron_job_claim (
    job_name text NOT NULL,
    fecha_ejecucion date NOT NULL,
    ejecutado_en timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cron_job_claim_pkey PRIMARY KEY (job_name, fecha_ejecucion)
);

-- 4. Índices
CREATE INDEX IF NOT EXISTS aviso_recordatorio_usuario_id_idx ON public.aviso_recordatorio (usuario_id);
CREATE INDEX IF NOT EXISTS aviso_recordatorio_recordatorio_id_idx ON public.aviso_recordatorio (recordatorio_id);

CREATE INDEX IF NOT EXISTS aviso_recordatorio_cola_idx
    ON public.aviso_recordatorio (estado, reintentar_en)
    WHERE estado = ANY (ARRAY['pendiente'::text, 'failed'::text]);

-- 5. Seguridad: RLS y REVOKE
ALTER TABLE public.aviso_recordatorio ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aviso_recordatorio FROM anon, authenticated;

ALTER TABLE public.cron_job_claim ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.cron_job_claim FROM anon, authenticated;
