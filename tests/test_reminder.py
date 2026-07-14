import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Recordatorio, Usuario


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestRecordatorioModel:
    def test_create_recordatorio_minimal(self):
        session = _make_session()
        user = Usuario(id=uuid.uuid4(), nombre="Test", email="t@t.com")
        session.add(user)
        session.flush()

        rec = Recordatorio(
            usuario_id=user.id,
            titulo="Luz",
            dia_del_mes=15,
        )
        session.add(rec)
        session.commit()

        saved = session.query(Recordatorio).first()
        assert saved.titulo == "Luz"
        assert saved.dia_del_mes == 15
        assert saved.estado == "activo"
        assert saved.monto is None
        assert saved.moneda == "ARS"
        assert saved.ultimo_aviso_enviado is None

    def test_create_recordatorio_with_monto(self):
        session = _make_session()
        user = Usuario(id=uuid.uuid4(), nombre="Test", email="t2@t.com")
        session.add(user)
        session.flush()

        rec = Recordatorio(
            usuario_id=user.id,
            titulo="Alquiler",
            dia_del_mes=1,
            monto=Decimal("350000"),
            moneda="ARS",
        )
        session.add(rec)
        session.commit()

        saved = session.query(Recordatorio).first()
        assert saved.monto == Decimal("350000")
        assert saved.dia_del_mes == 1


class TestUsuarioUltimoMensaje:
    def test_ultimo_mensaje_en_nullable(self):
        session = _make_session()
        user = Usuario(id=uuid.uuid4(), nombre="Test", email="t3@t.com")
        session.add(user)
        session.commit()

        saved = session.query(Usuario).first()
        assert saved.ultimo_mensaje_en is None

    def test_ultimo_mensaje_en_set(self):
        session = _make_session()
        user = Usuario(
            id=uuid.uuid4(),
            nombre="Test",
            email="t4@t.com",
            ultimo_mensaje_en=datetime(2026, 7, 14, 10, 0, 0),
        )
        session.add(user)
        session.commit()

        saved = session.query(Usuario).first()
        assert saved.ultimo_mensaje_en is not None
