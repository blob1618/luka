"""Focal tests for the read-only monthly balance aggregation (STK-241)."""

import uuid
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.monthly_balance as monthly_balance_module
from app.models.database import Base, MovimientoFinanciero, Usuario
from app.services.monthly_balance import (
    ARGENTINA_TZ,
    CurrencyBalance,
    previous_month_period,
    query_monthly_balance,
)


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(monthly_balance_module, "SessionLocal", testing_session_local)

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
        id=uuid.uuid4(),
        whatsapp_id=whatsapp_id,
        nombre="Test User",
        email=f"user_{uuid.uuid4()}@example.com",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def create_movement(
    session,
    user_id,
    fecha_movimiento,
    *,
    tipo="egreso",
    cantidad=Decimal("100.00"),
    moneda="ARS",
    descripcion="movimiento",
    anulado_en=None,
):
    movement = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user_id,
        tipo=tipo,
        cantidad=cantidad,
        moneda=moneda,
        descripcion=descripcion,
        fecha_movimiento=fecha_movimiento,
        origen="whatsapp_text",
        whatsapp_message_id=f"wamid.{uuid.uuid4()}",
        anulado_en=anulado_en,
    )
    session.add(movement)
    session.commit()
    session.refresh(movement)
    return movement


def balance_for(result, currency):
    return next(item for item in result.currencies if item.currency == currency)


OCTOBER_2026 = datetime(2026, 10, 15, 12, 0, tzinfo=ARGENTINA_TZ)
SEPTEMBER_START = date(2026, 9, 1)
OCTOBER_START = date(2026, 10, 1)


class TestPreviousMonthPeriod:
    """Pure period derivation, including year and leap-year boundaries."""

    def test_reference_in_october_returns_september(self):
        assert previous_month_period(date(2026, 10, 3)) == (SEPTEMBER_START, OCTOBER_START)

    def test_january_reference_returns_previous_december(self):
        assert previous_month_period(date(2027, 1, 1)) == (
            date(2026, 12, 1),
            date(2027, 1, 1),
        )

    def test_december_reference_returns_november(self):
        assert previous_month_period(date(2026, 12, 15)) == (
            date(2026, 11, 1),
            date(2026, 12, 1),
        )

    def test_leap_february_period_ends_in_march(self):
        assert previous_month_period(date(2024, 3, 1)) == (
            date(2024, 2, 1),
            date(2024, 3, 1),
        )

    def test_non_leap_february_period_ends_in_march(self):
        assert previous_month_period(date(2026, 3, 1)) == (
            date(2026, 2, 1),
            date(2026, 3, 1),
        )


class TestLocalMonthChange:
    """The period follows the local date in America/Argentina/Buenos_Aires."""

    def test_utc_instant_before_local_midnight_keeps_january_period(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        # 2026-03-01 02:30 UTC is 2026-02-28 23:30 in Argentina.
        now = datetime(2026, 3, 1, 2, 30, tzinfo=timezone.utc)
        create_movement(session, user.id, date(2026, 1, 15), tipo="ingreso", cantidad=Decimal("100.00"))
        create_movement(session, user.id, date(2026, 2, 15), tipo="ingreso", cantidad=Decimal("900.00"))

        result = query_monthly_balance(user.whatsapp_id, now=now)

        assert result.period_start == date(2026, 1, 1)
        assert result.period_end == date(2026, 2, 1)
        assert result.status == "ok"
        assert balance_for(result, "ARS").income == Decimal("100.00")

    def test_utc_instant_at_or_after_local_midnight_uses_february_period(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        # 2026-03-01 03:30 UTC is 2026-03-01 00:30 in Argentina.
        now = datetime(2026, 3, 1, 3, 30, tzinfo=timezone.utc)
        create_movement(session, user.id, date(2026, 1, 31), tipo="ingreso", cantidad=Decimal("100.00"))
        create_movement(session, user.id, date(2026, 2, 15), tipo="ingreso", cantidad=Decimal("900.00"))

        result = query_monthly_balance(user.whatsapp_id, now=now)

        assert result.period_start == date(2026, 2, 1)
        assert result.period_end == date(2026, 3, 1)
        assert balance_for(result, "ARS").income == Decimal("900.00")

    def test_exact_local_midnight_belongs_to_the_new_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        now = datetime(2026, 3, 1, 0, 0, tzinfo=ARGENTINA_TZ)
        create_movement(session, user.id, date(2026, 2, 15), tipo="ingreso", cantidad=Decimal("900.00"))
        create_movement(session, user.id, date(2026, 3, 1), tipo="ingreso", cantidad=Decimal("700.00"))

        result = query_monthly_balance(user.whatsapp_id, now=now)

        assert result.period_start == date(2026, 2, 1)
        assert result.period_end == date(2026, 3, 1)
        assert balance_for(result, "ARS").income == Decimal("900.00")

    def test_naive_datetime_is_read_as_argentina_local(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 2, 20), tipo="ingreso", cantidad=Decimal("50.00"))

        result = query_monthly_balance(user.whatsapp_id, now=datetime(2026, 3, 1, 0, 0))

        assert result.period_start == date(2026, 2, 1)
        assert result.period_end == date(2026, 3, 1)
        assert balance_for(result, "ARS").income == Decimal("50.00")


class TestPeriodBoundaries:
    """Range is [first day of previous month, first day of current month)."""

    def test_start_is_inclusive_and_end_is_exclusive(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 8, 31), tipo="egreso", cantidad=Decimal("999.00"))
        create_movement(session, user.id, SEPTEMBER_START, tipo="ingreso", cantidad=Decimal("100.00"))
        create_movement(session, user.id, date(2026, 9, 30), tipo="egreso", cantidad=Decimal("200.00"))
        create_movement(session, user.id, OCTOBER_START, tipo="ingreso", cantidad=Decimal("1000.00"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.period_start == SEPTEMBER_START
        assert result.period_end == OCTOBER_START
        balance = balance_for(result, "ARS")
        assert balance.income == Decimal("100.00")
        assert balance.expense == Decimal("200.00")
        assert balance.difference == Decimal("-100.00")
        assert balance.movement_count == 2

    def test_leap_day_movement_is_inside_february_period(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2024, 2, 29), tipo="ingreso", cantidad=Decimal("321.00"))

        result = query_monthly_balance(
            user.whatsapp_id, now=datetime(2024, 3, 1, 0, 0, tzinfo=ARGENTINA_TZ)
        )

        assert result.period_start == date(2024, 2, 1)
        assert result.period_end == date(2024, 3, 1)
        assert balance_for(result, "ARS").income == Decimal("321.00")

    def test_january_first_reports_previous_december(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2025, 12, 31), tipo="ingreso", cantidad=Decimal("800.00"))
        create_movement(session, user.id, date(2026, 1, 1), tipo="ingreso", cantidad=Decimal("5000.00"))

        result = query_monthly_balance(
            user.whatsapp_id, now=datetime(2026, 1, 1, 8, 0, tzinfo=ARGENTINA_TZ)
        )

        assert result.period_start == date(2025, 12, 1)
        assert result.period_end == date(2026, 1, 1)
        assert balance_for(result, "ARS").income == Decimal("800.00")


