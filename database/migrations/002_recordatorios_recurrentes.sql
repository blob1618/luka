-- Migración: recordatorios de pagos recurrentes fijos (STK-41).
-- Cambios en public.recordatorio y public.usuario.

BEGIN;

-- 1. Agregar columnas nuevas a recordatorio
ALTER TABLE public.recordatorio
  ADD COLUMN IF NOT EXISTS dia_del_mes integer,
  ADD COLUMN IF NOT EXISTS monto numeric,
  ADD COLUMN IF NOT EXISTS moneda text DEFAULT 'ARS',
  ADD COLUMN IF NOT EXISTS estado text NOT NULL DEFAULT 'activo',
  ADD COLUMN IF NOT EXISTS ultimo_aviso_enviado date;

-- 2. Eliminar columnas obsoletas de recordatorio
ALTER TABLE public.recordatorio
  DROP COLUMN IF EXISTS recordar_en,
  DROP COLUMN IF EXISTS es_recurrente;

-- 3. Constraints
ALTER TABLE public.recordatorio
  ADD CONSTRAINT recordatorio_dia_del_mes_check
    CHECK (dia_del_mes BETWEEN 1 AND 31);

ALTER TABLE public.recordatorio
  ADD CONSTRAINT recordatorio_estado_check
    CHECK (estado IN ('activo', 'pausado', 'eliminado'));

ALTER TABLE public.recordatorio
  ADD CONSTRAINT recordatorio_monto_check
    CHECK (monto IS NULL OR monto > 0);

-- 4. Índice para queries del scheduler
CREATE INDEX IF NOT EXISTS recordatorio_usuario_estado_idx
  ON public.recordatorio (usuario_id, estado)
  WHERE estado = 'activo';

-- 5. Agregar ultimo_mensaje_en a usuario
ALTER TABLE public.usuario
  ADD COLUMN IF NOT EXISTS ultimo_mensaje_en timestamp with time zone;

COMMIT;
