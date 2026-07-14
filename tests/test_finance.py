import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.models.database import Base, Categoria, MovimientoFinanciero, Usuario
from app.services.finance import FinanceService
from unittest.mock import patch, MagicMock
from datetime import datetime, date
from uuid import UUID


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


# ── Tests de registro de movimientos (STK-35) ────────────────────────

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


# ── Tests legacy ──────────────────────────────────────────────────────

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


# ── Tests de validación (STK-85) ─────────────────────────────────────

def test_validate_budget_amount_valid():
    is_valid, msg = FinanceService.validate_budget_amount(50000.0)
    assert is_valid is True
    assert msg == ""


def test_validate_budget_amount_zero():
    is_valid, msg = FinanceService.validate_budget_amount(0)
    assert is_valid is False
    assert "mayor a cero" in msg


def test_validate_budget_amount_negative():
    is_valid, msg = FinanceService.validate_budget_amount(-100)
    assert is_valid is False
    assert "mayor a cero" in msg


def test_validate_budget_amount_none():
    is_valid, msg = FinanceService.validate_budget_amount(None)
    assert is_valid is False
    assert "vacío" in msg


def test_validate_budget_amount_too_high():
    is_valid, msg = FinanceService.validate_budget_amount(10_000_000_000)
    assert is_valid is False
    assert "demasiado alto" in msg


def test_validate_budget_amount_string():
    is_valid, msg = FinanceService.validate_budget_amount("abc")
    assert is_valid is False
    assert "numérico" in msg


# ── Tests de _obtener_rango_mes ────────────────────────────────────

def test_obtener_rango_mes_default():
    inicio, fin = FinanceService._obtener_rango_mes()
    hoy = datetime.utcnow()
    assert inicio.month == hoy.month
    assert inicio.year == hoy.year
    assert inicio.day == 1


def test_obtener_rango_mes_especifico():
    inicio, fin = FinanceService._obtener_rango_mes("2026-07")
    assert inicio == date(2026, 7, 1)
    assert fin == date(2026, 7, 31)


def test_obtener_rango_mes_diciembre():
    inicio, fin = FinanceService._obtener_rango_mes("2026-12")
    assert inicio == date(2026, 12, 1)
    assert fin == date(2026, 12, 31)


def test_obtener_rango_mes_invalido():
    with pytest.raises(ValueError, match="inválido"):
        FinanceService._obtener_rango_mes("julio-2026")


# ── Tests de _obtener_categoria_por_nombre ──────────────────────────

def test_obtener_categoria_por_nombre_usuario():
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    mock_categoria = MagicMock()
    mock_categoria.nombre = "Comida"
    mock_categoria.id = UUID("00000000-0000-0000-0000-000000000010")

    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = mock_categoria
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "comida")
    assert result is not None
    assert result.nombre == "Comida"


def test_obtener_categoria_por_nombre_default():
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    mock_categoria = MagicMock()
    mock_categoria.nombre = "Supermercado"
    mock_categoria.id = UUID("00000000-0000-0000-0000-000000000020")

    mock_query1 = MagicMock()
    mock_filter1 = MagicMock()
    mock_filter1.first.return_value = None
    mock_query1.filter.return_value = mock_filter1

    mock_query2 = MagicMock()
    mock_filter2 = MagicMock()
    mock_filter2.first.return_value = mock_categoria
    mock_query2.filter.return_value = mock_filter2

    mock_db.query.side_effect = [mock_query1, mock_query2]

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "supermercado")
    assert result is not None
    assert result.nombre == "Supermercado"


def test_obtener_categoria_por_nombre_no_encontrada():
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = None
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "inexistente")
    assert result is None


# ── Tests de set_budget_limit (STK-86 + STK-87) ─────────────────────

@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_create(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = None
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="salidas",
        amount=50000.0,
        month="2026-07",
    )

    assert result["success"] is True
    assert "$50,000" in result["message"]
    assert "Salidas" in result["message"]
    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_update(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 30000.0
    mock_limite.id = UUID("00000000-0000-0000-0000-000000000100")

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = mock_limite
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="salidas",
        amount=50000.0,
        month="2026-07",
    )

    assert result["success"] is True
    assert "Actualicé" in result["message"]
    assert mock_limite.cantidad_max == 50000.0
    mock_db.commit.assert_called_once()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_invalid_amount(mock_session_local):
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="comida",
        amount=-100,
    )

    assert result["success"] is False
    assert "mayor a cero" in result["message"]
    mock_session_local.return_value.commit.assert_not_called()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_invalid_month_format(mock_session_local):
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="comida",
        amount=10000,
        month="julio-2026",
    )

    assert result["success"] is False
    assert "inválido" in result["message"].lower()
    mock_session_local.return_value.commit.assert_not_called()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_categoria_no_encontrada(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = None
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="inexistente",
        amount=10000,
        month="2026-07",
    )

    assert result["success"] is False
    assert "no encontré" in result["message"].lower()


