import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Recordatorio, Usuario
from app.services.reminder import ReminderService


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

def _seed_user(session, whatsapp_id="5491155551234"):
    user = Usuario(
        id=uuid.uuid4(),
        nombre="Test User",
        email=f"{whatsapp_id}@test.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.commit()
    return user


class TestReminderServiceCreate:
    def test_create_reminder_success(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155551234")
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155551234",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": "Luz",
                "reminder_day": 15,
                "reminder_amount": None,
                "reminder_currency": None,
            },
        )

        assert result.status == "created"
        assert result.reminder_id is not None

        session = TestSession()
        rec = session.query(Recordatorio).first()
        assert rec.titulo == "Luz"
        assert rec.dia_del_mes == 15
        assert rec.estado == "activo"
        assert rec.monto is None
        session.close()

    def test_create_reminder_with_monto(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155559999")
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155559999",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": "Alquiler",
                "reminder_day": 1,
                "reminder_amount": 350000,
                "reminder_currency": "ARS",
            },
        )

        assert result.status == "created"
        session = TestSession()
        rec = session.query(Recordatorio).first()
        assert rec.monto == Decimal("350000")
        assert rec.moneda == "ARS"
        session.close()

    def test_create_reminder_user_not_found(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        result = ReminderService.create_reminder(
            sender_phone="0000000000",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": "Luz",
                "reminder_day": 15,
            },
        )

        assert result.status == "user_not_found"

    def test_create_reminder_missing_concept(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155551234")
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155551234",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": None,
                "reminder_day": 15,
            },
        )

        assert result.status == "invalid_data"

    def test_create_reminder_missing_day(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155551234")
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155551234",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": "Luz",
                "reminder_day": None,
            },
        )

        assert result.status == "invalid_data"
        assert "día" in result.message.lower() or "dia" in result.message.lower()

    def test_create_reminder_day_out_of_range(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155551234")
        session.close()

        for bad_day in [0, 32, -1]:
            result = ReminderService.create_reminder(
                sender_phone="5491155551234",
                llm_result={
                    "intent": "create_reminder",
                    "reminder_concept": "Luz",
                    "reminder_day": bad_day,
                },
            )
            assert result.status == "invalid_data"

    def test_create_reminder_negative_monto(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        _seed_user(session, "5491155551234")
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155551234",
            llm_result={
                "intent": "create_reminder",
                "reminder_concept": "Luz",
                "reminder_day": 15,
                "reminder_amount": -500,
            },
        )

        assert result.status == "invalid_data"
