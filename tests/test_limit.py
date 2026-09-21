import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
import app.services.limit as limit_module
from app.models.database import Base, Categoria, LimiteCategoria, Usuario
from app.services.limit import LimitService

TODAY = date(2026, 7, 15)


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(limit_module, "SessionLocal", testing_session_local)
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


def create_limit(session, user_id, categoria_id, amount, month, year):
    limite = LimiteCategoria(
        usuario_id=user_id,
        categoria_id=categoria_id,
        cantidad_max=Decimal(str(amount)),
        inicio_periodo=date(year, month, 1),
        fin_periodo=date(year, month, 28 if month == 2 else 30),
    )
    session.add(limite)
    session.commit()
    return limite


def test_zero_limit_amount_is_persisted(db_context):
    session = db_context["session"]
    user = create_user(session)
    category = create_category(session, user.id, "Transporte")
    limit = LimiteCategoria(
        usuario_id=user.id,
        categoria_id=category.id,
        cantidad_max=Decimal("0.00"),
        moneda="ARS",
        inicio_periodo=date(2026, 9, 1),
        fin_periodo=date(2026, 9, 30),
    )
    session.add(limit)
    session.commit()
    assert session.get(LimiteCategoria, limit.id) is not None


def limit_data(**overrides):
    data = {
        "limit_category": "Comida",
        "limit_amount": 300000,
        "limit_month": None,
        "limit_year": None,
    }
    data.update(overrides)
    return data


class TestCreateLimit:
    def test_create_without_month_uses_current_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        result = LimitService.create_limit(
            "5491111111111", limit_data(), today=TODAY
        )

        assert result.status == "created"
        assert result.month == 7
        assert result.year == 2026
        saved = session.query(LimiteCategoria).one()
        assert saved.inicio_periodo == date(2026, 7, 1)
        assert saved.fin_periodo == date(2026, 7, 31)
        assert saved.cantidad_max == Decimal("300000")

    def test_create_with_explicit_future_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_month=11, limit_year=2026),
            today=TODAY,
        )

        assert result.status == "created"
        assert result.month == 11
        assert result.year == 2026

    def test_create_past_month_proposes_next_year(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_month=1),
            today=TODAY,
        )

        assert result.status == "needs_year_confirmation"
        assert result.proposed_month == 1
        assert result.proposed_year == 2027
        assert session.query(LimiteCategoria).count() == 0

    def test_create_expired_explicit_period_is_rejected(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_month=1, limit_year=2025),
            today=TODAY,
        )

        assert result.status == "expired_period"
        assert session.query(LimiteCategoria).count() == 0

    def test_create_missing_amount(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_amount=None),
            today=TODAY,
        )

        assert result.status == "needs_amount"
        assert result.category_name == "Comida"

    def test_create_missing_category(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category=None),
            today=TODAY,
        )

        assert result.status == "needs_category"

    def test_create_user_not_found(self, db_context):
        session = db_context["session"]

        result = LimitService.create_limit(
            "5499999999999", limit_data(), today=TODAY
        )

        assert result.status == "user_not_found"
        assert session.query(LimiteCategoria).count() == 0

    def test_create_missing_category_requires_confirmation(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category="Ropa"),
            today=TODAY,
        )

        assert result.status == "needs_category_confirmation"
        assert result.category_name == "Ropa"
        assert session.query(Categoria).count() == 0

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category="Ropa"),
            today=TODAY,
            allow_category_creation=True,
        )

        assert result.status == "created"
        categoria = session.query(Categoria).filter_by(nombre="Ropa").one()
        assert categoria.usuario_id == user.id

    def test_create_arbitrary_category_after_confirmation(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        pending = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category="Viajes"),
            today=TODAY,
        )

        assert pending.status == "needs_category_confirmation"
        assert pending.category_name == "Viajes"
        assert session.query(Categoria).count() == 0

        created = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category="Viajes"),
            today=TODAY,
            allow_category_creation=True,
        )

        assert created.status == "created"
        categoria = session.query(Categoria).filter_by(nombre="Viajes").one()
        assert categoria.usuario_id == user.id
        assert session.query(LimiteCategoria).filter_by(categoria_id=categoria.id).count() == 1

    def test_create_case_and_whitespace_insensitive_category(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id, nombre="Comida")

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_category="  comida "),
            today=TODAY,
        )

        assert result.status == "created"
        assert session.query(Categoria).count() == 1
        assert result.category_name == "Comida"

    def test_create_upserts_same_category_and_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        first = LimitService.create_limit(
            "5491111111111", limit_data(), today=TODAY
        )
        second = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_amount=500000),
            today=TODAY,
        )

        assert first.status == "created"
        assert second.status == "updated"
        assert second.limit_id == first.limit_id
        assert session.query(LimiteCategoria).count() == 1
        assert session.query(LimiteCategoria).one().cantidad_max == Decimal("500000")

    def test_edit_with_stale_last_limit_is_rejected(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id, nombre="Ropa")

        class LastLimit:
            limit_id = "x"
            sender_phone = "5491111111111"
            category_name = "Ropa"
            amount = Decimal("300000")
            month = 7
            year = 2026

        result = LimitService.create_limit(
            "5491111111111",
            {"limit_month": 8},
            last_limit=LastLimit(),
            today=TODAY,
        )

        assert result.status == "stale_context"
        assert session.query(LimiteCategoria).count() == 0

    def test_create_invalid_amount_becomes_needs_amount(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_amount="no-es-un-numero"),
            today=TODAY,
        )

        assert result.status == "needs_amount"


