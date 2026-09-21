import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.budget as budget_module
import app.services.compensation as compensation_module
from app.models.database import (
    Base,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.compensation import BudgetCompensationService


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
    monkeypatch.setattr(compensation_module, "SessionLocal", session_factory)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def create_user(session, phone="5491111111111"):
    user = Usuario(
        nombre="Compensation User",
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
    annulled=False,
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
        anulado_en=datetime.now(timezone.utc) if annulled else None,
    )
    session.add(movement)
    session.commit()
    return movement


def test_proposal_covers_excess_with_exact_donor(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    food_budget = create_budget(session, user, food, amount="1000")
    donor_budget = create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1100)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    proposal = result.proposal
    assert proposal is not None
    assert str(proposal.amount) == "100.00"
    assert proposal.currency == "ARS"
    assert proposal.user_id == str(user.id)
    assert proposal.period_start == date(2026, 9, 1)
    assert proposal.period_end == date(2026, 9, 30)
    assert proposal.target.category_name == "Comida"
    assert proposal.target.limit_id == str(food_budget.id)
    assert proposal.target.before_limit == Decimal("1000.00")
    assert proposal.target.after_limit == Decimal("1100.00")
    assert proposal.target.spent_amount == Decimal("1100.00")
    assert proposal.target.available_before == Decimal("0.00")
    assert len(proposal.donors) == 1
    donor = proposal.donors[0]
    assert donor.category_name == "Transporte"
    assert donor.limit_id == str(donor_budget.id)
    assert donor.before_limit == Decimal("500.00")
    assert donor.after_limit == Decimal("400.00")
    assert donor.spent_amount == Decimal("400.00")
    assert donor.available_before == Decimal("100.00")

    session.expire_all()
    assert (
        session.query(LimiteCategoria)
        .filter(LimiteCategoria.id == food_budget.id)
        .one()
        .cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.query(LimiteCategoria)
        .filter(LimiteCategoria.id == donor_budget.id)
        .one()
        .cantidad_max
        == Decimal("500.00")
    )


def test_proposal_never_exceeds_donor_availability(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1500)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.amount == Decimal("100.00")
    assert result.proposal.target.after_limit == Decimal("1100.00")
    assert result.proposal.donors[0].after_limit == Decimal("400.00")


def test_proposal_without_donors_reports_no_funds(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1500)
    create_movement(session, user, transport, 500)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "no_funds"
    assert result.proposal is None


def test_proposal_ignores_other_currency_and_period(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    leisure = create_category(session, user, "Ocio")
    auto = create_category(session, user, "Auto")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="500")
    create_budget(
        session,
        user,
        leisure,
        amount="1000",
        start=date(2026, 9, 1),
        end=date(2026, 9, 15),
    )
    create_budget(session, user, auto, amount="1000", currency="USD")
    create_movement(session, user, food, 1200)
    create_movement(session, user, transport, 300)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.amount == Decimal("200.00")
    assert [donor.category_name for donor in result.proposal.donors] == [
        "Transporte"
    ]


def test_proposal_excludes_annulled_movements(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_movement(session, user, food, 1200)
    create_movement(session, user, transport, 300, annulled=True)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.amount == Decimal("200.00")
    donor = result.proposal.donors[0]
    assert donor.available_before == Decimal("1000.00")
    assert donor.after_limit == Decimal("800.00")


def test_proposal_uses_multiple_donors_in_order(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    leisure = create_category(session, user, "Ocio")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="500")
    create_budget(session, user, leisure, amount="1000")
    create_movement(session, user, food, 1400)
    create_movement(session, user, transport, 400)
    create_movement(session, user, leisure, 700)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    proposal = result.proposal
    assert proposal is not None
    assert proposal.amount == Decimal("400.00")
    assert [donor.category_name for donor in proposal.donors] == [
        "Ocio",
        "Transporte",
    ]
    assert proposal.donors[0].after_limit == Decimal("700.00")
    assert proposal.donors[1].after_limit == Decimal("400.00")


def test_proposal_ignores_unrelated_excess_when_target_given(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    leisure = create_category(session, user, "Ocio")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, leisure, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_movement(session, user, food, 1200)
    create_movement(session, user, leisure, 1600)
    create_movement(session, user, transport, 900)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.target.category_name == "Comida"
    assert result.proposal.amount == Decimal("100.00")


def test_proposal_without_target_uses_largest_excess(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    leisure = create_category(session, user, "Ocio")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, leisure, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_movement(session, user, food, 1200)
    create_movement(session, user, leisure, 1600)
    create_movement(session, user, transport, 900)

    result = BudgetCompensationService.build_proposal(
        user.id,
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.target.category_name == "Ocio"


def test_proposal_invalid_user_id_returns_invalid_data(db_context):
    result = BudgetCompensationService.build_proposal(
        "not-a-uuid",
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "invalid_data"
    assert result.proposal is None


@pytest.mark.parametrize("requested", ["0", "-10", "0.001"])
def test_proposal_non_positive_requested_amount_returns_invalid_data(
    db_context,
    requested,
):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    create_budget(session, user, food, amount="1000")
    create_movement(session, user, food, 1200)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        requested_amount=Decimal(requested),
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "invalid_data"
    assert result.proposal is None


def test_proposal_oversized_requested_amount_returns_invalid_data(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    create_budget(session, user, food, amount="1000")
    create_movement(session, user, food, 1200)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        requested_amount=Decimal("1e30"),
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "invalid_data"
    assert result.proposal is None


def test_proposal_uses_requested_amount(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_movement(session, user, food, 1500)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        requested_amount=Decimal("120"),
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.amount == Decimal("120.00")
    assert result.proposal.donors[0].after_limit == Decimal("880.00")


def test_proposal_prefers_source_category_for_donors(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    leisure = create_category(session, user, "Ocio")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_budget(session, user, leisure, amount="1000")
    create_movement(session, user, food, 1200)
    create_movement(session, user, transport, 950)
    create_movement(session, user, leisure, 500)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        source_category="Transporte",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    proposal = result.proposal
    assert proposal is not None
    assert proposal.amount == Decimal("200.00")
    assert [donor.category_name for donor in proposal.donors] == [
        "Transporte",
        "Ocio",
    ]
    assert proposal.donors[0].after_limit == Decimal("950.00")
    assert proposal.donors[1].after_limit == Decimal("850.00")


def test_proposal_target_category_not_found_returns_no_excess(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    create_budget(session, user, food, amount="1000")
    create_movement(session, user, food, 1200)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="NoExiste",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "no_excess"
    assert result.proposal is None


def test_proposal_target_without_excess_returns_no_excess(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    create_budget(session, user, food, amount="1000")
    create_movement(session, user, food, 900)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "no_excess"
    assert result.proposal is None


def test_proposal_invalid_currency_returns_invalid_data(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    create_budget(session, user, food, amount="1000")
    create_movement(session, user, food, 1200)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        currency="arsx",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "invalid_data"
    assert result.proposal is None


def test_proposal_normalizes_lowercase_currency(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="1000")
    create_movement(session, user, food, 1200)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        currency="ars",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal.currency == "ARS"


def test_proposal_serialization_roundtrip(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    create_budget(session, user, food, amount="1000")
    create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1100)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    proposal = result.proposal
    assert proposal is not None
    payload = proposal.to_dict()
    assert json.dumps(payload)
    assert payload["amount"] == "100.00"
    assert payload["period_start"] == "2026-09-01"
    assert payload["period_end"] == "2026-09-30"
    assert payload["target"]["before_limit"] == "1000.00"
    assert payload["donors"][0]["after_limit"] == "400.00"

    restored = compensation_module.CompensationProposal.from_dict(payload)

    assert restored == proposal


def test_proposal_snapshot_and_expiration(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    food_budget = create_budget(session, user, food, amount="1000")
    donor_budget = create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1100)
    create_movement(session, user, transport, 400)

    result = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    )

    assert result.status == "ok"
    proposal = result.proposal
    assert proposal is not None
    assert proposal.snapshot == {
        str(food_budget.id): "1000.00",
        str(donor_budget.id): "500.00",
    }
    assert uuid.UUID(proposal.proposal_id)
    created_at = datetime.fromisoformat(proposal.created_at)
    expires_at = datetime.fromisoformat(proposal.expires_at)
    assert created_at.tzinfo is not None
    assert expires_at.tzinfo is not None
    assert expires_at - created_at == timedelta(minutes=30)


def build_standard_proposal(session, user):
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    food_budget = create_budget(session, user, food, amount="1000")
    donor_budget = create_budget(session, user, transport, amount="500")
    create_movement(session, user, food, 1100)
    create_movement(session, user, transport, 400)

    proposal = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    ).proposal
    assert proposal is not None
    return proposal, food_budget, donor_budget


def test_apply_moves_limits_and_preserves_total_and_movements(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    result = BudgetCompensationService.apply(proposal)

    assert result.status == "applied"
    assert result.proposal is proposal
    session.expire_all()
    food_row = session.get(LimiteCategoria, food_budget.id)
    donor_row = session.get(LimiteCategoria, donor_budget.id)
    assert food_row.cantidad_max == Decimal("1100.00")
    assert donor_row.cantidad_max == Decimal("400.00")
    assert food_row.cantidad_max + donor_row.cantidad_max == Decimal("1500.00")
    assert session.query(MovimientoFinanciero).count() == 2
    total = session.query(func.sum(MovimientoFinanciero.cantidad)).scalar()
    assert Decimal(str(total)) == Decimal("1500.00")


def test_apply_rejects_changed_donor_snapshot(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    donor_budget.cantidad_max = Decimal("450")
    session.commit()

    result = BudgetCompensationService.apply(proposal)

    assert result.status == "stale"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("450.00")
    )


def test_apply_rejects_donor_consumed_by_new_movement(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    transport = session.get(Categoria, donor_budget.categoria_id)
    create_movement(session, user, transport, 50)

    result = BudgetCompensationService.apply(proposal)

    assert result.status == "stale"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("500.00")
    )


def test_apply_rejects_expired_proposal(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    now = datetime.fromisoformat(proposal.expires_at) + timedelta(seconds=1)

    result = BudgetCompensationService.apply(proposal, now=now)

    assert result.status == "expired"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("500.00")
    )


def test_apply_is_idempotent_on_same_proposal(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    first = BudgetCompensationService.apply(proposal)
    second = BudgetCompensationService.apply(proposal)

    assert first.status == "applied"
    assert second.status == "stale"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1100.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("400.00")
    )


def test_apply_allows_donor_reaching_zero(db_context):
    session = db_context
    user = create_user(session)
    food = create_category(session, user, "Comida")
    transport = create_category(session, user, "Transporte")
    food_budget = create_budget(session, user, food, amount="1000")
    donor_budget = create_budget(session, user, transport, amount="100")
    create_movement(session, user, food, 1100)

    proposal = BudgetCompensationService.build_proposal(
        user.id,
        target_category="Comida",
        reference_date=REFERENCE_DATE,
    ).proposal
    assert proposal is not None
    assert proposal.donors[0].after_limit == Decimal("0.00")

    result = BudgetCompensationService.apply(proposal)

    assert result.status == "applied"
    session.expire_all()
    food_row = session.get(LimiteCategoria, food_budget.id)
    donor_row = session.get(LimiteCategoria, donor_budget.id)
    assert food_row.cantidad_max == Decimal("1100.00")
    assert donor_row.cantidad_max == Decimal("0")
    assert food_row.cantidad_max + donor_row.cantidad_max == Decimal("1100.00")


def test_apply_rolls_back_on_commit_failure(db_context, monkeypatch):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    class FailingCommitSession:
        def __init__(self, wrapped_session):
            self._wrapped_session = wrapped_session

        def __getattr__(self, name):
            return getattr(self._wrapped_session, name)

        def commit(self):
            raise RuntimeError("commit failed")

    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=session.get_bind(),
    )
    monkeypatch.setattr(
        compensation_module,
        "SessionLocal",
        lambda: FailingCommitSession(session_factory()),
    )

    result = BudgetCompensationService.apply(proposal)

    assert result.status == "persistence_error"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("500.00")
    )


def test_apply_requires_same_user(db_context):
    session = db_context
    user = create_user(session)
    other_user = create_user(session, phone="5492222222222")
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)

    payload = proposal.to_dict()
    payload["user_id"] = str(other_user.id)

    result = BudgetCompensationService.apply(payload)

    assert result.status == "stale"
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("500.00")
    )


def test_apply_invalid_proposal_dict_returns_invalid_data(db_context):
    result = BudgetCompensationService.apply({"amount": "not-a-number"})

    assert result.status == "invalid_data"
    assert result.proposal is None


def assert_limits_unchanged(session, food_budget, donor_budget):
    session.expire_all()
    assert (
        session.get(LimiteCategoria, food_budget.id).cantidad_max
        == Decimal("1000.00")
    )
    assert (
        session.get(LimiteCategoria, donor_budget.id).cantidad_max
        == Decimal("500.00")
    )


def test_apply_rejects_empty_donors(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    payload = proposal.to_dict()
    payload["donors"] = []

    result = BudgetCompensationService.apply(payload)

    assert result.status == "invalid_data"
    assert result.proposal is None
    assert_limits_unchanged(session, food_budget, donor_budget)


def test_apply_rejects_duplicate_donor_limit_id(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    payload = proposal.to_dict()
    payload["donors"] = [payload["donors"][0], payload["donors"][0]]

    result = BudgetCompensationService.apply(payload)

    assert result.status == "invalid_data"
    assert result.proposal is None
    assert_limits_unchanged(session, food_budget, donor_budget)


def test_apply_rejects_negative_amount(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    payload = proposal.to_dict()
    payload["amount"] = "-100.00"
    payload["target"]["after_limit"] = "900.00"
    payload["donors"][0]["after_limit"] = "600.00"

    result = BudgetCompensationService.apply(payload)

    assert result.status == "invalid_data"
    assert result.proposal is None
    assert_limits_unchanged(session, food_budget, donor_budget)


def test_apply_rejects_inconsistent_donor_sum(db_context):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    payload = proposal.to_dict()
    payload["amount"] = "50.00"
    payload["target"]["after_limit"] = "1050.00"

    result = BudgetCompensationService.apply(payload)

    assert result.status == "invalid_data"
    assert result.proposal is None
    assert_limits_unchanged(session, food_budget, donor_budget)


@pytest.mark.parametrize("forged", ["target", "donor"])
def test_apply_rejects_forged_before_limit_against_snapshot(db_context, forged):
    session = db_context
    user = create_user(session)
    proposal, food_budget, donor_budget = build_standard_proposal(session, user)
    payload = proposal.to_dict()

    if forged == "target":
        payload["target"]["before_limit"] = "900.00"
        payload["target"]["after_limit"] = "1000.00"
    else:
        payload["donors"][0]["before_limit"] = "400.00"
        payload["donors"][0]["after_limit"] = "300.00"

    result = BudgetCompensationService.apply(payload)

    assert result.status == "invalid_data"
    assert result.proposal is None
    assert_limits_unchanged(session, food_budget, donor_budget)
