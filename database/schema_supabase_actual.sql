-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.usuario (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  nombre text NOT NULL,
  email text NOT NULL UNIQUE,
  creado_en timestamp with time zone DEFAULT now(),
  actualizado_en timestamp with time zone DEFAULT now(),
  CONSTRAINT usuario_pkey PRIMARY KEY (id)
);
CREATE TABLE public.acuerdo_version (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  version text NOT NULL,
  contenido text NOT NULL,
  creado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT acuerdo_version_pkey PRIMARY KEY (id)
);
CREATE TABLE public.acuerdo_aceptado (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid,
  version_acuerdo_id uuid,
  aceptado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT acuerdo_aceptado_pkey PRIMARY KEY (id),
  CONSTRAINT acuerdo_aceptado_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id),
  CONSTRAINT acuerdo_aceptado_version_acuerdo_id_fkey FOREIGN KEY (version_acuerdo_id) REFERENCES public.acuerdo_version(id)
);
CREATE TABLE public.categorias (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid,
  nombre text NOT NULL,
  es_default boolean DEFAULT false,
  esta_eliminado boolean DEFAULT false,
  creado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT categorias_pkey PRIMARY KEY (id),
  CONSTRAINT categorias_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id)
);
CREATE TABLE public.gastos (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid NOT NULL,
  categoria_id uuid,
  cantidad numeric NOT NULL CHECK (cantidad > 0::numeric),
  descripcion text,
  fecha_gasto date NOT NULL,
  creado_en timestamp without time zone DEFAULT now(),
  actualizado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT gastos_pkey PRIMARY KEY (id),
  CONSTRAINT gastos_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id),
  CONSTRAINT gastos_categoria_id_fkey FOREIGN KEY (categoria_id) REFERENCES public.categorias(id)
);
CREATE TABLE public.limite_categoria (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid NOT NULL,
  categoria_id uuid NOT NULL,
  cantidad_max numeric NOT NULL CHECK (cantidad_max > 0::numeric),
  inicio_periodo date NOT NULL,
  fin_periodo date NOT NULL,
  creado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT limite_categoria_pkey PRIMARY KEY (id),
  CONSTRAINT limite_categoria_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id),
  CONSTRAINT limite_categoria_categoria_id_fkey FOREIGN KEY (categoria_id) REFERENCES public.categorias(id)
);
CREATE TABLE public.recordatorio (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid NOT NULL,
  titulo text NOT NULL,
  descripcion text,
  recordar_en timestamp without time zone NOT NULL,
  es_recurrente boolean DEFAULT false,
  creado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT recordatorio_pkey PRIMARY KEY (id),
  CONSTRAINT recordatorio_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuario(id)
);
CREATE TABLE public.evento (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  usuario_id uuid,
  agregar_tipo text NOT NULL,
  agregar_id uuid NOT NULL,
  tipo_evento text NOT NULL,
  carga jsonb,
  creado_en timestamp without time zone DEFAULT now(),
  CONSTRAINT evento_pkey PRIMARY KEY (id)
);
CREATE TABLE public.limites_gasto (
  id uuid NOT NULL,
  usuario_id uuid NOT NULL,
  categoria_id uuid,
  monto_maximo numeric NOT NULL,
  periodo_inicio date NOT NULL,
  periodo_fin date NOT NULL,
  creado_en timestamp with time zone,
  CONSTRAINT limites_gasto_pkey PRIMARY KEY (id),
  CONSTRAINT limites_gasto_categoria_id_fkey FOREIGN KEY (categoria_id) REFERENCES public.categorias(id)
);
CREATE TABLE public.versiones_consentimiento (
  id uuid NOT NULL,
  version character varying NOT NULL,
  contenido character varying NOT NULL,
  fecha_publicacion timestamp with time zone,
  es_activa boolean,
  CONSTRAINT versiones_consentimiento_pkey PRIMARY KEY (id)
);
CREATE TABLE public.consentimientos_usuario (
  id uuid NOT NULL,
  usuario_id uuid NOT NULL,
  version_id uuid,
  aceptado boolean,
  fecha_aceptacion timestamp with time zone,
  CONSTRAINT consentimientos_usuario_pkey PRIMARY KEY (id),
  CONSTRAINT consentimientos_usuario_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.versiones_consentimiento(id)
);
CREATE TABLE public.usuarios (
  id integer NOT NULL DEFAULT nextval('usuarios_id_seq'::regclass),
  whatsapp_id character varying,
  creado_en timestamp without time zone,
  CONSTRAINT usuarios_pkey PRIMARY KEY (id)
);
CREATE TABLE public.presupuestos (
  id integer NOT NULL DEFAULT nextval('presupuestos_id_seq'::regclass),
  usuario_id integer,
  categoria character varying NOT NULL,
  monto_limite double precision NOT NULL,
  CONSTRAINT presupuestos_pkey PRIMARY KEY (id),
  CONSTRAINT presupuestos_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuarios(id)
);
CREATE TABLE public.recordatorios (
  id integer NOT NULL DEFAULT nextval('recordatorios_id_seq'::regclass),
  usuario_id integer,
  titulo character varying NOT NULL,
  fecha_vencimiento timestamp without time zone NOT NULL,
  activo integer,
  CONSTRAINT recordatorios_pkey PRIMARY KEY (id),
  CONSTRAINT recordatorios_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.usuarios(id)
);
