-- Baseline del esquema remoto de Luka, capturado el 2026-09-11.
-- No editar este archivo después de publicado: cada cambio posterior debe ser
-- una nueva migración timestamped dentro de supabase/migrations/.

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


CREATE SCHEMA IF NOT EXISTS "public";


ALTER SCHEMA "public" OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."acuerdo_aceptado" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid" NOT NULL,
    "version_acuerdo_id" "uuid" NOT NULL,
    "aceptado_en" timestamp without time zone DEFAULT "now"() NOT NULL,
    "origen" "text" DEFAULT 'web_onboarding'::"text" NOT NULL
);


ALTER TABLE "public"."acuerdo_aceptado" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."acuerdo_version" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "version" "text" NOT NULL,
    "contenido" "text" NOT NULL,
    "creado_en" timestamp without time zone DEFAULT "now"(),
    "esta_vigente" boolean DEFAULT false NOT NULL,
    "vigente_desde" timestamp with time zone,
    CONSTRAINT "acuerdo_version_vigencia_fecha_check" CHECK ((("esta_vigente" = false) OR ("vigente_desde" IS NOT NULL)))
);


ALTER TABLE "public"."acuerdo_version" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."categorias" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid",
    "nombre" "text" NOT NULL,
    "es_default" boolean DEFAULT false,
    "esta_eliminado" boolean DEFAULT false,
    "creado_en" timestamp without time zone DEFAULT "now"()
);


ALTER TABLE "public"."categorias" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."dashboard_login_link" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid" NOT NULL,
    "token_hash" "text" NOT NULL,
    "estado" "text" DEFAULT 'pendiente'::"text" NOT NULL,
    "expira_en" timestamp with time zone NOT NULL,
    "reenvios" integer DEFAULT 0 NOT NULL,
    "ultimo_envio_en" timestamp with time zone,
    "consumido_en" timestamp with time zone,
    "creado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    "actualizado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "dashboard_login_link_estado_campos_check" CHECK (((("estado" = 'pendiente'::"text") AND ("consumido_en" IS NULL)) OR (("estado" = 'consumido'::"text") AND ("consumido_en" IS NOT NULL)) OR (("estado" = 'vencido'::"text") AND ("consumido_en" IS NULL)))),
    CONSTRAINT "dashboard_login_link_estado_check" CHECK (("estado" = ANY (ARRAY['pendiente'::"text", 'consumido'::"text", 'vencido'::"text"]))),
    CONSTRAINT "dashboard_login_link_expiracion_check" CHECK (("expira_en" > "creado_en")),
    CONSTRAINT "dashboard_login_link_reenvios_check" CHECK (("reenvios" >= 0)),
    CONSTRAINT "dashboard_login_link_token_hash_no_vacio_check" CHECK (("btrim"("token_hash") <> ''::"text"))
);


ALTER TABLE "public"."dashboard_login_link" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."evento" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid",
    "agregar_tipo" "text" NOT NULL,
    "agregar_id" "uuid" NOT NULL,
    "tipo_evento" "text" NOT NULL,
    "carga" "jsonb",
    "creado_en" timestamp without time zone DEFAULT "now"()
);


ALTER TABLE "public"."evento" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."limite_categoria" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid" NOT NULL,
    "categoria_id" "uuid" NOT NULL,
    "cantidad_max" numeric(18,2) NOT NULL,
    "inicio_periodo" "date" NOT NULL,
    "fin_periodo" "date" NOT NULL,
    "creado_en" timestamp without time zone DEFAULT "now"(),
    "moneda" character varying(3) DEFAULT 'ARS'::character varying NOT NULL,
    "actualizado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "limite_categoria_cantidad_max_check" CHECK (("cantidad_max" > (0)::numeric)),
    CONSTRAINT "limite_categoria_moneda_check" CHECK ((("char_length"(("moneda")::"text") = 3) AND (("moneda")::"text" = "upper"(("moneda")::"text")))),
    CONSTRAINT "limite_categoria_periodo_check" CHECK (("inicio_periodo" <= "fin_periodo"))
);


ALTER TABLE "public"."limite_categoria" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."metas" (
    "id" integer NOT NULL,
    "usuario_id" integer,
    "nombre" character varying NOT NULL,
    "monto_objetivo" double precision NOT NULL,
    "monto_actual" double precision,
    "fecha_limite" "date",
    "creada_en" timestamp without time zone
);


