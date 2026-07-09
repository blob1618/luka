import os
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

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

class User(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    whatsapp_id = Column(String, unique=True, index=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

class Expense(Base):
    __tablename__ = "gastos"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    monto = Column(Float, nullable=False)
    categoria = Column(String, nullable=False)
    descripcion = Column(String, nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

class Budget(Base):
    __tablename__ = "presupuestos"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    categoria = Column(String, nullable=False)
    monto_limite = Column(Float, nullable=False)

class Reminder(Base):
    __tablename__ = "recordatorios"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    titulo = Column(String, nullable=False)
    fecha_vencimiento = Column(DateTime, nullable=False)
    activo = Column(Integer, default=1)
