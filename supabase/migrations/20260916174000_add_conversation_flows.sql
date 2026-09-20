-- Mensajes y recorridos administrables.
-- El backend es el unico escritor. RLS queda habilitado sin policies publicas.

CREATE TABLE public.conversation_flow (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    event_key text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT conversation_flow_pkey PRIMARY KEY (id),
    CONSTRAINT conversation_flow_slug_key UNIQUE (slug),
    CONSTRAINT conversation_flow_event_key_key UNIQUE (event_key),
    CONSTRAINT conversation_flow_slug_no_vacio_check CHECK (btrim(slug) <> ''::text),
    CONSTRAINT conversation_flow_name_no_vacio_check CHECK (btrim(name) <> ''::text),
    CONSTRAINT conversation_flow_event_key_no_vacio_check CHECK (btrim(event_key) <> ''::text),
    CONSTRAINT conversation_flow_status_check CHECK (status = ANY (ARRAY['active'::text, 'archived'::text]))
);

CREATE TABLE public.conversation_flow_version (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    flow_id uuid NOT NULL,
    version_number integer NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    definition jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT conversation_flow_version_pkey PRIMARY KEY (id),
    CONSTRAINT conversation_flow_version_flow_id_fkey
        FOREIGN KEY (flow_id) REFERENCES public.conversation_flow(id) ON DELETE CASCADE,
    CONSTRAINT conversation_flow_version_flow_number_key UNIQUE (flow_id, version_number),
    CONSTRAINT conversation_flow_version_number_check CHECK (version_number > 0),
    CONSTRAINT conversation_flow_version_status_check
        CHECK (status = ANY (ARRAY['draft'::text, 'published'::text, 'retired'::text])),
    CONSTRAINT conversation_flow_version_publication_check CHECK (
        (status = 'draft'::text AND published_at IS NULL)
        OR (status = ANY (ARRAY['published'::text, 'retired'::text]) AND published_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX conversation_flow_version_draft_uidx
    ON public.conversation_flow_version (flow_id)
    WHERE status = 'draft'::text;

CREATE UNIQUE INDEX conversation_flow_version_published_uidx
    ON public.conversation_flow_version (flow_id)
    WHERE status = 'published'::text;

CREATE INDEX conversation_flow_version_flow_status_idx
    ON public.conversation_flow_version (flow_id, status);

ALTER TABLE public.conversation_flow ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation_flow_version ENABLE ROW LEVEL SECURITY;