ALTER TABLE "public"."metas" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."metas_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."metas_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."metas_id_seq" OWNED BY "public"."metas"."id";



CREATE TABLE IF NOT EXISTS "public"."movimientos" (
    "id" integer NOT NULL,
    "usuario_id" integer,
    "tipo" character varying NOT NULL,
    "monto" double precision NOT NULL,
    "categoria" character varying NOT NULL,
    "descripcion" character varying,
    "divisa" character varying,
    "creado_en" timestamp without time zone
);


ALTER TABLE "public"."movimientos" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."movimientos_financieros" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid" NOT NULL,
    "categoria_id" "uuid",
    "tipo" "text" NOT NULL,
    "cantidad" numeric NOT NULL,
    "moneda" "text" DEFAULT 'ARS'::"text" NOT NULL,
    "descripcion" "text",
    "fecha_movimiento" "date" DEFAULT CURRENT_DATE NOT NULL,
    "origen" "text" DEFAULT 'whatsapp_text'::"text" NOT NULL,
    "whatsapp_message_id" "text",
    "creado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    "actualizado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "movimientos_financieros_cantidad_check" CHECK (("cantidad" > (0)::numeric)),
    CONSTRAINT "movimientos_financieros_tipo_check" CHECK (("tipo" = ANY (ARRAY['ingreso'::"text", 'egreso'::"text"])))
);


ALTER TABLE "public"."movimientos_financieros" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."movimientos_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."movimientos_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."movimientos_id_seq" OWNED BY "public"."movimientos"."id";



CREATE TABLE IF NOT EXISTS "public"."onboarding_invitacion" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "whatsapp_id" "text" NOT NULL,
    "token_hash" "text" NOT NULL,
    "estado" "text" DEFAULT 'pendiente'::"text" NOT NULL,
    "expira_en" timestamp with time zone NOT NULL,
    "intentos" integer DEFAULT 0 NOT NULL,
    "reenvios" integer DEFAULT 0 NOT NULL,
    "ultimo_envio_en" timestamp with time zone,
    "usuario_id" "uuid",
    "consumida_en" timestamp with time zone,
    "revocada_en" timestamp with time zone,
    "creado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    "actualizado_en" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "onboarding_invitacion_estado_campos_check" CHECK (((("estado" = 'pendiente'::"text") AND ("usuario_id" IS NULL) AND ("consumida_en" IS NULL) AND ("revocada_en" IS NULL)) OR (("estado" = 'consumida'::"text") AND ("usuario_id" IS NOT NULL) AND ("consumida_en" IS NOT NULL) AND ("revocada_en" IS NULL)) OR (("estado" = 'revocada'::"text") AND ("usuario_id" IS NULL) AND ("consumida_en" IS NULL) AND ("revocada_en" IS NOT NULL)) OR (("estado" = 'vencida'::"text") AND ("usuario_id" IS NULL) AND ("consumida_en" IS NULL) AND ("revocada_en" IS NULL)))),
    CONSTRAINT "onboarding_invitacion_estado_check" CHECK (("estado" = ANY (ARRAY['pendiente'::"text", 'consumida'::"text", 'revocada'::"text", 'vencida'::"text"]))),
    CONSTRAINT "onboarding_invitacion_expiracion_check" CHECK (("expira_en" > "creado_en")),
    CONSTRAINT "onboarding_invitacion_intentos_check" CHECK (("intentos" >= 0)),
    CONSTRAINT "onboarding_invitacion_reenvios_check" CHECK (("reenvios" >= 0)),
    CONSTRAINT "onboarding_invitacion_token_hash_no_vacio_check" CHECK (("btrim"("token_hash") <> ''::"text")),
    CONSTRAINT "onboarding_invitacion_whatsapp_id_no_vacio_check" CHECK (("btrim"("whatsapp_id") <> ''::"text"))
);


