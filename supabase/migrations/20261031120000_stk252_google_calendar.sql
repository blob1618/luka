-- STK-252: Google Calendar authorization and durable reminder reconciliation.
ALTER TABLE public.recordatorio
    ADD COLUMN google_calendar_event_id text,
    ADD COLUMN google_calendar_synced_fingerprint text;

CREATE TABLE public.google_calendar_connection (
    usuario_id uuid PRIMARY KEY REFERENCES public.usuario(id) ON DELETE CASCADE,
    refresh_token_ciphertext text NOT NULL,
    conectado_en timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT google_calendar_connection_token_nonempty CHECK (trim(refresh_token_ciphertext) <> '')
);

CREATE TABLE public.google_calendar_oauth_state (
    state_hash char(64) PRIMARY KEY,
    usuario_id uuid NOT NULL REFERENCES public.usuario(id) ON DELETE CASCADE,
    expira_en timestamptz NOT NULL,
    consumido_en timestamptz
);
CREATE INDEX google_calendar_oauth_state_usuario_idx
    ON public.google_calendar_oauth_state (usuario_id);

ALTER TABLE public.google_calendar_connection ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.google_calendar_oauth_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.google_calendar_connection, public.google_calendar_oauth_state
    FROM anon, authenticated;
