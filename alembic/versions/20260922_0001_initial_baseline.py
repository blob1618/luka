"""Baseline auditada del esquema existente

Revision ID: 20260922_0001
Revises: None
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "20260922_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    uuid_default = sa.text("extensions.uuid_generate_v4()") if is_postgres else None

    # Extensiones de PostgreSQL
    if is_postgres:
        op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;')

    # 1. usuario
    op.create_table(
        "usuario",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("whatsapp_id", sa.Text(), nullable=True),
        sa.Column("ultimo_mensaje_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("proactivo_habilitado", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("proactivo_ultimo_envio", sa.Date(), nullable=True),
        sa.UniqueConstraint("email", name="usuario_email_key"),
        sa.CheckConstraint(
            "whatsapp_id IS NULL OR trim(whatsapp_id) <> ''",
            name="usuario_whatsapp_id_no_vacio_check",
        ),
    )
    op.create_index(
        "usuario_whatsapp_id_uidx",
        "usuario",
        ["whatsapp_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_id IS NOT NULL"),
        sqlite_where=sa.text("whatsapp_id IS NOT NULL"),
    )
    op.create_index(
        "usuario_auth_user_id_uidx",
        "usuario",
        ["auth_user_id"],
        unique=True,
        postgresql_where=sa.text("auth_user_id IS NOT NULL"),
        sqlite_where=sa.text("auth_user_id IS NOT NULL"),
    )

    if is_postgres:
        op.create_foreign_key(
            "usuario_auth_user_id_fkey",
            "usuario",
            "users",
            ["auth_user_id"],
            ["id"],
            source_schema="public",
            referent_schema="auth",
            ondelete="SET NULL",
        )

    # 2. onboarding_invitacion
    op.create_table(
        "onboarding_invitacion",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("whatsapp_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reenvios", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ultimo_envio_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("consumida_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="onboarding_invitacion_token_hash_key"),
        sa.CheckConstraint("trim(token_hash) <> ''", name="onboarding_invitacion_token_hash_no_vacio_check"),
        sa.CheckConstraint("trim(whatsapp_id) <> ''", name="onboarding_invitacion_whatsapp_id_no_vacio_check"),
        sa.CheckConstraint("estado IN ('pendiente', 'consumida', 'revocada', 'vencida')", name="onboarding_invitacion_estado_check"),
        sa.CheckConstraint("intentos >= 0", name="onboarding_invitacion_intentos_check"),
        sa.CheckConstraint("reenvios >= 0", name="onboarding_invitacion_reenvios_check"),
        sa.CheckConstraint("expira_en > creado_en", name="onboarding_invitacion_expiracion_check"),
        sa.CheckConstraint(
            "(estado = 'pendiente' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL) OR "
            "(estado = 'consumida' AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL) OR "
            "(estado = 'revocada' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL) OR "
            "(estado = 'vencida' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL)",
            name="onboarding_invitacion_estado_campos_check",
        ),
    )
    op.create_index("onboarding_invitacion_whatsapp_id_idx", "onboarding_invitacion", ["whatsapp_id"])
    op.create_index("onboarding_invitacion_estado_expira_idx", "onboarding_invitacion", ["estado", "expira_en"])
    op.create_index(
        "onboarding_invitacion_whatsapp_pendiente_uidx",
        "onboarding_invitacion",
        ["whatsapp_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendiente'"),
        sqlite_where=sa.text("estado = 'pendiente'"),
    )

    # 3. dashboard_login_link
    op.create_table(
        "dashboard_login_link",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reenvios", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ultimo_envio_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="dashboard_login_link_token_hash_key"),
        sa.CheckConstraint("trim(token_hash) <> ''", name="dashboard_login_link_token_hash_no_vacio_check"),
        sa.CheckConstraint("estado IN ('pendiente', 'consumido', 'vencido')", name="dashboard_login_link_estado_check"),
        sa.CheckConstraint("reenvios >= 0", name="dashboard_login_link_reenvios_check"),
        sa.CheckConstraint("expira_en > creado_en", name="dashboard_login_link_expiracion_check"),
        sa.CheckConstraint(
            "(estado = 'pendiente' AND consumido_en IS NULL) OR "
            "(estado = 'consumido' AND consumido_en IS NOT NULL) OR "
            "(estado = 'vencido' AND consumido_en IS NULL)",
            name="dashboard_login_link_estado_campos_check",
        ),
    )
    op.create_index("dashboard_login_link_usuario_id_idx", "dashboard_login_link", ["usuario_id"])
    op.create_index("dashboard_login_link_estado_expira_idx", "dashboard_login_link", ["estado", "expira_en"])
    op.create_index(
        "dashboard_login_link_usuario_pendiente_uidx",
        "dashboard_login_link",
        ["usuario_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendiente'"),
        sqlite_where=sa.text("estado = 'pendiente'"),
    )

    # 4. acuerdo_version
    op.create_table(
        "acuerdo_version",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("contenido", sa.Text(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("esta_vigente", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("vigente_desde", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("version", name="acuerdo_version_version_key"),
        sa.CheckConstraint("esta_vigente = false OR vigente_desde IS NOT NULL", name="acuerdo_version_vigencia_fecha_check"),
    )
    op.create_index(
        "acuerdo_version_vigente_uidx",
        "acuerdo_version",
        ["esta_vigente"],
        unique=True,
        postgresql_where=sa.text("esta_vigente = true"),
        sqlite_where=sa.text("esta_vigente = 1"),
    )

    # 5. acuerdo_aceptado
    op.create_table(
        "acuerdo_aceptado",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("version_acuerdo_id", sa.Uuid(as_uuid=True), sa.ForeignKey("acuerdo_version.id"), nullable=False),
        sa.Column("aceptado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("origen", sa.Text(), nullable=False, server_default="web_onboarding"),
        sa.UniqueConstraint("usuario_id", "version_acuerdo_id", name="acuerdo_aceptado_usuario_version_key"),
    )

    # 6. categorias
    op.create_table(
        "categorias",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("es_default", sa.Boolean(), server_default=sa.false(), nullable=True),
        sa.Column("esta_eliminado", sa.Boolean(), server_default=sa.false(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.create_index(
        "categorias_usuario_nombre_activo_uidx",
        "categorias",
        ["usuario_id", sa.text("lower(trim(nombre))")],
        unique=True,
        postgresql_where=sa.text("esta_eliminado = false"),
        sqlite_where=sa.text("esta_eliminado = 0"),
    )

    # 7. limite_categoria
    op.create_table(
        "limite_categoria",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("categoria_id", sa.Uuid(as_uuid=True), sa.ForeignKey("categorias.id"), nullable=False),
        sa.Column("cantidad_max", sa.Numeric(18, 2), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default="ARS"),
        sa.Column("inicio_periodo", sa.Date(), nullable=False),
        sa.Column("fin_periodo", sa.Date(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("usuario_id", "categoria_id", "inicio_periodo", "moneda", name="limite_categoria_usuario_categoria_periodo_moneda_key"),
        sa.CheckConstraint("cantidad_max >= 0", name="limite_categoria_cantidad_max_check"),
        sa.CheckConstraint("length(moneda) = 3 AND moneda = upper(moneda)", name="limite_categoria_moneda_check"),
        sa.CheckConstraint("inicio_periodo <= fin_periodo", name="limite_categoria_periodo_check"),
    )
    op.create_index("limite_categoria_usuario_vigencia_idx", "limite_categoria", ["usuario_id", "fin_periodo", "inicio_periodo"])
    op.create_index("limite_categoria_categoria_id_idx", "limite_categoria", ["categoria_id"])

    # 8. candidato_gasto_recurrente
    op.create_table(
        "candidato_gasto_recurrente",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patron_hash", sa.Text(), nullable=False),
        sa.Column("descripcion_normalizada", sa.Text(), nullable=False),
        sa.Column("categoria_id", sa.Uuid(as_uuid=True), sa.ForeignKey("categorias.id", ondelete="SET NULL"), nullable=True),
        sa.Column("moneda", sa.Text(), nullable=False, server_default="ARS"),
        sa.Column("concepto", sa.Text(), nullable=False),
        sa.Column("monto_estimado", sa.Numeric(18, 2), nullable=True),
        sa.Column("dia_estimado", sa.Integer(), nullable=False),
        sa.Column("proxima_fecha_estimada", sa.Date(), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("ultima_fecha_movimiento", sa.Date(), nullable=False),
        sa.Column("evidencia_movimiento_ids", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("propuesta_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("propuesta_conteo", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_origen", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("usuario_id", "patron_hash", name="candidato_gasto_recurrente_usuario_patron_key"),
        sa.CheckConstraint("dia_estimado BETWEEN 1 AND 31", name="candidato_gasto_recurrente_dia_check"),
        sa.CheckConstraint("monto_estimado IS NULL OR monto_estimado > 0", name="candidato_gasto_recurrente_monto_check"),
        sa.CheckConstraint("length(moneda) = 3 AND moneda = upper(moneda)", name="candidato_gasto_recurrente_moneda_check"),
        sa.CheckConstraint("estado IN ('pendiente', 'aceptado', 'rechazado', 'pausado', 'desactivado', 'invalidado')", name="candidato_gasto_recurrente_estado_check"),
        sa.CheckConstraint("propuesta_conteo >= 0", name="candidato_gasto_recurrente_propuesta_conteo_check"),
        sa.CheckConstraint("length(patron_hash) = 64", name="candidato_gasto_recurrente_patron_hash_len_check"),
        sa.CheckConstraint("trim(descripcion_normalizada) <> ''", name="candidato_gasto_recurrente_desc_no_vacio_check"),
        sa.CheckConstraint("trim(concepto) <> ''", name="candidato_gasto_recurrente_concepto_no_vacio_check"),
    )
    op.create_index("candidato_gasto_recurrente_usuario_estado_idx", "candidato_gasto_recurrente", ["usuario_id", "estado"])
    op.create_index("candidato_gasto_recurrente_proxima_fecha_idx", "candidato_gasto_recurrente", ["proxima_fecha_estimada"])

    # 9. recordatorio
    op.create_table(
        "recordatorio",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("candidato_id", sa.Uuid(as_uuid=True), sa.ForeignKey("candidato_gasto_recurrente.id", ondelete="SET NULL"), unique=True, nullable=True),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("dia_del_mes", sa.Integer(), nullable=True),
        sa.Column("monto", sa.Numeric(), nullable=True),
        sa.Column("moneda", sa.Text(), server_default="ARS", nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("dias_anticipacion", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("origen", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("ultimo_aviso_enviado", sa.Date(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.CheckConstraint("dia_del_mes BETWEEN 1 AND 31", name="recordatorio_dia_del_mes_check"),
        sa.CheckConstraint("estado IN ('activo', 'pausado', 'eliminado')", name="recordatorio_estado_check"),
        sa.CheckConstraint("monto IS NULL OR monto > 0", name="recordatorio_monto_check"),
        sa.CheckConstraint("dias_anticipacion BETWEEN 1 AND 30", name="recordatorio_dias_anticipacion_check"),
        sa.CheckConstraint("origen IN ('manual', 'recurrente_inteligente')", name="recordatorio_origen_check"),
    )
    op.create_index(
        "recordatorio_usuario_estado_idx",
        "recordatorio",
        ["usuario_id", "estado"],
        postgresql_where=sa.text("estado = 'activo'"),
        sqlite_where=sa.text("estado = 'activo'"),
    )

    # 10. evento
    op.create_table(
        "evento",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("agregar_tipo", sa.Text(), nullable=False),
        sa.Column("agregar_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tipo_evento", sa.Text(), nullable=False),
        sa.Column("carga", sa.JSON().with_variant(JSONB, "postgresql"), nullable=True),
        sa.Column("creado_en", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )

    # 11. movimientos_financieros
    op.create_table(
        "movimientos_financieros",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("categoria_id", sa.Uuid(as_uuid=True), sa.ForeignKey("categorias.id"), nullable=True),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("cantidad", sa.Numeric(), nullable=False),
        sa.Column("moneda", sa.Text(), nullable=False, server_default="ARS"),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("fecha_movimiento", sa.Date(), nullable=False, server_default=sa.func.current_date()),
        sa.Column("origen", sa.Text(), nullable=False, server_default="whatsapp_text"),
        sa.Column("whatsapp_message_id", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("anulado_en", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("tipo IN ('ingreso', 'egreso')", name="movimientos_financieros_tipo_check"),
        sa.CheckConstraint("cantidad > 0", name="movimientos_financieros_cantidad_check"),
    )
    op.create_index(
        "movimientos_financieros_whatsapp_message_id_uidx",
        "movimientos_financieros",
        ["whatsapp_message_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_message_id IS NOT NULL"),
        sqlite_where=sa.text("whatsapp_message_id IS NOT NULL"),
    )
    op.create_index(
        "movimientos_financieros_presupuesto_egresos_idx",
        "movimientos_financieros",
        ["usuario_id", "categoria_id", "moneda", "fecha_movimiento"],
        postgresql_where=sa.text("tipo = 'egreso' AND categoria_id IS NOT NULL"),
        sqlite_where=sa.text("tipo = 'egreso' AND categoria_id IS NOT NULL"),
    )
    op.create_index(
        "movimientos_financieros_egresos_activos_fecha_idx",
        "movimientos_financieros",
        ["fecha_movimiento", "usuario_id"],
        postgresql_where=sa.text("tipo = 'egreso' AND anulado_en IS NULL"),
        sqlite_where=sa.text("tipo = 'egreso' AND anulado_en IS NULL"),
    )
    if is_postgres:
        op.create_index("movimientos_financieros_usuario_fecha_idx", "movimientos_financieros", ["usuario_id", sa.text("fecha_movimiento DESC")])
        op.create_index("movimientos_financieros_usuario_tipo_fecha_idx", "movimientos_financieros", ["usuario_id", "tipo", sa.text("fecha_movimiento DESC")])
        op.create_index(
            "movimientos_financieros_usuario_categoria_fecha_idx",
            "movimientos_financieros",
            ["usuario_id", "categoria_id", sa.text("fecha_movimiento DESC")],
            postgresql_where=sa.text("categoria_id IS NOT NULL"),
        )

    # 12. conversation_flow
    op.create_table(
        "conversation_flow",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="conversation_flow_slug_key"),
        sa.UniqueConstraint("event_key", name="conversation_flow_event_key_key"),
        sa.CheckConstraint("trim(slug) <> ''", name="conversation_flow_slug_no_vacio_check"),
        sa.CheckConstraint("trim(name) <> ''", name="conversation_flow_name_no_vacio_check"),
        sa.CheckConstraint("trim(event_key) <> ''", name="conversation_flow_event_key_no_vacio_check"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="conversation_flow_status_check"),
    )

    # 13. conversation_flow_version
    op.create_table(
        "conversation_flow_version",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("flow_id", sa.Uuid(as_uuid=True), sa.ForeignKey("conversation_flow.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("definition", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("flow_id", "version_number", name="conversation_flow_version_flow_number_key"),
        sa.CheckConstraint("version_number > 0", name="conversation_flow_version_number_check"),
        sa.CheckConstraint("status IN ('draft', 'published', 'retired')", name="conversation_flow_version_status_check"),
        sa.CheckConstraint(
            "(status = 'draft' AND published_at IS NULL) OR "
            "(status IN ('published', 'retired') AND published_at IS NOT NULL)",
            name="conversation_flow_version_publication_check",
        ),
    )
    op.create_index(
        "conversation_flow_version_draft_uidx",
        "conversation_flow_version",
        ["flow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
        sqlite_where=sa.text("status = 'draft'"),
    )
    op.create_index(
        "conversation_flow_version_published_uidx",
        "conversation_flow_version",
        ["flow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )
    op.create_index("conversation_flow_version_flow_status_idx", "conversation_flow_version", ["flow_id", "status"])

    # 14. aviso_recordatorio
    op.create_table(
        "aviso_recordatorio",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=uuid_default),
        sa.Column("usuario_id", sa.Uuid(as_uuid=True), sa.ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patron_hash", sa.Text(), nullable=False),
        sa.Column("periodo", sa.Text(), nullable=False),
        sa.Column("recordatorio_id", sa.Uuid(as_uuid=True), sa.ForeignKey("recordatorio.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("motivo_supresion", sa.Text(), nullable=True),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_intentos", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("es_reintentable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reintentar_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_intento_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enviado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_message_id", sa.Text(), nullable=True),
        sa.Column("error_detalle", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("usuario_id", "patron_hash", "periodo", name="aviso_recordatorio_usuario_patron_periodo_key"),
        sa.CheckConstraint("estado IN ('pendiente', 'sending', 'sent', 'failed', 'unknown', 'suprimido')", name="aviso_recordatorio_estado_check"),
        sa.CheckConstraint("intentos >= 0 AND intentos <= max_intentos", name="aviso_recordatorio_intentos_check"),
    )
    if is_postgres:
        op.create_check_constraint(
            "aviso_recordatorio_periodo_check",
            "aviso_recordatorio",
            "periodo ~ '^\\d{4}-(0[1-9]|1[0-2])$'",
        )
    op.create_index("aviso_recordatorio_usuario_id_idx", "aviso_recordatorio", ["usuario_id"])
    op.create_index("aviso_recordatorio_recordatorio_id_idx", "aviso_recordatorio", ["recordatorio_id"])
    if is_postgres:
        op.create_index(
            "aviso_recordatorio_cola_idx",
            "aviso_recordatorio",
            ["estado", "reintentar_en"],
            postgresql_where=sa.text("estado = ANY (ARRAY['pendiente'::text, 'failed'::text])"),
        )

    # 15. cron_job_claim
    op.create_table(
        "cron_job_claim",
        sa.Column("job_name", sa.Text(), primary_key=True),
        sa.Column("fecha_ejecucion", sa.Date(), primary_key=True),
        sa.Column("ejecutado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Políticas RLS y REVOKE en PostgreSQL
    if is_postgres:
        tablas_rls = [
            "acuerdo_aceptado",
            "acuerdo_version",
            "dashboard_login_link",
            "limite_categoria",
            "onboarding_invitacion",
            "usuario",
            "movimientos_financieros",
            "conversation_flow",
            "conversation_flow_version",
            "candidato_gasto_recurrente",
            "aviso_recordatorio",
            "cron_job_claim",
        ]
        for tbl in tablas_rls:
            op.execute(f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY;")

        op.execute("REVOKE ALL ON TABLE public.candidato_gasto_recurrente FROM anon, authenticated;")
        op.execute("REVOKE ALL ON TABLE public.aviso_recordatorio FROM anon, authenticated;")
        op.execute("REVOKE ALL ON TABLE public.cron_job_claim FROM anon, authenticated;")


def downgrade() -> None:
    tablas = [
        "cron_job_claim",
        "aviso_recordatorio",
        "conversation_flow_version",
        "conversation_flow",
        "movimientos_financieros",
        "evento",
        "recordatorio",
        "candidato_gasto_recurrente",
        "limite_categoria",
        "categorias",
        "acuerdo_aceptado",
        "acuerdo_version",
        "dashboard_login_link",
        "onboarding_invitacion",
        "usuario",
    ]
    for tbl in tablas:
        op.drop_table(tbl)
