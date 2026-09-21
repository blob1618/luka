import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.budget as budget_module
from app.models.database import (
    Base,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.budget import BudgetService


REFERENCE_DATE = date(2026, 9, 5)


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(budget_module, "SessionLocal", session_factory)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def create_user(session, phone="5491111111111"):
    user = Usuario(
        nombre="Budget User",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=phone,
    )
    session.add(user)
    session.commit()
    return user


def create_category(session, user, name):
    category = Categoria(
        usuario_id=user.id,
        nombre=name,
        es_default=False,
        esta_eliminado=False,
    )
    session.add(category)
    session.commit()
    return category


def create_budget(
    session,
    user,
    category,
    amount="1000",
    currency="ARS",
    start=date(2026, 9, 1),
    end=date(2026, 9, 30),
):
    budget = LimiteCategoria(
        usuario_id=user.id,
        categoria_id=category.id,
        cantidad_max=Decimal(amount),
        moneda=currency,
        inicio_periodo=start,
        fin_periodo=end,
    )
    session.add(budget)
    session.commit()
    return budget


def create_movement(
    session,
    user,
    category,
    amount,
    *,
    movement_type="egreso",
    currency="ARS",
    movement_date=REFERENCE_DATE,
):
    movement = MovimientoFinanciero(
        usuario_id=user.id,
        categoria_id=category.id if category else None,
        tipo=movement_type,
        cantidad=Decimal(str(amount)),
        moneda=currency,
        descripcion="test",
        fecha_movimiento=movement_date,
        origen="test",
    )
    session.add(movement)
    session.commit()
    return movement


def test_status_uses_only_matching_expenses(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food)
    create_movement(session, user, food, 400)
    create_movement(session, user, food, 200, movement_type="ingreso")
    create_movement(session, user, transport, 300)
    create_movement(session, user, food, 500, currency="USD")
    create_movement(session, user, food, 700, movement_date=date(2026, 8, 31))

    result = BudgetService.get_status(
        user.id,
        " comida ",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.budget is not None
    assert result.budget.state == "available"
    assert result.budget.spent_amount == Decimal("400.0000000000")
    assert result.budget.remaining_amount == Decimal("600.0000000000")
    assert result.budget.exceeded_amount == 0
    assert result.budget.percentage == Decimal("40.0")


@pytest.mark.parametrize(
    ("spent", "state", "remaining", "exceeded", "percentage"),
    [
        ("1000", "reached", "0", "0", "100.0"),
        ("1250", "exceeded", "0", "250", "125.0"),
    ],
)
def test_status_reached_and_exceeded(
    db_context,
    spent,
    state,
    remaining,
    exceeded,
    percentage,
):
    session = db_context
    user = create_user(session)
    category = create_category(session, user, "Comida")
    create_budget(session, user, category)
    create_movement(session, user, category, spent)

    result = BudgetService.get_status(
        user.id,
        "Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.budget is not None
    assert result.budget.state == state
    assert result.budget.remaining_amount == Decimal(remaining)
    assert result.budget.exceeded_amount == Decimal(exceeded)
    assert result.budget.percentage == Decimal(percentage)


def test_zero_limit_without_spending_is_available(db_context):
    session = db_context
    user = create_user(session)
    category = create_category(session, user, "Transporte")
    create_budget(session, user, category, amount="0")

    result = BudgetService.get_status(
        user.id,
        "Transporte",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.budget.state == "available"


def test_status_is_isolated_by_user(db_context):
    session = db_context
    first = create_user(session)
    second = create_user(session, "5492222222222")
    first_category = create_category(session, first, "Comida")
    second_category = create_category(session, second, "Comida")
    create_budget(session, first, first_category)
    create_movement(session, first, first_category, 100)
    create_movement(session, second, second_category, 900)

    result = BudgetService.get_status(
        first.id,
        "Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.budget is not None
    assert result.budget.spent_amount == Decimal("100.0000000000")


def test_list_statuses_returns_current_active_categories(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    deleted = create_category(session, user, "Ocio")
    deleted.esta_eliminado = True
    session.commit()
    create_budget(session, user, food)
    create_budget(session, user, transport, amount="2000")
    create_budget(session, user, deleted)
    create_budget(
        session,
        user,
        food,
        currency="USD",
    )

    result = BudgetService.list_statuses(
        user.id,
        reference_date=REFERENCE_DATE,
        currency="ARS",
    )

    assert result.status == "ok"
    assert [budget.category_name for budget in result.budgets] == [
        "Comida",
        "Transporte",
    ]


def test_evaluate_movement_alerts_only_when_exceeded(db_context):
    session = db_context
    user = create_user(session)
    category = create_category(session, user, "Comida")
    create_budget(session, user, category)
    first = create_movement(session, user, category, 900)
    second = create_movement(session, user, category, 200)

    evaluations = BudgetService.evaluate_movements([first.id, second.id])

    assert len(evaluations) == 2
    assert all(item.has_limit for item in evaluations)
    assert all(item.should_alert for item in evaluations)
    assert evaluations[0].budget is not None
    assert evaluations[0].budget.exceeded_amount == Decimal("100.0000000000")


def test_evaluate_non_expense_and_expense_without_limit(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food)
    income = create_movement(
        session,
        user,
        food,
        200,
        movement_type="ingreso",
    )
    expense = create_movement(session, user, transport, 200)

    income_result, expense_result = BudgetService.evaluate_movements(
        [income.id, expense.id]
    )

    assert income_result.status == "not_applicable"
    assert income_result.should_alert is False
    assert expense_result.status == "no_limit"
    assert expense_result.should_alert is False
