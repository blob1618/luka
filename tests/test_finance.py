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


# ---------------------------------------------------------------------------
# STK-39: Tests de gestión de categorías
# ---------------------------------------------------------------------------


def test_create_category_new(db_context):
    session = db_context["session"]
    user = create_user(session)

    result = FinanceService.create_category(user.id, "Transporte")

    assert result.status == "created"
    assert result.category_name == "Transporte"
    assert result.category_id is not None

    cat = session.query(Categoria).filter(Categoria.id == uuid.UUID(result.category_id)).first()
    assert cat is not None
    assert cat.nombre == "Transporte"
    assert cat.esta_eliminado is False


def test_create_category_case_insensitive_duplicate(db_context):
    session = db_context["session"]
    user = create_user(session)
    create_category(session, user.id, nombre="Comida")

    result = FinanceService.create_category(user.id, "  comida  ")

    assert result.status == "already_exists"
    assert result.category_name == "Comida"


def test_create_category_reactivates_deleted(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat = create_category(session, user.id, nombre="Comida", esta_eliminado=True)

    result = FinanceService.create_category(user.id, "Comida")

    assert result.status == "created"
    assert result.category_name == "Comida"

    session.refresh(cat)
    assert cat.esta_eliminado is False


def test_create_category_empty_name_returns_error(db_context):
    result = FinanceService.create_category(uuid.uuid4(), "   ")

    assert result.status == "error"


def test_delete_category_existing(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat = create_category(session, user.id, nombre="Comida")

    result = FinanceService.delete_category(user.id, "Comida")

    assert result.status == "deleted"
    assert result.category_name == "Comida"

    session.refresh(cat)
    assert cat.esta_eliminado is True


def test_delete_category_not_found(db_context):
    session = db_context["session"]
    user = create_user(session)

    result = FinanceService.delete_category(user.id, "Inexistente")

    assert result.status == "not_found"


def test_delete_category_case_insensitive(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat = create_category(session, user.id, nombre="Comida")

    result = FinanceService.delete_category(user.id, "  COMIDA  ")

    assert result.status == "deleted"
    session.refresh(cat)
    assert cat.esta_eliminado is True


def test_delete_category_sets_movements_to_null(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat = create_category(session, user.id, nombre="Comida")

    # Crear movimiento con esa categoría
    mov = MovimientoFinanciero(
        usuario_id=user.id,
        categoria_id=cat.id,
        tipo="egreso",
        cantidad=1500,
        moneda="ARS",
        descripcion="almuerzo",
    )
    session.add(mov)
    session.commit()

    result = FinanceService.delete_category(user.id, "Comida")

    assert result.status == "deleted"

    session.refresh(mov)
    assert mov.categoria_id is None


def test_get_categories_with_totals_empty(db_context):
    session = db_context["session"]
    user = create_user(session)

    result = FinanceService.get_categories_with_totals(user.id)

    assert result.status == "ok"
    assert len(result.categories) == 0


def test_get_categories_with_totals_with_movements(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat1 = create_category(session, user.id, nombre="Comida")
    cat2 = create_category(session, user.id, nombre="Sueldo")

    # Egreso en Comida
    session.add(MovimientoFinanciero(
        usuario_id=user.id, categoria_id=cat1.id,
        tipo="egreso", cantidad=5000, moneda="ARS", descripcion="super",
    ))
    # Ingreso en Sueldo
    session.add(MovimientoFinanciero(
        usuario_id=user.id, categoria_id=cat2.id,
        tipo="ingreso", cantidad=250000, moneda="ARS", descripcion="sueldo",
    ))
    session.commit()

    result = FinanceService.get_categories_with_totals(user.id)

    assert result.status == "ok"
    assert len(result.categories) == 2

    cats_by_name = {c.category_name: c for c in result.categories}
    assert cats_by_name["Comida"].total_egresos == 5000
    assert cats_by_name["Comida"].total_ingresos == 0
    assert cats_by_name["Sueldo"].total_ingresos == 250000
    assert cats_by_name["Sueldo"].total_egresos == 0


def test_get_categories_with_totals_excludes_deleted(db_context):
    session = db_context["session"]
    user = create_user(session)
    create_category(session, user.id, nombre="Comida")
    create_category(session, user.id, nombre="Vieja", esta_eliminado=True)

    result = FinanceService.get_categories_with_totals(user.id)

    assert result.status == "ok"
    names = [c.category_name for c in result.categories]
    assert "Comida" in names
    assert "Vieja" not in names


def test_register_movement_with_category_existing(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat = create_category(session, user.id, nombre="Comida")

    result = FinanceService.register_movement_with_category(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.cat1",
        original_text="Gaste 1500 en almuerzo",
        movement_type="egreso",
        amount=1500,
        currency="ARS",
        description="almuerzo",
        category_name="Comida",
    )

    assert result.status == "registered"
    movement = session.query(MovimientoFinanciero).one()
    assert movement.categoria_id == cat.id


def test_register_movement_with_category_create_if_missing(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_with_category(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.cat2",
        original_text="Gaste 2000 en taxi",
        movement_type="egreso",
        amount=2000,
        currency="ARS",
        description="taxi",
        category_name="Transporte",
        create_category_if_missing=True,
    )

    assert result.status == "registered"
    movement = session.query(MovimientoFinanciero).one()
    assert movement.categoria_id is not None
    cat = session.query(Categoria).filter(Categoria.id == movement.categoria_id).first()
    assert cat.nombre == "Transporte"


def test_register_movement_with_category_not_found_no_create(db_context):
    session = db_context["session"]
    create_user(session)

    result = FinanceService.register_movement_with_category(
        sender_phone="5491111111111",
        whatsapp_message_id="wamid.cat3",
        original_text="Gaste 3000 en algo",
        movement_type="egreso",
        amount=3000,
        currency="ARS",
        description="algo",
        category_name="Inexistente",
        create_category_if_missing=False,
    )

    assert result.status == "registered"
    movement = session.query(MovimientoFinanciero).one()
    assert movement.categoria_id is None


# ---------------------------------------------------------------------------
# STK-39 v2: Tests de update_movement_category
# ---------------------------------------------------------------------------


def test_update_movement_category_existing(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat1 = create_category(session, user.id, nombre="Comida")
    cat2 = create_category(session, user.id, nombre="Transporte")

    # Crear movimiento con categoría "Comida"
    mov = MovimientoFinanciero(
        usuario_id=user.id,
        categoria_id=cat1.id,
        tipo="egreso",
        cantidad=1500,
        moneda="ARS",
        descripcion="almuerzo",
    )
    session.add(mov)
    session.commit()
    mov_id = str(mov.id)

    result = FinanceService.update_movement_category(
        movement_id=mov_id,
        user_id=user.id,
        new_category_name="Transporte",
        create_if_missing=False,
    )

    assert result.status == "updated"
    session.refresh(mov)
    assert mov.categoria_id == cat2.id


def test_update_movement_category_create_if_missing(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat1 = create_category(session, user.id, nombre="Comida")

    mov = MovimientoFinanciero(
        usuario_id=user.id,
        categoria_id=cat1.id,
        tipo="egreso",
        cantidad=1500,
        moneda="ARS",
        descripcion="almuerzo",
    )
    session.add(mov)
    session.commit()
    mov_id = str(mov.id)

    result = FinanceService.update_movement_category(
        movement_id=mov_id,
        user_id=user.id,
        new_category_name="NuevaCat",
        create_if_missing=True,
    )

    assert result.status == "updated"
    session.refresh(mov)
    assert mov.categoria_id is not None
    cat = session.query(Categoria).filter(Categoria.id == mov.categoria_id).first()
    assert cat.nombre == "NuevaCat"


def test_update_movement_category_not_found(db_context):
    session = db_context["session"]
    user = create_user(session)

    result = FinanceService.update_movement_category(
        movement_id=str(uuid.uuid4()),
        user_id=user.id,
        new_category_name="Comida",
    )

    assert result.status == "not_found"


def test_update_movement_category_case_insensitive(db_context):
    session = db_context["session"]
    user = create_user(session)
    cat1 = create_category(session, user.id, nombre="Comida")
    cat2 = create_category(session, user.id, nombre="Transporte")

    mov = MovimientoFinanciero(
        usuario_id=user.id,
        categoria_id=cat1.id,
        tipo="egreso",
        cantidad=1500,
        moneda="ARS",
        descripcion="almuerzo",
    )
    session.add(mov)
    session.commit()
    mov_id = str(mov.id)

    result = FinanceService.update_movement_category(
        movement_id=mov_id,
        user_id=user.id,
        new_category_name="  transporte  ",
        create_if_missing=False,
    )

    assert result.status == "updated"
    session.refresh(mov)
    assert mov.categoria_id == cat2.id