ALTER TABLE "public"."onboarding_invitacion" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."recordatorio" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "usuario_id" "uuid" NOT NULL,
    "titulo" "text" NOT NULL,
    "descripcion" "text",
    "creado_en" timestamp without time zone DEFAULT "now"(),
    "dia_del_mes" integer,
    "monto" numeric,
    "moneda" "text" DEFAULT 'ARS'::"text",
    "estado" "text" DEFAULT 'activo'::"text" NOT NULL,
    "ultimo_aviso_enviado" "date",
    CONSTRAINT "recordatorio_dia_del_mes_check" CHECK ((("dia_del_mes" >= 1) AND ("dia_del_mes" <= 31))),
    CONSTRAINT "recordatorio_estado_check" CHECK (("estado" = ANY (ARRAY['activo'::"text", 'pausado'::"text", 'eliminado'::"text"]))),
    CONSTRAINT "recordatorio_monto_check" CHECK ((("monto" IS NULL) OR ("monto" > (0)::numeric)))
);


ALTER TABLE "public"."recordatorio" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."usuario" (
    "id" "uuid" DEFAULT "extensions"."uuid_generate_v4"() NOT NULL,
    "nombre" "text" NOT NULL,
    "email" "text" NOT NULL,
    "creado_en" timestamp with time zone DEFAULT "now"(),
    "actualizado_en" timestamp with time zone DEFAULT "now"(),
    "whatsapp_id" "text",
    "ultimo_mensaje_en" timestamp with time zone,
    "auth_user_id" "uuid",
    CONSTRAINT "usuario_whatsapp_id_no_vacio_check" CHECK ((("whatsapp_id" IS NULL) OR ("btrim"("whatsapp_id") <> ''::"text")))
);


ALTER TABLE "public"."usuario" OWNER TO "postgres";


ALTER TABLE ONLY "public"."metas" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."metas_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."movimientos" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."movimientos_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."acuerdo_aceptado"
    ADD CONSTRAINT "acuerdo_aceptado_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."acuerdo_aceptado"
    ADD CONSTRAINT "acuerdo_aceptado_usuario_version_key" UNIQUE ("usuario_id", "version_acuerdo_id");



ALTER TABLE ONLY "public"."acuerdo_version"
    ADD CONSTRAINT "acuerdo_version_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."acuerdo_version"
    ADD CONSTRAINT "acuerdo_version_version_key" UNIQUE ("version");



ALTER TABLE ONLY "public"."categorias"
    ADD CONSTRAINT "categorias_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."dashboard_login_link"
    ADD CONSTRAINT "dashboard_login_link_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."dashboard_login_link"
    ADD CONSTRAINT "dashboard_login_link_token_hash_key" UNIQUE ("token_hash");



ALTER TABLE ONLY "public"."evento"
    ADD CONSTRAINT "evento_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."limite_categoria"
    ADD CONSTRAINT "limite_categoria_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."limite_categoria"
    ADD CONSTRAINT "limite_categoria_usuario_categoria_periodo_moneda_key" UNIQUE ("usuario_id", "categoria_id", "inicio_periodo", "moneda");



ALTER TABLE ONLY "public"."metas"
    ADD CONSTRAINT "metas_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."movimientos_financieros"
    ADD CONSTRAINT "movimientos_financieros_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."movimientos"
    ADD CONSTRAINT "movimientos_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."onboarding_invitacion"
    ADD CONSTRAINT "onboarding_invitacion_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."onboarding_invitacion"
    ADD CONSTRAINT "onboarding_invitacion_token_hash_key" UNIQUE ("token_hash");



ALTER TABLE ONLY "public"."recordatorio"
    ADD CONSTRAINT "recordatorio_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."usuario"
    ADD CONSTRAINT "usuario_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."usuario"
    ADD CONSTRAINT "usuario_pkey" PRIMARY KEY ("id");



CREATE UNIQUE INDEX "acuerdo_version_vigente_uidx" ON "public"."acuerdo_version" USING "btree" ("esta_vigente") WHERE ("esta_vigente" = true);



CREATE UNIQUE INDEX "categorias_usuario_nombre_activo_uidx" ON "public"."categorias" USING "btree" ("usuario_id", "lower"("btrim"("nombre"))) WHERE ("esta_eliminado" = false);



CREATE INDEX "dashboard_login_link_estado_expira_idx" ON "public"."dashboard_login_link" USING "btree" ("estado", "expira_en");



CREATE INDEX "dashboard_login_link_usuario_id_idx" ON "public"."dashboard_login_link" USING "btree" ("usuario_id");



