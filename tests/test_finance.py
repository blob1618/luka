import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.models.database import Base, Categoria, MovimientoFinanciero, Usuario
from app.services.finance import FinanceService


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)

    session = testing_session_local()
    try:
        yield {
            "session": session,
            "session_factory": testing_session_local,
        }
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def create_user(session, whatsapp_id="5491111111111"):
    user = Usuario(
        nombre="Test User",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.commit()
    return user


def create_category(session, user_id, nombre="Comida", esta_eliminado=False):
    category = Categoria(
        usuario_id=user_id,
        nombre=nombre,
        es_default=False,
        esta_eliminado=esta_eliminado,
    )
    session.add(category)
    session.commit()
    return category


def movement_payload(**overrides):
    payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 1500,
        "currency": "ars",
        "description": "almuerzo",
        "expense": "almuerzo",
        "category": " comida ",
    }
    payload.update(overrides)
    return payload


def count_movements(session):
    return session.query(MovimientoFinanciero).count()


def test_register_movement_existing_user_and_category(db_context):
    session = db_context["session"]
    user = create_user(session)
    category = create_category(session, user.id, nombre="Comida")

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.1",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )

    movement = session.query(MovimientoFinanciero).one()

    assert result.status == "registered"
    assert result.duplicate is False
    assert result.movement_id == str(movement.id)
    assert result.user_id == str(user.id)
    assert movement.usuario_id == user.id
    assert movement.categoria_id == category.id
    assert movement.tipo == "egreso"
    assert movement.moneda == "ARS"
    assert movement.descripcion == "almuerzo"
    assert movement.whatsapp_message_id == "wamid.1"


def test_register_movement_user_not_found_does_not_save(db_context):
    session = db_context["session"]

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5499999999999",
        whatsapp_message_id="wamid.2",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )

    assert result.status == "user_not_found"
    assert result.movement_id is None
    assert count_movements(session) == 0


def test_register_movement_without_matching_category_saves_null_category(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.3",
        original_text="Gaste 1500 en farmacia",
        llm_result=movement_payload(category="salud"),
    )

    movement = session.query(MovimientoFinanciero).one()

    assert result.status == "registered"
    assert movement.categoria_id is None


def test_register_movement_deleted_category_is_not_used(db_context):
    session = db_context["session"]
    user = create_user(session)
    create_category(session, user.id, nombre="Comida", esta_eliminado=True)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.4",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )

    movement = session.query(MovimientoFinanciero).one()

    assert result.status == "registered"
    assert movement.categoria_id is None


@pytest.mark.parametrize("amount", [None, "abc", 0, -10, "NaN"])
def test_register_movement_invalid_amount_is_invalid_data(db_context, amount):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.invalid",
        original_text="Gaste en almuerzo",
        llm_result=movement_payload(amount=amount),
    )

    assert result.status == "invalid_data"
    assert count_movements(session) == 0


def test_register_movement_invalid_movement_type_is_invalid_data(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.invalid-type",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(movement_type="transferencia"),
    )

    assert result.status == "invalid_data"
    assert count_movements(session) == 0


def test_register_movement_missing_type_non_financial_intent_is_not_a_movement(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.not-movement",
        original_text="Hola",
        llm_result={"intent": "greeting", "reply_text": "Hola"},
    )

    assert result.status == "not_a_movement"
    assert count_movements(session) == 0


def test_register_movement_missing_type_reminder_with_amount_is_not_a_movement(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.reminder",
        original_text="Recordame pagar 1500",
        llm_result={"intent": "reminder", "amount": 1500, "reply_text": "Recordatorio"},
    )

    assert result.status == "not_a_movement"
    assert count_movements(session) == 0


def test_register_movement_missing_type_budget_query_with_amount_is_not_a_movement(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.budget-query",
        original_text="Cuanto me queda si gaste 1500?",
        llm_result={"intent": "budget_query", "amount": 1500, "reply_text": "Consultando"},
    )

    assert result.status == "not_a_movement"
    assert count_movements(session) == 0


def test_register_movement_missing_type_financial_intent_is_invalid_data(db_context):
    session = db_context["session"]
    create_user(session)
    payload = movement_payload()
    payload.pop("movement_type")

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.missing-type",
        original_text="Gaste 1500 en almuerzo",
        llm_result=payload,
    )

    assert result.status == "invalid_data"
    assert count_movements(session) == 0


