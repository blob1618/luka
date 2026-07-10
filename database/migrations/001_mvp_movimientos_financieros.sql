-- Migracion MVP Release 1: contrato oficial de movimientos financieros.
-- Cambio acotado a public.usuario y public.movimientos_financieros.

BEGIN;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

ALTER TABLE public.usuario
  ADD COLUMN IF NOT EXISTS whatsapp_id text;

CREATE UNIQUE INDEX IF NOT EXISTS usuario_whatsapp_id_uidx
  ON public.usuario (whatsapp_id)
  WHERE whatsapp_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.movimientos_financieros (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid NOT NULL,
  categoria_id uuid NULL,
  tipo text NOT NULL,
  cantidad numeric NOT NULL,
  moneda text NOT NULL DEFAULT 'ARS',
  descripcion text NULL,
  fecha_movimiento date NOT NULL DEFAULT CURRENT_DATE,
  origen text NOT NULL DEFAULT 'whatsapp_text',
  whatsapp_message_id text NULL,
  creado_en timestamp with time zone NOT NULL DEFAULT now(),
  actualizado_en timestamp with time zone NOT NULL DEFAULT now(),

  CONSTRAINT movimientos_financieros_pkey PRIMARY KEY (id),
  CONSTRAINT movimientos_financieros_tipo_check CHECK (tipo IN ('ingreso', 'egreso')),
  CONSTRAINT movimientos_financieros_cantidad_check CHECK (cantidad > 0),
  CONSTRAINT movimientos_financieros_usuario_id_fkey
    FOREIGN KEY (usuario_id) REFERENCES public.usuario(id),
  CONSTRAINT movimientos_financieros_categoria_id_fkey
    FOREIGN KEY (categoria_id) REFERENCES public.categorias(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS movimientos_financieros_whatsapp_message_id_uidx
  ON public.movimientos_financieros (whatsapp_message_id)
  WHERE whatsapp_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS movimientos_financieros_usuario_fecha_idx
  ON public.movimientos_financieros (usuario_id, fecha_movimiento DESC);

CREATE INDEX IF NOT EXISTS movimientos_financieros_usuario_tipo_fecha_idx
  ON public.movimientos_financieros (usuario_id, tipo, fecha_movimiento DESC);

CREATE INDEX IF NOT EXISTS movimientos_financieros_usuario_categoria_fecha_idx
  ON public.movimientos_financieros (usuario_id, categoria_id, fecha_movimiento DESC)
  WHERE categoria_id IS NOT NULL;

COMMIT;
