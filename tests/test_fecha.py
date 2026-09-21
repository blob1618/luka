from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.models.database import Base, MovimientoFinanciero, Usuario
from app.services.dispatcher import _register_multiop, process_incoming_message
from app.services.finance import MovementRegistrationResult
from app.services.llm import LLMService
from app.services.llm_contract import resolve_relative_date
from app.services.onboarding import OnboardingDecision, OnboardingResult


@pytest.fixture(autouse=True)
def no_pending_limit_flows(monkeypatch):
    for method in (
        "is_awaiting_limit_year_confirmation",
        "is_awaiting_limit_category_confirmation",
        "is_awaiting_limit_data",
        "is_awaiting_limit_delete_category",
        "is_awaiting_limit_month_selection",
        "is_awaiting_compensation_confirmation",
    ):
        monkeypatch.setattr(
            f"app.services.dispatcher.ConversationService.{method}",
            AsyncMock(return_value=False),
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
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)

    session = testing_session_local()
    try:
        yield {"session": session, "session_factory": testing_session_local}
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _create_user(session, whatsapp_id="5491111111111"):
    user = Usuario(
        nombre="Test User",
        email=f"user_{whatsapp_id}@example.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.commit()
    return user


TODAY = date(2026, 8, 21)  # viernes


def test_iso_passthrough():
    assert resolve_relative_date("2026-08-19", TODAY) == date(2026, 8, 19)


def test_yesterday():
    assert resolve_relative_date("ayer", TODAY) == date(2026, 8, 20)


def test_last_weekday_resolves_backward():
    assert resolve_relative_date("martes", TODAY) == date(2026, 8, 18)


def test_invalid_or_missing_returns_today():
    assert resolve_relative_date(None, TODAY) == TODAY
    assert resolve_relative_date("no-fecha", TODAY) == TODAY
    assert resolve_relative_date("2026-13-99", TODAY) == TODAY


def test_future_beyond_tolerance_returns_today():
    assert resolve_relative_date("2027-01-01", TODAY) == TODAY


@pytest.mark.asyncio
async def test_process_message_normalizes_fecha():
    from tests.test_llm import _process_message_with_mock_response

    result = await _process_message_with_mock_response({
        "intent": "expense", "movement_type": "egreso", "amount": 100,
        "fecha": "ayer", "reply_text": "",
    })
    assert result["movements"][0]["fecha"] == str(date.today() - timedelta(days=1))  # noqa: DTZ011


@pytest.mark.asyncio
async def test_context_is_appended_to_system_prompt():
    with patch.object(LLMService, "_get_provider") as gp:
        provider = AsyncMock()
        provider.generate_json.return_value = {"intent": "greeting"}
        gp.return_value = provider
        await LLMService.process_message("hola", context="FECHA ACTUAL: 2026-08-21.")
    sent_prompt = provider.generate_json.await_args.kwargs.get("system_prompt")
    assert "FECHA ACTUAL: 2026-08-21." in sent_prompt


@pytest.mark.asyncio
async def test_gaste_100_ayer_persists_yesterday(db_context):
    session = db_context["session"]
    _create_user(session)

    extracted_data = {
        "intent": "expense",
        "movements": [
            {
                "movement_type": "egreso",
                "amount": 100,
                "currency": "ARS",
                "description": "gasté 100 ayer",
                "fecha": "ayer",
                "reply_text": "",
            }
        ],
    }
    movements = extracted_data["movements"]

    await _register_multiop(
        "5491111111111", "w1", "gasté 100 ayer", extracted_data, movements
    )

    movement = session.query(MovimientoFinanciero).first()
    assert movement is not None
    assert movement.fecha_movimiento == date.today() - timedelta(days=1)  # noqa: DTZ011


@pytest.mark.asyncio
async def test_single_expense_direct_path_passes_ayer():
    llm_result = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 100,
        "currency": "ARS",
        "description": "gasté 100 ayer",
        "fecha": "ayer",
        "category": None,
        "reply_text": "",
    }

    spy = MagicMock()
    spy.return_value = MovementRegistrationResult(
        status="registered", message="ok", movement_id="m1",
        user_id="u1", duplicate=False,
    )

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=llm_result,
        ),
        patch(
            "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
            spy,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_rename",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.services.dispatcher._update_ultimo_mensaje"),
    ):
        await process_incoming_message("5491111111111", "gasté 100 ayer", "w1")

    assert spy.called
    captured = spy.call_args.kwargs
    assert captured["fecha_movimiento"] == date.today() - timedelta(days=1)  # noqa: DTZ011
