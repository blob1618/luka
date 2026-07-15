import os
import uuid
from datetime import datetime, date
from sqlalchemy import (
    Column, String, DateTime, ForeignKey, create_engine,
    Boolean, Date, Integer, Numeric, CheckConstraint, Index, UniqueConstraint
)
from sqlalchemy.types import Uuid, JSON
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func

# Obtener DATABASE_URL del entorno, usando SQLite como fallback para desarrollo local
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./luka.db")

# Manejar la conexión a PostgreSQL de Supabase con psycopg3
if DATABASE_URL.startswith("postgresql"):
    # psycopg3 usa postgresql:// directamente (no requiere especificar el driver)
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,  # Verificar conexiones antes de usarlas
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Usuario(Base):
    __tablename__ = "usuario"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    creado_en = Column(DateTime(timezone=True), default=func.now())
    actualizado_en = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    whatsapp_id = Column(String, nullable=True)
    auth_user_id = Column(Uuid(as_uuid=True), nullable=True)
    ultimo_mensaje_en = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "whatsapp_id IS NULL OR trim(whatsapp_id) <> ''",
            name="usuario_whatsapp_id_no_vacio_check",
        ),
        Index(
            "usuario_whatsapp_id_uidx",
            "whatsapp_id",
            unique=True,
            postgresql_where=whatsapp_id.isnot(None),
            sqlite_where=whatsapp_id.isnot(None),
        ),
        Index(
            "usuario_auth_user_id_uidx",
            "auth_user_id",
            unique=True,
            postgresql_where=auth_user_id.isnot(None),
            sqlite_where=auth_user_id.isnot(None),
        ),
    )

class OnboardingInvitacion(Base):
    __tablename__ = "onboarding_invitacion"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    whatsapp_id = Column(String, nullable=False)
    token_hash = Column(String, nullable=False)
    estado = Column(String, nullable=False, default="pendiente")
    expira_en = Column(DateTime(timezone=True), nullable=False)
    intentos = Column(Integer, nullable=False, default=0)
    reenvios = Column(Integer, nullable=False, default=0)
    ultimo_envio_en = Column(DateTime(timezone=True), nullable=True)
    usuario_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("usuario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    consumida_en = Column(DateTime(timezone=True), nullable=True)
    revocada_en = Column(DateTime(timezone=True), nullable=True)
    creado_en = Column(DateTime(timezone=True), nullable=False, default=func.now())
    actualizado_en = Column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("token_hash", name="onboarding_invitacion_token_hash_key"),
        CheckConstraint(
            "trim(token_hash) <> ''",
            name="onboarding_invitacion_token_hash_no_vacio_check",
        ),
        CheckConstraint(
            "trim(whatsapp_id) <> ''",
            name="onboarding_invitacion_whatsapp_id_no_vacio_check",
        ),
        CheckConstraint(
            "estado IN ('pendiente', 'consumida', 'revocada', 'vencida')",
            name="onboarding_invitacion_estado_check",
        ),
        CheckConstraint(
            "intentos >= 0",
            name="onboarding_invitacion_intentos_check",
        ),
        CheckConstraint(
            "reenvios >= 0",
            name="onboarding_invitacion_reenvios_check",
        ),
        CheckConstraint(
            "expira_en > creado_en",
            name="onboarding_invitacion_expiracion_check",
        ),
        CheckConstraint(
            "(estado = 'pendiente' AND usuario_id IS NULL "
            "AND consumida_en IS NULL AND revocada_en IS NULL) OR "
            "(estado = 'consumida' AND usuario_id IS NOT NULL "
            "AND consumida_en IS NOT NULL AND revocada_en IS NULL) OR "
            "(estado = 'revocada' AND usuario_id IS NULL "
            "AND consumida_en IS NULL AND revocada_en IS NOT NULL) OR "
            "(estado = 'vencida' AND usuario_id IS NULL "
            "AND consumida_en IS NULL AND revocada_en IS NULL)",
            name="onboarding_invitacion_estado_campos_check",
        ),
        Index("onboarding_invitacion_whatsapp_id_idx", "whatsapp_id"),
        Index("onboarding_invitacion_estado_expira_idx", "estado", "expira_en"),
        Index(
            "onboarding_invitacion_whatsapp_pendiente_uidx",
            "whatsapp_id",
            unique=True,
            postgresql_where=(estado == "pendiente"),
            sqlite_where=(estado == "pendiente"),
        ),
    )