class TestCreateLimitEditByLastLimit:
    def _make_last_limit(self, limit: LimiteCategoria):
        return type(
            "LastLimit",
            (),
            {
                "limit_id": str(limit.id),
                "sender_phone": "5491111111111",
                "category_name": "Comida",
                "amount": limit.cantidad_max,
                "month": limit.inicio_periodo.month,
                "year": limit.inicio_periodo.year,
            },
        )()

    def test_change_month_edits_existing_record_not_creates_new(self, db_context):
        """Flujo 2: 'cambia el mes por agosto' debe mover el límite existente."""
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)
        LimitService.create_limit(
            "5491111111111",
            limit_data(limit_month=9, limit_year=2026),
            today=TODAY,
        )
        limite = session.query(LimiteCategoria).one()
        last_limit = self._make_last_limit(limite)

        result = LimitService.create_limit(
            "5491111111111",
            {"limit_month": 8},
            last_limit=last_limit,
            today=TODAY,
        )

        assert result.status == "updated"
        assert result.limit_id == str(limite.id)
        assert result.month == 8
        assert session.query(LimiteCategoria).count() == 1
        session.expire_all()
        saved = session.query(LimiteCategoria).one()
        assert saved.inicio_periodo == date(2026, 8, 1)
        assert saved.fin_periodo == date(2026, 8, 31)
        assert saved.cantidad_max == Decimal("300000")

    def test_change_amount_edits_existing_record(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)
        LimitService.create_limit(
            "5491111111111",
            limit_data(),
            today=TODAY,
        )
        limite = session.query(LimiteCategoria).one()
        last_limit = self._make_last_limit(limite)

        result = LimitService.create_limit(
            "5491111111111",
            {"limit_amount": 500000},
            last_limit=last_limit,
            today=TODAY,
        )

        assert result.status == "updated"
        assert result.limit_id == str(limite.id)
        assert session.query(LimiteCategoria).count() == 1
        session.expire_all()
        assert session.query(LimiteCategoria).one().cantidad_max == Decimal("500000")

    def test_edit_year_confirmation_preserves_id(self, db_context):
        """Editar a un mes pasado propone el año siguiente conservando el target."""
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)
        LimitService.create_limit(
            "5491111111111",
            limit_data(limit_month=9, limit_year=2026),
            today=TODAY,
        )
        limite = session.query(LimiteCategoria).one()
        last_limit = self._make_last_limit(limite)

        result = LimitService.create_limit(
            "5491111111111",
            {"limit_month": 1},
            last_limit=last_limit,
            today=TODAY,
        )

        assert result.status == "needs_year_confirmation"
        assert result.proposed_month == 1
        assert result.proposed_year == 2027
        assert session.query(LimiteCategoria).count() == 1

        confirmed = LimitService.create_limit(
            "5491111111111",
            {"limit_month": 1, "limit_year": 2027},
            last_limit=last_limit,
            today=TODAY,
        )
        assert confirmed.status == "updated"
        assert confirmed.limit_id == str(limite.id)
        assert session.query(LimiteCategoria).count() == 1
        session.expire_all()
        saved = session.query(LimiteCategoria).one()
        assert saved.inicio_periodo == date(2027, 1, 1)