CREATE UNIQUE INDEX "dashboard_login_link_usuario_pendiente_uidx" ON "public"."dashboard_login_link" USING "btree" ("usuario_id") WHERE ("estado" = 'pendiente'::"text");



CREATE INDEX "ix_metas_id" ON "public"."metas" USING "btree" ("id");



CREATE INDEX "ix_movimientos_id" ON "public"."movimientos" USING "btree" ("id");



CREATE INDEX "limite_categoria_categoria_id_idx" ON "public"."limite_categoria" USING "btree" ("categoria_id");



CREATE INDEX "limite_categoria_usuario_vigencia_idx" ON "public"."limite_categoria" USING "btree" ("usuario_id", "fin_periodo", "inicio_periodo");



CREATE INDEX "movimientos_financieros_presupuesto_egresos_idx" ON "public"."movimientos_financieros" USING "btree" ("usuario_id", "categoria_id", "moneda", "fecha_movimiento") WHERE (("tipo" = 'egreso'::"text") AND ("categoria_id" IS NOT NULL));



CREATE INDEX "onboarding_invitacion_estado_expira_idx" ON "public"."onboarding_invitacion" USING "btree" ("estado", "expira_en");



CREATE INDEX "onboarding_invitacion_whatsapp_id_idx" ON "public"."onboarding_invitacion" USING "btree" ("whatsapp_id");



CREATE UNIQUE INDEX "onboarding_invitacion_whatsapp_pendiente_uidx" ON "public"."onboarding_invitacion" USING "btree" ("whatsapp_id") WHERE ("estado" = 'pendiente'::"text");



CREATE INDEX "recordatorio_usuario_estado_idx" ON "public"."recordatorio" USING "btree" ("usuario_id", "estado") WHERE ("estado" = 'activo'::"text");



CREATE UNIQUE INDEX "usuario_auth_user_id_uidx" ON "public"."usuario" USING "btree" ("auth_user_id") WHERE ("auth_user_id" IS NOT NULL);



CREATE UNIQUE INDEX "usuario_whatsapp_id_uidx" ON "public"."usuario" USING "btree" ("whatsapp_id") WHERE ("whatsapp_id" IS NOT NULL);



ALTER TABLE ONLY "public"."acuerdo_aceptado"
    ADD CONSTRAINT "acuerdo_aceptado_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id");



ALTER TABLE ONLY "public"."acuerdo_aceptado"
    ADD CONSTRAINT "acuerdo_aceptado_version_acuerdo_id_fkey" FOREIGN KEY ("version_acuerdo_id") REFERENCES "public"."acuerdo_version"("id");



ALTER TABLE ONLY "public"."categorias"
    ADD CONSTRAINT "categorias_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id");



ALTER TABLE ONLY "public"."dashboard_login_link"
    ADD CONSTRAINT "dashboard_login_link_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."limite_categoria"
    ADD CONSTRAINT "limite_categoria_categoria_id_fkey" FOREIGN KEY ("categoria_id") REFERENCES "public"."categorias"("id");



ALTER TABLE ONLY "public"."limite_categoria"
    ADD CONSTRAINT "limite_categoria_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id");



ALTER TABLE ONLY "public"."movimientos_financieros"
    ADD CONSTRAINT "movimientos_financieros_categoria_id_fkey" FOREIGN KEY ("categoria_id") REFERENCES "public"."categorias"("id");



ALTER TABLE ONLY "public"."movimientos_financieros"
    ADD CONSTRAINT "movimientos_financieros_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id");



ALTER TABLE ONLY "public"."onboarding_invitacion"
    ADD CONSTRAINT "onboarding_invitacion_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."recordatorio"
    ADD CONSTRAINT "recordatorio_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "public"."usuario"("id");



ALTER TABLE ONLY "public"."usuario"
    ADD CONSTRAINT "usuario_auth_user_id_fkey" FOREIGN KEY ("auth_user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE "public"."acuerdo_aceptado" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."acuerdo_version" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."dashboard_login_link" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."limite_categoria" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."onboarding_invitacion" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."usuario" ENABLE ROW LEVEL SECURITY;


REVOKE USAGE ON SCHEMA "public" FROM PUBLIC;
GRANT ALL ON SCHEMA "public" TO PUBLIC;
