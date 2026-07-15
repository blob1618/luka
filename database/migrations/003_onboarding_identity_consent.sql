-- STK-143: identidad de Auth, invitaciones de onboarding y consentimiento versionado.
-- Revisar contra el estado remoto antes de aplicar. Esta migración no vincula usuarios existentes.

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation
      ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relname = 'usuario'
      AND attribute.attname = 'auth_user_id'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) THEN
    RAISE EXCEPTION
      'STK-143 detectó una columna public.usuario.auth_user_id preexistente; la migración no la adoptará automáticamente. Revisar el estado manualmente antes de volver a ejecutar';
  END IF;
END
$$;

UPDATE public.usuario
SET whatsapp_id = NULL
WHERE whatsapp_id IS NOT NULL
  AND btrim(whatsapp_id) = '';

ALTER TABLE public.usuario
  ADD COLUMN auth_user_id uuid NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT auth_user_id
    FROM public.usuario
    WHERE auth_user_id IS NOT NULL
    GROUP BY auth_user_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'STK-143 no puede garantizar auth_user_id único: existen identidades duplicadas en public.usuario';
  END IF;

  IF EXISTS (
    SELECT whatsapp_id
    FROM public.usuario
    WHERE whatsapp_id IS NOT NULL
    GROUP BY whatsapp_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'STK-143 no puede garantizar whatsapp_id único: existen identificadores duplicados en public.usuario';
  END IF;

  IF EXISTS (
    SELECT version
    FROM public.acuerdo_version
    GROUP BY version
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'STK-143 no puede garantizar versiones únicas: existen valores duplicados en public.acuerdo_version.version';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.acuerdo_aceptado
    WHERE usuario_id IS NULL
       OR version_acuerdo_id IS NULL
       OR aceptado_en IS NULL
  ) THEN
    RAISE EXCEPTION
      'STK-143 no puede endurecer public.acuerdo_aceptado: existen filas con usuario_id, version_acuerdo_id o aceptado_en nulos';
  END IF;

  IF EXISTS (
    SELECT usuario_id, version_acuerdo_id
    FROM public.acuerdo_aceptado
    GROUP BY usuario_id, version_acuerdo_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'STK-143 no puede garantizar aceptaciones únicas: existen pares (usuario_id, version_acuerdo_id) duplicados';
  END IF;
END
$$;

ALTER TABLE public.usuario
  ADD CONSTRAINT usuario_auth_user_id_fkey
    FOREIGN KEY (auth_user_id)
    REFERENCES auth.users(id)
    ON DELETE SET NULL,
  ADD CONSTRAINT usuario_whatsapp_id_no_vacio_check
    CHECK (whatsapp_id IS NULL OR btrim(whatsapp_id) <> '');

CREATE UNIQUE INDEX usuario_auth_user_id_uidx
  ON public.usuario (auth_user_id)
  WHERE auth_user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS usuario_whatsapp_id_uidx
  ON public.usuario (whatsapp_id)
  WHERE whatsapp_id IS NOT NULL;

CREATE TABLE public.onboarding_invitacion (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  whatsapp_id text NOT NULL,
  token_hash text NOT NULL,
  estado text NOT NULL DEFAULT 'pendiente',
  expira_en timestamp with time zone NOT NULL,
  intentos integer NOT NULL DEFAULT 0,
  reenvios integer NOT NULL DEFAULT 0,
  ultimo_envio_en timestamp with time zone NULL,
  usuario_id uuid NULL,
  consumida_en timestamp with time zone NULL,
  revocada_en timestamp with time zone NULL,
  creado_en timestamp with time zone NOT NULL DEFAULT now(),
  actualizado_en timestamp with time zone NOT NULL DEFAULT now(),

  CONSTRAINT onboarding_invitacion_pkey PRIMARY KEY (id),
  CONSTRAINT onboarding_invitacion_token_hash_key UNIQUE (token_hash),
  CONSTRAINT onboarding_invitacion_token_hash_no_vacio_check
    CHECK (btrim(token_hash) <> ''),
  CONSTRAINT onboarding_invitacion_whatsapp_id_no_vacio_check
    CHECK (btrim(whatsapp_id) <> ''),
  CONSTRAINT onboarding_invitacion_estado_check
    CHECK (estado IN ('pendiente', 'consumida', 'revocada', 'vencida')),
  CONSTRAINT onboarding_invitacion_intentos_check CHECK (intentos >= 0),
  CONSTRAINT onboarding_invitacion_reenvios_check CHECK (reenvios >= 0),
  CONSTRAINT onboarding_invitacion_expiracion_check CHECK (expira_en > creado_en),
  CONSTRAINT onboarding_invitacion_estado_campos_check
    CHECK (
      (
        estado = 'pendiente'
        AND usuario_id IS NULL
        AND consumida_en IS NULL
        AND revocada_en IS NULL
      )
      OR (
        estado = 'consumida'
        AND usuario_id IS NOT NULL
        AND consumida_en IS NOT NULL
        AND revocada_en IS NULL
      )
      OR (
        estado = 'revocada'
        AND usuario_id IS NULL
        AND consumida_en IS NULL
        AND revocada_en IS NOT NULL
      )
      OR (
        estado = 'vencida'
        AND usuario_id IS NULL
        AND consumida_en IS NULL
        AND revocada_en IS NULL
      )
    ),
  CONSTRAINT onboarding_invitacion_usuario_id_fkey
    FOREIGN KEY (usuario_id)
    REFERENCES public.usuario(id)
    ON DELETE RESTRICT
);

CREATE INDEX onboarding_invitacion_whatsapp_id_idx
  ON public.onboarding_invitacion (whatsapp_id);

CREATE INDEX onboarding_invitacion_estado_expira_idx
  ON public.onboarding_invitacion (estado, expira_en);

CREATE UNIQUE INDEX onboarding_invitacion_whatsapp_pendiente_uidx
  ON public.onboarding_invitacion (whatsapp_id)
  WHERE estado = 'pendiente';

ALTER TABLE public.acuerdo_version
  ADD COLUMN esta_vigente boolean NOT NULL DEFAULT false,
  ADD COLUMN vigente_desde timestamp with time zone NULL;

ALTER TABLE public.acuerdo_version
  ADD CONSTRAINT acuerdo_version_version_key UNIQUE (version),
  ADD CONSTRAINT acuerdo_version_vigencia_fecha_check
    CHECK (esta_vigente = false OR vigente_desde IS NOT NULL);

CREATE UNIQUE INDEX acuerdo_version_vigente_uidx
  ON public.acuerdo_version (esta_vigente)
  WHERE esta_vigente = true;

ALTER TABLE public.acuerdo_aceptado
  ALTER COLUMN usuario_id SET NOT NULL,
  ALTER COLUMN version_acuerdo_id SET NOT NULL,
  ALTER COLUMN aceptado_en SET NOT NULL,
  ADD COLUMN origen text NULL;

UPDATE public.acuerdo_aceptado
SET origen = 'legacy_desconocido'
WHERE origen IS NULL;

ALTER TABLE public.acuerdo_aceptado
  ALTER COLUMN origen SET DEFAULT 'web_onboarding',
  ALTER COLUMN origen SET NOT NULL,
  ADD CONSTRAINT acuerdo_aceptado_usuario_version_key
    UNIQUE (usuario_id, version_acuerdo_id);

ALTER TABLE public.usuario ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.onboarding_invitacion ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.acuerdo_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.acuerdo_aceptado ENABLE ROW LEVEL SECURITY;

COMMIT;
