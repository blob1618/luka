-- El acceso financiero de Release 1 pasa exclusivamente por el backend.
-- El rol de conexión del backend posee BYPASSRLS; anon/authenticated no reciben
-- policies públicas en esta migración.
ALTER TABLE public.movimientos_financieros ENABLE ROW LEVEL SECURITY;

CREATE UNIQUE INDEX movimientos_financieros_whatsapp_message_id_uidx
  ON public.movimientos_financieros (whatsapp_message_id)
  WHERE whatsapp_message_id IS NOT NULL;

CREATE INDEX movimientos_financieros_usuario_fecha_idx
  ON public.movimientos_financieros (usuario_id, fecha_movimiento DESC);

CREATE INDEX movimientos_financieros_usuario_tipo_fecha_idx
  ON public.movimientos_financieros (usuario_id, tipo, fecha_movimiento DESC);

CREATE INDEX movimientos_financieros_usuario_categoria_fecha_idx
  ON public.movimientos_financieros (usuario_id, categoria_id, fecha_movimiento DESC)
  WHERE categoria_id IS NOT NULL;
