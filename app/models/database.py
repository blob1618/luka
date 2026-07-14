import os
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Boolean, DateTime, Date, Text,
    ForeignKey, create_engine, UniqueConstraint, CheckConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker

# Obtener DATABASE_URL del entorno, usando SQLite como fallback para desarrollo local
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./luka.db")

# Manejar la conexión a PostgreSQL de Supabase con psycopg3
if DATABASE_URL.startswith("postgresql"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def generate_uuid():
    """Genera un UUID string para compatibilidad con SQLite."""
    return str(uuid.uuid4())


class Usuario(Base):
    """Mapea la tabla 'usuario' de Supabase."""
    __tablename__ = "usuario"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre = Column(String, nullable=True)
    email = Column(String, unique=True, nullable=True)
    whatsapp_id = Column(String, nullable=True, index=True)
    creado_en = Column(DateTime(timezone=True), default=datetime.utcnow)
    actualizado_en = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Categoria(Base):
    """Mapea la tabla 'categorias' de Supabase."""
    __tablename__ = "categorias"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=True)
    nombre = Column(String, nullable=False)
    es_default = Column(Boolean, default=False)
    esta_eliminado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=datetime.utcnow)


class MovimientoFinanciero(Base):
    """Mapea la tabla 'movimientos_financieros' de Supabase."""
    __tablename__ = "movimientos_financieros"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    categoria_id = Column(UUID(as_uuid=True), ForeignKey("categorias.id"), nullable=True)
    tipo = Column(String, nullable=False)  # 'ingreso' | 'egreso'
    cantidad = Column(Float, nullable=False)
    moneda = Column(String, nullable=False, default="ARS")
    descripcion = Column(Text, nullable=True)
    fecha_movimiento = Column(Date, nullable=False, default=datetime.utcnow)
    origen = Column(String, nullable=False, default="whatsapp_text")
    whatsapp_message_id = Column(String, nullable=True)
    creado_en = Column(DateTime(timezone=True), default=datetime.utcnow)
    actualizado_en = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("tipo IN ('ingreso', 'egreso')", name="movimientos_financieros_tipo_check"),
        CheckConstraint("cantidad > 0", name="movimientos_financieros_cantidad_check"),
    )


class LimiteCategoria(Base):
    """Mapea la tabla 'limite_categoria' de Supabase."""
    __tablename__ = "limite_categoria"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    categoria_id = Column(UUID(as_uuid=True), ForeignKey("categorias.id"), nullable=False)
    cantidad_max = Column(Float, nullable=False)
    inicio_periodo = Column(Date, nullable=False)
    fin_periodo = Column(Date, nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("cantidad_max > 0", name="limite_categoria_cantidad_max_check"),
    )


class Recordatorio(Base):
    """Mapea la tabla 'recordatorio' de Supabase."""
    __tablename__ = "recordatorio"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=False)
    titulo = Column(String, nullable=False)
    descripcion = Column(Text, nullable=True)
    recordar_en = Column(DateTime, nullable=False)
    es_recurrente = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=datetime.utcnow)


class Evento(Base):
    """Mapea la tabla 'evento' de Supabase (log de eventos)."""
    __tablename__ = "evento"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=True)
    agregar_tipo = Column(String, nullable=False)
    agregar_id = Column(UUID(as_uuid=True), nullable=False)
    tipo_evento = Column(String, nullable=False)
    carga = Column(Text, nullable=True)  # JSONB en Supabase, text como fallback
    creado_en = Column(DateTime, default=datetime.utcnow)


# Mantener alias para retrocompatibilidad en importaciones
User = Usuario
Expense = MovimientoFinanciero
Budget = LimiteCategoria
Reminder = Recordatorio