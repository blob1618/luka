import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.chart_query as finance_module
from app.models.database import Base, Categoria, MovimientoFinanciero, Usuario
from app.services.dispatcher import _current_month_period
from app.services.finance import (
    CategoryMovementTotal,
)
from app.services.movement_chart import MovementChartService


@pytest.fixture()
def chart_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(finance_module, "SessionLocal", session_factory)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _user(session, phone):
    user = Usuario(
        id=uuid.uuid4(),
        whatsapp_id=phone,
        nombre="Chart User",
        email=f"{uuid.uuid4()}@example.com",
    )
    session.add(user)
    session.commit()
    return user


def _category(session, user, name):
    category = Categoria(
        id=uuid.uuid4(),
        usuario_id=user.id,
        nombre=name,
        esta_eliminado=False,
    )
    session.add(category)
    session.commit()
    return category


def _movement(
    session,
    user,
    *,
    amount,
    category=None,
    currency="ARS",
    movement_type="egreso",
    movement_date=date(2026, 9, 10),
    annulled=False,
):
    movement = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user.id,
        categoria_id=category.id if category else None,
        tipo=movement_type,
        cantidad=Decimal(str(amount)),
        moneda=currency,
        descripcion="test",
        fecha_movimiento=movement_date,
        whatsapp_message_id=f"wamid.{uuid.uuid4()}",
        anulado_en=datetime.now(timezone.utc) if annulled else None,
    )
    session.add(movement)
    session.commit()
    return movement


def test_current_month_period_uses_calendar_boundaries():
    assert _current_month_period(date(2026, 2, 12)) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert _current_month_period(date(2026, 12, 31)) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )


@pytest.mark.parametrize(
    ("ranking", "expected_names", "other_amount"),
    [
        ("highest", ["C7", "C6", "C5", "C4", "C3", "Otros"], Decimal("30")),
        ("lowest", ["C1", "C2", "C3", "C4", "C5", "Otros"], Decimal("130")),
    ],
)
def test_prepare_selects_five_relevant_categories_and_preserves_total(
    ranking,
    expected_names,
    other_amount,
):
    categories = [
        CategoryMovementTotal(f"C{index}", Decimal(index * 10))
        for index in range(1, 8)
    ]

    spec = MovementChartService.prepare(
        categories,
        movement_type="egreso",
        currency="ARS",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        ranking=ranking,
    )

    assert [item.category_name for item in spec.categories] == expected_names
    assert spec.categories[-1].amount == other_amount
    assert spec.total == Decimal("280")
    assert sum((item.amount for item in spec.categories), Decimal("0")) == spec.total


@pytest.mark.parametrize("chart_type", ["bar", "pie"])
def test_render_returns_bounded_png(chart_type):
    spec = MovementChartService.prepare(
        [
            CategoryMovementTotal("Comida", Decimal("1500.50")),
            CategoryMovementTotal("Transporte", Decimal("500")),
        ],
        movement_type="egreso",
        currency="ARS",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        chart_type=chart_type,
    )

    png = MovementChartService.render_png(spec)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) < 5 * 1024 * 1024