class AcuerdoVersion(Base):
    __tablename__ = "acuerdo_version"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version = Column(String, nullable=False)
    contenido = Column(String, nullable=False)
    creado_en = Column(DateTime, default=func.now())
    esta_vigente = Column(Boolean, nullable=False, default=False)
    vigente_desde = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("version", name="acuerdo_version_version_key"),
        CheckConstraint(
            "esta_vigente = false OR vigente_desde IS NOT NULL",
            name="acuerdo_version_vigencia_fecha_check",
        ),
        Index(
            "acuerdo_version_vigente_uidx",
            "esta_vigente",
            unique=True,
            postgresql_where=(esta_vigente.is_(True)),
            sqlite_where=(esta_vigente.is_(True)),
        ),
    )

class AcuerdoAceptado(Base):
    __tablename__ = "acuerdo_aceptado"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    version_acuerdo_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("acuerdo_version.id"),
        nullable=False,
    )
    aceptado_en = Column(DateTime(timezone=True), nullable=False, default=func.now())
    origen = Column(String, nullable=False, default="web_onboarding")

    __table_args__ = (
        UniqueConstraint(
            "usuario_id",
            "version_acuerdo_id",
            name="acuerdo_aceptado_usuario_version_key",
        ),
    )

class Categoria(Base):
    __tablename__ = "categorias"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True), ForeignKey("usuario.id"))
    nombre = Column(String, nullable=False)
    es_default = Column(Boolean, default=False)
    esta_eliminado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=datetime.utcnow)

class LimiteCategoria(Base):
    __tablename__ = "limite_categoria"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    categoria_id = Column(Uuid(as_uuid=True), ForeignKey("categorias.id"), nullable=False)
    cantidad_max = Column(Numeric, nullable=False)
    inicio_periodo = Column(Date, nullable=False)
    fin_periodo = Column(Date, nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint('cantidad_max > 0', name='limite_categoria_cantidad_max_check'),
    )

class Recordatorio(Base):
    __tablename__ = "recordatorio"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    titulo = Column(String, nullable=False)
    descripcion = Column(String)
    dia_del_mes = Column(Integer, nullable=False)
    monto = Column(Numeric)
    moneda = Column(String, default="ARS")
    estado = Column(String, nullable=False, default="activo")
    ultimo_aviso_enviado = Column(Date)
    creado_en = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("dia_del_mes BETWEEN 1 AND 31", name="recordatorio_dia_del_mes_check"),
        CheckConstraint("estado IN ('activo', 'pausado', 'eliminado')", name="recordatorio_estado_check"),
        CheckConstraint("monto IS NULL OR monto > 0", name="recordatorio_monto_check"),
    )

class Evento(Base):
    __tablename__ = "evento"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True))
    agregar_tipo = Column(String, nullable=False)
    agregar_id = Column(Uuid(as_uuid=True), nullable=False)
    tipo_evento = Column(String, nullable=False)
    carga = Column(JSON)
    creado_en = Column(DateTime, default=datetime.utcnow)

class MovimientoFinanciero(Base):
    __tablename__ = "movimientos_financieros"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(Uuid(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    categoria_id = Column(Uuid(as_uuid=True), ForeignKey("categorias.id"))
    tipo = Column(String, nullable=False)
    cantidad = Column(Numeric, nullable=False)
    moneda = Column(String, nullable=False, default="ARS")
    descripcion = Column(String)
    fecha_movimiento = Column(Date, nullable=False, default=date.today)
    origen = Column(String, nullable=False, default="whatsapp_text")
    whatsapp_message_id = Column(String)
    creado_en = Column(DateTime(timezone=True), nullable=False, default=func.now())
    actualizado_en = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("tipo IN ('ingreso', 'egreso')", name="movimientos_financieros_tipo_check"),
        CheckConstraint("cantidad > 0", name="movimientos_financieros_cantidad_check"),
        Index(
            "movimientos_financieros_whatsapp_message_id_uidx",
            "whatsapp_message_id",
            unique=True,
            postgresql_where=whatsapp_message_id.isnot(None),
            sqlite_where=whatsapp_message_id.isnot(None),
        ),
    )