# ── Tests de get_budget_limit (STK-88) ──────────────────────────────

@patch("app.services.finance.SessionLocal")
def test_get_budget_limit_found(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 50000.0
    mock_limite.inicio_periodo = date(2026, 7, 1)
    mock_limite.fin_periodo = date(2026, 7, 31)

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = mock_limite
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.get_budget_limit(
        user_id=user_id,
        category="salidas",
        month="2026-07",
    )

    assert result["found"] is True
    assert result["category"] == "Salidas"
    assert result["amount"] == 50000.0
    assert result["month"] == "2026-07"


@patch("app.services.finance.SessionLocal")
def test_get_budget_limit_not_found(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = None
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.get_budget_limit(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["found"] is False
    assert "no tenés" in result["message"].lower()


# ── Tests de get_all_budget_limits ──────────────────────────────────

@patch("app.services.finance.SessionLocal")
def test_get_all_budget_limits(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    cat_salidas = UUID("00000000-0000-0000-0000-000000000010")
    cat_comida = UUID("00000000-0000-0000-0000-000000000020")

    mock_lim1 = MagicMock()
    mock_lim1.id = UUID("00000000-0000-0000-0000-000000000100")
    mock_lim1.categoria_id = cat_salidas
    mock_lim1.cantidad_max = 50000.0
    mock_lim1.inicio_periodo = date(2026, 7, 1)

    mock_lim2 = MagicMock()
    mock_lim2.id = UUID("00000000-0000-0000-0000-000000000200")
    mock_lim2.categoria_id = cat_comida
    mock_lim2.cantidad_max = 30000.0
    mock_lim2.inicio_periodo = date(2026, 7, 1)

    mock_cat1 = MagicMock()
    mock_cat1.nombre = "Salidas"
    mock_cat2 = MagicMock()
    mock_cat2.nombre = "Comida"

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.all.return_value = [mock_lim1, mock_lim2]
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_query_cat1 = MagicMock()
    mock_filter_cat1 = MagicMock()
    mock_filter_cat1.first.return_value = mock_cat1
    mock_query_cat1.filter.return_value = mock_filter_cat1

    mock_query_cat2 = MagicMock()
    mock_filter_cat2 = MagicMock()
    mock_filter_cat2.first.return_value = mock_cat2
    mock_query_cat2.filter.return_value = mock_filter_cat2

    mock_db.query.side_effect = [mock_query_lim, mock_query_cat1, mock_query_cat2]

    result = FinanceService.get_all_budget_limits(user_id=user_id, month="2026-07")

    assert len(result) == 2
    assert result[0]["category"] == "Salidas"
    assert result[1]["category"] == "Comida"


# ── Tests de check_budget_status ────────────────────────────────────

@patch("app.services.finance.SessionLocal")
def test_check_budget_status_under_limit(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 30000.0
    mock_limite.inicio_periodo = date(2026, 7, 1)
    mock_limite.fin_periodo = date(2026, 7, 31)

    mock_q1 = MagicMock()
    mock_f1 = MagicMock()
    mock_f1.first.return_value = mock_categoria
    mock_q1.filter.return_value = mock_f1

    mock_q2 = MagicMock()
    mock_f2 = MagicMock()
    mock_f2.first.return_value = mock_limite
    mock_q2.filter.return_value = mock_f2

    mock_q3 = MagicMock()
    mock_f3 = MagicMock()
    mock_f3.all.return_value = [(15000.0,)]
    mock_q3.filter.return_value = mock_f3

    mock_db.query.side_effect = [mock_q1, mock_q2, mock_q3]

    result = FinanceService.check_budget_status(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["has_limit"] is True
    assert result["limit"] == 30000.0
    assert result["spent"] == 15000.0
    assert result["remaining"] == 15000.0
    assert result["percentage"] == 50.0


@patch("app.services.finance.SessionLocal")
def test_check_budget_status_no_limit(mock_session_local):
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_q1 = MagicMock()
    mock_f1 = MagicMock()
    mock_f1.first.return_value = mock_categoria
    mock_q1.filter.return_value = mock_f1

    mock_q2 = MagicMock()
    mock_f2 = MagicMock()
    mock_f2.first.return_value = None
    mock_q2.filter.return_value = mock_f2

    mock_db.query.side_effect = [mock_q1, mock_q2]

    result = FinanceService.check_budget_status(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["has_limit"] is False