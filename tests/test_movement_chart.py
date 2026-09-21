import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.api.whatsapp import WhatsAppImage
from app.models.database import Base, Categoria, MovimientoFinanciero, Usuario
from app.services.conversation import ConversationService
from app.services.dispatcher import _current_month_period, _handle_movement_chart
from app.services.finance import (
    CategoryMovementTotal,
    FinanceService,
    MovementChartQueryResult,
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


def test_aggregation_isolates_user_period_type_and_annulled_rows(chart_db):
    user = _user(chart_db, "5491100000001")
    other = _user(chart_db, "5491100000002")
    food = _category(chart_db, user, "Comida")
    other_food = _category(chart_db, other, "Comida")
    _movement(chart_db, user, amount="100", category=food)
    _movement(chart_db, user, amount="50", category=food)
    _movement(chart_db, user, amount="900", category=food, annulled=True)
    _movement(chart_db, user, amount="800", category=food, movement_type="ingreso")
    _movement(chart_db, user, amount="700", category=food, movement_date=date(2026, 8, 31))
    _movement(chart_db, other, amount="600", category=other_food)

    before = chart_db.query(MovimientoFinanciero).count()
    result = FinanceService.aggregate_movements_by_category(
        user.id,
        movement_type="egreso",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        session=chart_db,
    )

    assert result.status == "ok"
    assert result.currency == "ARS"
    assert result.categories == [CategoryMovementTotal("Comida", Decimal("150"))]
    assert chart_db.query(MovimientoFinanciero).count() == before


def test_aggregation_requires_currency_when_period_contains_more_than_one(chart_db):
    user = _user(chart_db, "5491100000001")
    food = _category(chart_db, user, "Comida")
    _movement(chart_db, user, amount="100", category=food, currency="ARS")
    _movement(chart_db, user, amount="20", category=food, currency="USD")

    result = FinanceService.aggregate_movements_by_category(
        user.id,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        session=chart_db,
    )

    assert result.status == "needs_currency"
    assert result.available_currencies == ["ARS", "USD"]
    assert result.categories == []

    ars = FinanceService.aggregate_movements_by_category(
        user.id,
        currency="ARS",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        session=chart_db,
    )
    assert ars.status == "ok"
    assert ars.currency == "ARS"
    assert ars.categories == [CategoryMovementTotal("Comida", Decimal("100"))]


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


@pytest.mark.asyncio
async def test_dispatcher_returns_image_for_explicit_chart():
    query_result = MovementChartQueryResult(
        "ok",
        "ok",
        categories=[CategoryMovementTotal("Comida", Decimal("100"))],
        available_currencies=["ARS"],
        currency="ARS",
    )
    with (
        patch("app.services.dispatcher._user_id_by_phone", return_value=uuid.uuid4()),
        patch(
            "app.services.dispatcher.FinanceService.aggregate_movements_by_category",
            return_value=query_result,
        ),
        patch(
            "app.services.dispatcher.MovementChartService.render_png",
            return_value=b"\x89PNG\r\n\x1a\ncontent",
        ),
    ):
        result = await _handle_movement_chart(
            "5491100000001",
            {
                "movement_type": "egreso",
                "chart_type": "bar",
                "chart_ranking": "highest",
                "chart_currency": "ARS",
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
            },
        )

    assert result.intent == "movement_chart"
    assert isinstance(result.reply_message, WhatsAppImage)
    assert "Total" in result.reply_message.caption


@pytest.mark.asyncio
async def test_dispatcher_asks_for_currency_and_stores_pending_request():
    query_result = MovementChartQueryResult(
        "needs_currency",
        "multiple",
        available_currencies=["ARS", "USD"],
    )
    with (
        patch("app.services.dispatcher._user_id_by_phone", return_value=uuid.uuid4()),
        patch(
            "app.services.dispatcher.FinanceService.aggregate_movements_by_category",
            return_value=query_result,
        ),
    ):
        result = await _handle_movement_chart(
            "5491100000001",
            {"date_from": "2026-09-01", "date_to": "2026-09-30"},
        )

    assert result.reply_message is None
    assert "ARS, USD" in result.reply_text
    pending = await ConversationService.get_pending_movement_chart("5491100000001")
    assert pending is not None
    assert pending.available_currencies == ["ARS", "USD"]


@pytest.mark.asyncio
async def test_dispatcher_does_not_render_an_empty_chart():
    query_result = MovementChartQueryResult(
        "ok",
        "ok",
        categories=[],
        available_currencies=[],
        currency="ARS",
    )
    with (
        patch("app.services.dispatcher._user_id_by_phone", return_value=uuid.uuid4()),
        patch(
            "app.services.dispatcher.FinanceService.aggregate_movements_by_category",
            return_value=query_result,
        ),
        patch(
            "app.services.dispatcher.MovementChartService.render_png"
        ) as render,
    ):
        result = await _handle_movement_chart(
            "5491100000001",
            {
                "chart_currency": "ARS",
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
            },
        )

    assert result.reply_message is None
    assert "No encontré gastos" in result.reply_text
    render.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_asks_for_an_incomplete_period_before_querying():
    with patch(
        "app.services.dispatcher.FinanceService.aggregate_movements_by_category"
    ) as query:
        result = await _handle_movement_chart(
            "5491100000001",
            {"date_from": "2026-09-01", "date_to": None},
        )

    assert result.reply_message is None
    assert "período completo" in result.reply_text
    query.assert_not_called()
    pending = await ConversationService.get_pending_movement_chart("5491100000001")
    assert pending is not None
    assert pending.date_from == "2026-09-01"
    assert pending.date_to is None
