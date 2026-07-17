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
    return sessionmaker(bind=engine, expire_on_commit=False)()


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


def _seed_reminder(session, user, titulo, dia_del_mes, monto=None, estado="activo"):
    rec = Recordatorio(
        usuario_id=user.id,
        titulo=titulo,
        dia_del_mes=dia_del_mes,
        monto=monto,
        estado=estado,
    )
    session.add(rec)
    session.commit()
    return rec


class TestReminderServiceCreate:
    def test_create_reminder_success(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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


class TestReminderServiceCrud:
    def test_list_reminders_only_active_sorted(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155557777")
        _seed_reminder(session, user, titulo="Internet", dia_del_mes=20)
        _seed_reminder(session, user, titulo="Luz", dia_del_mes=10)
        _seed_reminder(session, user, titulo="Viejo", dia_del_mes=5, estado="pausado")
        session.close()

        result = ReminderService.list_reminders(user.id)

        assert result.status == "ok"
        assert [item["titulo"] for item in result.reminders] == ["Luz", "Internet"]
        assert all(item["estado"] == "activo" for item in result.reminders)

    def test_update_reminder_success(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155558888")
        reminder = _seed_reminder(session, user, titulo="Luz", dia_del_mes=10, monto=Decimal("1500"))
        session.close()

        result = ReminderService.update_reminder(
            sender_phone="5491155558888",
            reminder_id=str(reminder.id),
            llm_result={
                "reminder_concept": "Internet",
                "reminder_day": 12,
                "reminder_amount": 2000,
                "reminder_currency": "ars",
            },
        )

        assert result.status == "updated"
        session = TestSession()
        saved = session.query(Recordatorio).filter(Recordatorio.id == reminder.id).first()
        assert saved.titulo == "Internet"
        assert saved.dia_del_mes == 12
        assert saved.monto == Decimal("2000")
        assert saved.moneda == "ARS"
        session.close()

    def test_update_reminder_rejects_other_user(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        owner = _seed_user(session, "5491155551111")
        other = _seed_user(session, "5491155552222")
        reminder = _seed_reminder(session, owner, titulo="Luz", dia_del_mes=10)
        session.close()

        result = ReminderService.update_reminder(
            sender_phone="5491155552222",
            reminder_id=str(reminder.id),
            llm_result={
                "reminder_concept": "Internet",
                "reminder_day": 12,
            },
        )

        assert result.status in {"not_owned", "not_found"}

    def test_pause_activate_and_delete_reminder(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155553333")
        reminder = _seed_reminder(session, user, titulo="Internet", dia_del_mes=20)
        session.close()

        paused = ReminderService.pause_reminder("5491155553333", str(reminder.id))
        assert paused.status == "paused"

        activated = ReminderService.activate_reminder("5491155553333", str(reminder.id))
        assert activated.status == "activated"

        deleted = ReminderService.delete_reminder("5491155553333", str(reminder.id))
        assert deleted.status == "deleted"

        session = TestSession()
        saved = session.query(Recordatorio).filter(Recordatorio.id == reminder.id).first()
        assert saved.estado == "eliminado"
        session.close()

    def test_create_reminder_missing_day(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
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


class TestReminderFindByTitle:
    def _setup(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)
        session = TestSession()
        user = _seed_user(session, "5491155560000")
        session.close()
        return TestSession, user

    def test_find_by_title_exact_match(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="wifi", dia_del_mes=4)
        session.close()

        reminder, error = ReminderService.find_by_title("5491155560000", "wifi")
        assert error is None
        assert reminder is not None
        assert reminder.titulo == "wifi"

    def test_find_by_title_partial_match(self, monkeypatch):
        """'luz' should match 'pago de luz'."""
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="pago de luz", dia_del_mes=10)
        session.close()

        reminder, error = ReminderService.find_by_title("5491155560000", "luz")
        assert error is None
        assert reminder is not None
        assert reminder.titulo == "pago de luz"

    def test_find_by_title_case_insensitive(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="Netflix", dia_del_mes=15)
        session.close()

        reminder, error = ReminderService.find_by_title("5491155560000", "netflix")
        assert error is None
        assert reminder is not None

    def test_find_by_title_exact_takes_priority_over_partial(self, monkeypatch):
        """If 'luz' and 'pago de luz' both exist, exact 'luz' wins."""
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="pago de luz", dia_del_mes=10)
        _seed_reminder(session, user, titulo="luz", dia_del_mes=5)
        session.close()

        reminder, error = ReminderService.find_by_title("5491155560000", "luz")
        assert error is None
        assert reminder is not None
        assert reminder.titulo == "luz"

    def test_find_by_title_not_found(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)

        reminder, error = ReminderService.find_by_title("5491155560000", "inexistente")
        assert reminder is None
        assert error is not None
        assert error.status == "not_found"

    def test_find_by_title_ignores_eliminated(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="agua", dia_del_mes=18, estado="eliminado")
        session.close()

        reminder, error = ReminderService.find_by_title("5491155560000", "agua")
        assert reminder is None
        assert error.status == "not_found"

    def test_find_by_title_user_not_found(self, monkeypatch):
        TestSession, _ = self._setup(monkeypatch)

        reminder, error = ReminderService.find_by_title("0000000000", "luz")
        assert reminder is None
        assert error.status == "user_not_found"


class TestReminderTitleExists:
    def test_title_exists_true(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        session = TestSession()
        user = _seed_user(session, "5491155561111")
        _seed_reminder(session, user, titulo="wifi", dia_del_mes=4)

        assert ReminderService.title_exists(session, user.id, "wifi") is True
        assert ReminderService.title_exists(session, user.id, "WIFI") is True
        session.close()

    def test_title_exists_false(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        session = TestSession()
        user = _seed_user(session, "5491155562222")
        _seed_reminder(session, user, titulo="wifi", dia_del_mes=4)

        assert ReminderService.title_exists(session, user.id, "internet") is False
        session.close()

    def test_title_exists_ignores_eliminated(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        session = TestSession()
        user = _seed_user(session, "5491155563333")
        _seed_reminder(session, user, titulo="agua", dia_del_mes=18, estado="eliminado")

        assert ReminderService.title_exists(session, user.id, "agua") is False
        session.close()


class TestReminderByTitle:
    def _setup(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)
        session = TestSession()
        user = _seed_user(session, "5491155570000")
        session.close()
        return TestSession, user

    def test_pause_by_title(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="wifi", dia_del_mes=4)
        session.close()

        result = ReminderService.pause_by_title("5491155570000", "wifi")
        assert result.status == "paused"

        session = TestSession()
        rec = session.query(Recordatorio).first()
        assert rec.estado == "pausado"
        session.close()

    def test_activate_by_title(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="netflix", dia_del_mes=15, estado="pausado")
        session.close()

        result = ReminderService.activate_by_title("5491155570000", "netflix")
        assert result.status == "activated"

    def test_delete_by_title(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="luz", dia_del_mes=10)
        session.close()

        result = ReminderService.delete_by_title("5491155570000", "luz")
        assert result.status == "deleted"

    def test_delete_by_title_not_found(self, monkeypatch):
        TestSession, user = self._setup(monkeypatch)

        result = ReminderService.delete_by_title("5491155570000", "inexistente")
        assert result.status == "not_found"

    def test_pause_by_title_partial_match(self, monkeypatch):
        """'luz' matches 'pago de luz'."""
        TestSession, user = self._setup(monkeypatch)
        session = TestSession()
        _seed_reminder(session, user, titulo="pago de luz", dia_del_mes=10)
        session.close()

        result = ReminderService.pause_by_title("5491155570000", "luz")
        assert result.status == "paused"


class TestReminderDuplicateTitle:
    def test_create_duplicate_title_rejected(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155580000")
        _seed_reminder(session, user, titulo="wifi", dia_del_mes=4)
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155580000",
            llm_result={"reminder_concept": "wifi", "reminder_day": 10},
        )
        assert result.status == "duplicate_title"
        assert "wifi" in result.message

    def test_create_duplicate_title_case_insensitive(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155581111")
        _seed_reminder(session, user, titulo="Luz", dia_del_mes=10)
        session.close()

        result = ReminderService.create_reminder(
            sender_phone="5491155581111",
            llm_result={"reminder_concept": "LUZ", "reminder_day": 15},
        )
        assert result.status == "duplicate_title"

    def test_create_after_delete_allowed(self, monkeypatch):
        """After soft-deleting a reminder, the same title can be reused."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("app.services.reminder.SessionLocal", TestSession)

        session = TestSession()
        user = _seed_user(session, "5491155582222")
        reminder = _seed_reminder(session, user, titulo="luz", dia_del_mes=10)
        session.close()

        # Soft-delete
        ReminderService.delete_reminder("5491155582222", str(reminder.id))

        # Should now be allowed
        result = ReminderService.create_reminder(
            sender_phone="5491155582222",
            llm_result={"reminder_concept": "luz", "reminder_day": 15},
        )
        assert result.status == "created"


class TestConceptValidation:
    """Tests for _validate_reminder_concept and _extract_concept_from_text."""

    def setup_method(self):
        from app.main import _validate_reminder_concept, _extract_concept_from_text
        self.validate = _validate_reminder_concept
        self.extract = _extract_concept_from_text

    def test_valid_short_concept_passes_through(self):
        assert self.validate("luz", "recordatorio de luz") == "luz"

    def test_valid_multiword_concept_passes_through(self):
        assert self.validate("seguro de vida", "recordatorio seguro de vida") == "seguro de vida"

    def test_full_text_concept_is_extracted(self):
        result = self.validate(
            "hola quiero que crees un recordatorio para el wifi el dia 5",
            "hola quiero que crees un recordatorio para el wifi el dia 5",
        )
        assert result == "wifi"

    def test_none_concept_extracted_from_text(self):
        result = self.validate(None, "recordatorio de luz")
        assert result == "luz"

    def test_empty_concept_extracted_from_text(self):
        result = self.validate("", "recordatorio de internet el 10")
        assert result == "internet"

    def test_long_concept_with_stopwords_is_extracted(self):
        result = self.validate(
            "quiero crear un recordatorio para internet",
            "quiero crear un recordatorio para internet",
        )
        assert result == "internet"

    def test_extract_from_avisame_pattern(self):
        assert self.extract("avisame del cable") == "cable"

    def test_extract_from_recordame_pattern(self):
        assert self.extract("recordame pagar la luz") == "luz"

    def test_extract_from_crear_pattern(self):
        result = self.extract("hola quiero crear un recordatorio para el wifi el dia 5")
        assert result == "wifi"

    def test_extract_no_keyword_returns_none(self):
        assert self.extract("hola como estas") is None
