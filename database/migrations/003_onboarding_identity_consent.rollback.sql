-- ADVERTENCIA: ESTE ROLLBACK SOLO PUEDE USARSE ANTES DE INICIAR EL ONBOARDING REAL.
-- Se vuelve deliberadamente no ejecutable cuando detecta datos operativos de STK-143.
-- No restaura el estado previo de RLS: deshabilitarlo podría reducir protecciones
-- que ya existían antes de esta migración, por lo que se deja habilitado.

BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.onboarding_invitacion) THEN
    RAISE EXCEPTION
      'Rollback STK-143 abortado: public.onboarding_invitacion contiene filas';
  END IF;

  IF EXISTS (SELECT 1 FROM public.usuario WHERE auth_user_id IS NOT NULL) THEN
    RAISE EXCEPTION
      'Rollback STK-143 abortado: existen usuarios vinculados mediante auth_user_id';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.acuerdo_version
    WHERE esta_vigente = true
       OR vigente_desde IS NOT NULL
  ) THEN
    RAISE EXCEPTION
      'Rollback STK-143 abortado: existen versiones legales activadas o con fecha de vigencia';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.acuerdo_aceptado
    WHERE origen IS DISTINCT FROM 'legacy_desconocido'
  ) THEN
    RAISE EXCEPTION
      'Rollback STK-143 abortado: existen aceptaciones creadas por STK-143';
  END IF;
END
$$;

DROP TABLE public.onboarding_invitacion;

ALTER TABLE public.acuerdo_aceptado
  DROP CONSTRAINT acuerdo_aceptado_usuario_version_key,
  DROP COLUMN origen,
  ALTER COLUMN usuario_id DROP NOT NULL,
  ALTER COLUMN version_acuerdo_id DROP NOT NULL,
  ALTER COLUMN aceptado_en DROP NOT NULL;

DROP INDEX public.acuerdo_version_vigente_uidx;
ALTER TABLE public.acuerdo_version
  DROP CONSTRAINT acuerdo_version_version_key,
  DROP CONSTRAINT acuerdo_version_vigencia_fecha_check,
  DROP COLUMN esta_vigente,
  DROP COLUMN vigente_desde;

DROP INDEX public.usuario_auth_user_id_uidx;
-- Una aplicación exitosa implica que STK-143 creó auth_user_id: si era
-- preexistente, la migración habría abortado antes de modificar el esquema.
ALTER TABLE public.usuario
  DROP CONSTRAINT usuario_whatsapp_id_no_vacio_check,
  DROP CONSTRAINT usuario_auth_user_id_fkey,
  DROP COLUMN auth_user_id;

COMMIT;