class TestListLimits:
    def test_list_filters_expired_limits(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        categoria = create_category(session, user.id, nombre="Comida")
        create_limit(session, user.id, categoria.id, 300000, 7, 2026)  # vigente
        create_limit(session, user.id, categoria.id, 40000, 11, 2023)  # vencido

        result = LimitService.list_limits(user.id, today=TODAY)

        assert result.status == "ok"
        assert len(result.limits) == 1
        assert result.limits[0].category_name == "Comida"
        assert result.limits[0].amount == Decimal("300000")
        assert result.limits[0].month == 7
        assert result.limits[0].year == 2026

    def test_list_empty(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        result = LimitService.list_limits(user.id, today=TODAY)

        assert result.status == "ok"
        assert result.limits == []


class TestDeleteLimit:
    def test_delete_single_without_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        categoria = create_category(session, user.id, nombre="Comida")
        create_limit(session, user.id, categoria.id, 300000, 7, 2026)

        result = LimitService.delete_limit(
            "5491111111111", "comida", today=TODAY
        )

        assert result.status == "deleted"
        assert result.month == 7
        assert result.year == 2026
        assert session.query(LimiteCategoria).count() == 0

    def test_delete_multiple_asks_month_selection(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        categoria = create_category(session, user.id, nombre="Comida")
        create_limit(session, user.id, categoria.id, 300000, 7, 2026)
        create_limit(session, user.id, categoria.id, 400000, 11, 2026)

        result = LimitService.delete_limit(
            "5491111111111", "comida", today=TODAY
        )

        assert result.status == "needs_month_selection"
        assert len(result.candidates) == 2
        months = {c["month"] for c in result.candidates}
        assert months == {7, 11}
        assert session.query(LimiteCategoria).count() == 2


def test_find_existing_limit_and_delete_shown_pair_atomically(db_context):
    session = db_context["session"]
    user = create_user(session)
    food = create_category(session, user.id, "Comida")
    transport = create_category(session, user.id, "Transporte")
    create_limit(session, user.id, food.id, 40000, 9, 2026)
    create_limit(session, user.id, food.id, 40000, 10, 2026)
    create_limit(session, user.id, transport.id, 20000, 10, 2026)

    candidates = LimitService.find_limit_candidates(
        user.whatsapp_id, category="Comida", today=date(2026, 9, 17)
    )
    assert [item["month"] for item in candidates] == [9, 10]
    deleted = LimitService.delete_limits_by_ids(user.whatsapp_id, candidates)
    assert deleted.status == "deleted"
    assert len(deleted.deleted) == 2
    assert session.query(LimiteCategoria).count() == 1


def test_batch_delete_rejects_foreign_or_stale_candidate_without_partial_write(db_context):
    session = db_context["session"]
    user = create_user(session)
    other = create_user(session, "5492222222222")
    cat = create_category(session, user.id, "Comida")
    own = create_limit(session, user.id, cat.id, 40000, 9, 2026)
    candidates = LimitService.find_limit_candidates(
        user.whatsapp_id, category="Comida", today=date(2026, 9, 17)
    )
    assert LimitService.delete_limits_by_ids(other.whatsapp_id, candidates).status == "stale_context"
    assert LimitService.delete_limits_by_ids(
        user.whatsapp_id, [{**candidates[0], "amount": "1"}]
    ).status == "stale_context"
    assert session.query(LimiteCategoria).one().id == own.id

    def test_delete_with_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        categoria = create_category(session, user.id, nombre="Comida")
        create_limit(session, user.id, categoria.id, 300000, 7, 2026)
        create_limit(session, user.id, categoria.id, 400000, 11, 2026)

        result = LimitService.delete_limit(
            "5491111111111", "comida", month=11, today=TODAY
        )

        assert result.status == "deleted"
        assert result.month == 11
        assert session.query(LimiteCategoria).count() == 1

    def test_delete_not_found_category(self, db_context):
        session = db_context["session"]
        create_user(session)

        result = LimitService.delete_limit(
            "5491111111111", "Comida", today=TODAY
        )

        assert result.status == "not_found"

    def test_delete_not_found_expired_only(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        categoria = create_category(session, user.id, nombre="Comida")
        create_limit(session, user.id, categoria.id, 40000, 11, 2023)

        result = LimitService.delete_limit(
            "5491111111111", "comida", today=TODAY
        )

        assert result.status == "not_found"

    def test_delete_user_not_found(self, db_context):
        result = LimitService.delete_limit(
            "5499999999999", "Comida", today=TODAY
        )

        assert result.status == "user_not_found"


class TestLimitValidationAndConcurrencyContract:
    def test_year_without_month_requests_month(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_year=2027),
            today=TODAY,
        )

        assert result.status == "needs_month"

    def test_invalid_currency_is_rejected(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        result = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_currency="pesos"),
            today=TODAY,
        )

        assert result.status == "invalid_currency"

    def test_same_period_supports_separate_currencies(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        create_category(session, user.id)

        ars = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_currency="ARS"),
            today=TODAY,
        )
        usd = LimitService.create_limit(
            "5491111111111",
            limit_data(limit_currency="usd"),
            today=TODAY,
        )

        assert ars.status == "created"
        assert usd.status == "created"
        assert usd.currency == "USD"
        assert session.query(LimiteCategoria).count() == 2

    def test_delete_with_wrong_explicit_year_does_not_fallback(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        category = create_category(session, user.id)
        create_limit(session, user.id, category.id, 300000, 11, 2026)

        result = LimitService.delete_limit(
            "5491111111111",
            "Comida",
            month=11,
            year=2027,
            today=TODAY,
        )

        assert result.status == "not_found"
        assert session.query(LimiteCategoria).count() == 1

    def test_edit_collision_is_reported(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        category = create_category(session, user.id)
        july = create_limit(session, user.id, category.id, 300000, 7, 2026)
        create_limit(session, user.id, category.id, 400000, 8, 2026)
        last_limit = type(
            "LastLimit",
            (),
            {
                "limit_id": str(july.id),
                "sender_phone": "5491111111111",
                "category_name": "Comida",
                "amount": july.cantidad_max,
                "month": 7,
                "year": 2026,
                "currency": "ARS",
            },
        )()

        result = LimitService.create_limit(
            "5491111111111",
            {"limit_month": 8, "limit_year": 2026},
            last_limit=last_limit,
            today=TODAY,
        )

        assert result.status == "conflict"
        assert session.query(LimiteCategoria).count() == 2

    def test_edit_rejects_limit_changed_since_it_was_shown(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        category = create_category(session, user.id)
        existing = create_limit(session, user.id, category.id, 300000, 9, 2026)
        snapshot = type("LastLimit", (), {
            "limit_id": str(existing.id), "category_name": "Comida",
            "amount": Decimal("300000"), "month": 9, "year": 2026,
            "currency": "ARS",
        })()
        existing.cantidad_max = Decimal("350000")
        session.commit()
        result = LimitService.create_limit(
            user.whatsapp_id, {"limit_month": 10, "limit_year": 2026},
            last_limit=snapshot, today=TODAY,
        )
        assert result.status == "stale_context"
        session.refresh(existing)
        assert existing.inicio_periodo.month == 9
        assert existing.cantidad_max == 350000