class TestAggregation:
    """Positive, zero and negative differences plus currency separation."""

    def test_positive_difference(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("5000.00"))
        create_movement(session, user.id, date(2026, 9, 20), tipo="egreso", cantidad=Decimal("2000.00"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "ok"
        balance = balance_for(result, "ARS")
        assert balance.difference == Decimal("3000.00")
        assert balance.movement_count == 2

    def test_negative_difference(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"))
        create_movement(session, user.id, date(2026, 9, 20), tipo="egreso", cantidad=Decimal("2500.50"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "ok"
        assert balance_for(result, "ARS").difference == Decimal("-1500.50")

    def test_zero_difference_is_its_own_state_with_data(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"))
        create_movement(session, user.id, date(2026, 9, 20), tipo="egreso", cantidad=Decimal("1000.00"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "zero_difference"
        assert result.status != "no_data"
        balance = balance_for(result, "ARS")
        assert balance.income == Decimal("1000.00")
        assert balance.expense == Decimal("1000.00")
        assert balance.difference == Decimal("0")

    def test_decimal_amounts_are_summed_without_rounding(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 3), tipo="ingreso", cantidad=Decimal("1000.50"))
        create_movement(session, user.id, date(2026, 9, 4), tipo="ingreso", cantidad=Decimal("0.50"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        balance = balance_for(result, "ARS")
        assert isinstance(balance.income, Decimal)
        assert isinstance(balance.difference, Decimal)
        assert balance.income == Decimal("1001.00")
        assert balance.difference == Decimal("1001.00")

    def test_annulled_movements_are_excluded(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"))
        create_movement(
            session,
            user.id,
            date(2026, 9, 6),
            tipo="ingreso",
            cantidad=Decimal("5000.00"),
            anulado_en=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        )

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        balance = balance_for(result, "ARS")
        assert balance.income == Decimal("1000.00")
        assert balance.movement_count == 1

    def test_multiple_currencies_are_kept_separate(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"), moneda="ARS")
        create_movement(session, user.id, date(2026, 9, 6), tipo="egreso", cantidad=Decimal("400.00"), moneda="ARS")
        create_movement(session, user.id, date(2026, 9, 7), tipo="ingreso", cantidad=Decimal("100.00"), moneda="USD")

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "ok"
        assert [item.currency for item in result.currencies] == ["ARS", "USD"]
        ars = balance_for(result, "ARS")
        usd = balance_for(result, "USD")
        assert (ars.income, ars.expense, ars.difference) == (
            Decimal("1000.00"),
            Decimal("400.00"),
            Decimal("600.00"),
        )
        assert (usd.income, usd.expense, usd.difference) == (
            Decimal("100.00"),
            Decimal("0"),
            Decimal("100.00"),
        )
        # No currency conversion: the ARS total never absorbs the USD amount.
        assert usd.income != ars.income

    def test_one_zero_currency_does_not_hide_a_non_zero_one(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("500.00"), moneda="ARS")
        create_movement(session, user.id, date(2026, 9, 6), tipo="egreso", cantidad=Decimal("500.00"), moneda="ARS")
        create_movement(session, user.id, date(2026, 9, 7), tipo="ingreso", cantidad=Decimal("100.00"), moneda="USD")

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "ok"
        assert balance_for(result, "ARS").difference == Decimal("0")
        assert balance_for(result, "USD").difference == Decimal("100.00")

    def test_period_without_records_returns_no_data(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 8, 31), tipo="ingreso", cantidad=Decimal("100.00"))
        create_movement(session, user.id, OCTOBER_START, tipo="ingreso", cantidad=Decimal("200.00"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "no_data"
        assert result.currencies == []
        assert result.period_start == SEPTEMBER_START
        assert result.period_end == OCTOBER_START

    def test_user_without_any_movement_returns_no_data(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        assert result.status == "no_data"
        assert result.currencies == []


class TestUserIsolation:
    """Only the movements of the resolved account are aggregated."""

    def test_two_users_do_not_share_totals(self, db_context):
        session = db_context["session"]
        user_a = create_user(session, whatsapp_id="5491100000001")
        user_b = create_user(session, whatsapp_id="5491100000002")
        create_movement(session, user_a.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"))
        create_movement(session, user_b.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("7777.00"))

        result_a = query_monthly_balance(user_a.whatsapp_id, now=OCTOBER_2026)
        result_b = query_monthly_balance(user_b.whatsapp_id, now=OCTOBER_2026)

        assert balance_for(result_a, "ARS").income == Decimal("1000.00")
        assert balance_for(result_b, "ARS").income == Decimal("7777.00")

    def test_unknown_phone_returns_user_not_found(self, db_context):
        session = db_context["session"]
        create_user(session, whatsapp_id="5491100000001")

        result = query_monthly_balance("5499999999999", now=OCTOBER_2026)

        assert result.status == "user_not_found"
        assert result.currencies == []

    def test_blank_phone_returns_user_not_found(self, db_context):
        result = query_monthly_balance("   ", now=OCTOBER_2026)

        assert result.status == "user_not_found"
        assert result.currencies == []
        assert result.period_start == SEPTEMBER_START


class TestReadOnly:
    """The query never mutates persisted rows."""

    def test_query_does_not_modify_movements(self, db_context):
        session = db_context["session"]
        session_factory = db_context["session_factory"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("1000.00"))
        create_movement(session, user.id, date(2026, 9, 20), tipo="egreso", cantidad=Decimal("1000.00"))
        create_movement(
            session,
            user.id,
            date(2026, 9, 21),
            tipo="ingreso",
            cantidad=Decimal("300.00"),
            anulado_en=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        )
        create_movement(session, user.id, date(2026, 8, 1), tipo="ingreso", cantidad=Decimal("50.00"))

        def snapshot():
            with session_factory() as fresh_session:
                return fresh_session.query(
                    MovimientoFinanciero.id,
                    MovimientoFinanciero.usuario_id,
                    MovimientoFinanciero.tipo,
                    MovimientoFinanciero.cantidad,
                    MovimientoFinanciero.moneda,
                    MovimientoFinanciero.descripcion,
                    MovimientoFinanciero.fecha_movimiento,
                    MovimientoFinanciero.origen,
                    MovimientoFinanciero.whatsapp_message_id,
                    MovimientoFinanciero.creado_en,
                    MovimientoFinanciero.actualizado_en,
                    MovimientoFinanciero.anulado_en,
                ).order_by(MovimientoFinanciero.id).all()

        before = snapshot()

        query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)
        query_monthly_balance(user.whatsapp_id, now=datetime(2026, 2, 1, tzinfo=ARGENTINA_TZ))
        query_monthly_balance("5499999999999", now=OCTOBER_2026)

        after = snapshot()
        assert after == before
        assert len(after) == 4


class TestQueryErrors:
    """Database failures are reported, never raised or guessed."""

    def test_database_error_returns_error_status(self):
        broken_session = MagicMock()
        broken_session.query.side_effect = RuntimeError("database disk failure")

        result = query_monthly_balance("5491100000001", now=OCTOBER_2026, session=broken_session)

        assert result.status == "error"
        assert result.currencies == []


class TestResultShape:
    """The typed contract covers the plan's stated fields."""

    def test_currency_balance_is_frozen_and_typed(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_movement(session, user.id, date(2026, 9, 5), tipo="ingreso", cantidad=Decimal("10.00"))

        result = query_monthly_balance(user.whatsapp_id, now=OCTOBER_2026)

        balance = balance_for(result, "ARS")
        assert isinstance(balance, CurrencyBalance)
        with pytest.raises(FrozenInstanceError):
            balance.income = Decimal("0")