def test_register_movement_invalid_type_non_expense_intent_is_not_a_movement(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.invalid-type-reminder",
        original_text="Recordame pagar 1500",
        llm_result={
            "intent": "reminder",
            "movement_type": "transferencia",
            "amount": 1500,
        },
    )

    assert result.status == "not_a_movement"
    assert count_movements(session) == 0


def test_register_movement_duplicate_whatsapp_message_id_does_not_save_twice(db_context):
    session = db_context["session"]
    create_user(session)

    first = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.duplicate",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )
    second = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.duplicate",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )

    assert first.status == "registered"
    assert second.status == "duplicate"
    assert second.duplicate is True
    assert second.movement_id == first.movement_id
    assert count_movements(session) == 1


def test_register_movement_commit_error_returns_persistence_error(db_context, monkeypatch):
    session = db_context["session"]
    create_user(session)
    session_factory = db_context["session_factory"]

    class FailingCommitSession:
        def __init__(self, wrapped_session):
            self._wrapped_session = wrapped_session

        def __getattr__(self, name):
            return getattr(self._wrapped_session, name)

        def commit(self):
            raise RuntimeError("commit failed")

    monkeypatch.setattr(
        finance_module,
        "SessionLocal",
        lambda: FailingCommitSession(session_factory()),
    )

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.commit-error",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(),
    )

    assert result.status == "persistence_error"
    assert count_movements(session) == 0


class FakeQuery:
    def __init__(self, result):
        self.result = result

    def filter(self, *args):
        return self

    def first(self):
        return self.result


class IntegrityErrorSession:
    def __init__(self, user):
        self.user = user
        self.added = []
        self.rollback_called = False
        self.closed = False

    def query(self, model):
        if model is Usuario:
            return FakeQuery(self.user)
        return FakeQuery(None)

    def add(self, movement):
        self.added.append(movement)

    def commit(self):
        raise IntegrityError("insert", {}, Exception("duplicate"))

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.closed = True


def test_register_movement_integrity_error_rechecks_and_returns_duplicate(monkeypatch):
    user_id = uuid.uuid4()
    movement_id = uuid.uuid4()
    user = type("User", (), {"id": user_id})()
    duplicate = type("Duplicate", (), {"id": movement_id, "usuario_id": user_id})()
    fake_session = IntegrityErrorSession(user)
    duplicate_checks = iter([None, duplicate])

    monkeypatch.setattr(finance_module, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        FinanceService,
        "_find_duplicate",
        staticmethod(lambda session, message_id: next(duplicate_checks)),
    )

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.integrity-duplicate",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(category=None),
    )

    assert result.status == "duplicate"
    assert result.duplicate is True
    assert result.movement_id == str(movement_id)
    assert result.user_id == str(user_id)
    assert fake_session.rollback_called is True
    assert fake_session.closed is True


def test_register_movement_integrity_error_rechecks_and_returns_persistence_error(monkeypatch):
    user = type("User", (), {"id": uuid.uuid4()})()
    fake_session = IntegrityErrorSession(user)
    duplicate_checks = iter([None, None])

    monkeypatch.setattr(finance_module, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        FinanceService,
        "_find_duplicate",
        staticmethod(lambda session, message_id: next(duplicate_checks)),
    )

    result = FinanceService.register_movement_from_whatsapp_text(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.integrity-missing",
        original_text="Gaste 1500 en almuerzo",
        llm_result=movement_payload(category=None),
    )

    assert result.status == "persistence_error"
    assert result.duplicate is False
    assert result.movement_id is None
    assert fake_session.rollback_called is True
    assert fake_session.closed is True


def test_check_dynamic_budget():
    user_id = 1
    new_expense = 5000.0
    category = "ocio"

    result = FinanceService.check_dynamic_budget(user_id, new_expense, category)

    assert "buen registro" in result.lower()
    assert "ropa" in result.lower()


def test_generate_expense_chart():
    expenses = {
        "Comida": 15000.0,
        "Transporte": 5000.0,
        "Ocio": 2000.0,
    }

    chart_bytes = FinanceService.generate_expense_chart(expenses)

    assert isinstance(chart_bytes, bytes)
    assert len(chart_bytes) > 0
    assert chart_bytes.startswith(b"\x89PNG")
