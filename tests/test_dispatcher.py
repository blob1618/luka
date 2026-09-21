"""Tests for the extracted message dispatcher."""

import contextlib
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, LimiteCategoria, MovimientoFinanciero
from app.services.dispatcher import process_incoming_message
from app.services.dashboard_link import DashboardLinkDecision, DashboardLinkResult
from app.services.finance import MovementRegistrationResult
from app.services.onboarding import OnboardingDecision, OnboardingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

def known_user():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def send_invitation():
    return OnboardingResult(
        OnboardingDecision.SEND_INVITATION,
        registration_url="https://example.com/registro",
        invitation_ttl_minutes=30,
    )


def suppress_response():
    return OnboardingResult(OnboardingDecision.SUPPRESS_RESPONSE)


def onboarding_error():
    return OnboardingResult(OnboardingDecision.ERROR)


def movement_llm_result(**overrides):
    result = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 5000,
        "currency": "ARS",
        "description": "supermercado",
        "expense": "supermercado",
        "category": "supermercado",
        "reply_text": "LLM dice registrado",
        "reminder_concept": None,
        "reminder_day": None,
        "reminder_amount": None,
        "reminder_currency": None,
        "reminder_id": None,
        "reminder_title": None,
        "reminder_date": None,
    }
    result.update(overrides)
    return result


def greeting_llm_result():
    return {
        "intent": "greeting",
        "reply_text": "¡Hola! Soy Luka.",
        "expense": None,
        "amount": None,
        "currency": "ARS",
        "movement_type": None,
        "category": None,
        "description": None,
        "reminder_concept": None,
        "reminder_day": None,
        "reminder_amount": None,
        "reminder_currency": None,
        "reminder_id": None,
        "reminder_title": None,
        "reminder_date": None,
    }


def registered_result():
    return MovementRegistrationResult(
        status="registered",
        message="registered",
        movement_id="mov-1",
        user_id="user-1",
        duplicate=False,
    )


def duplicate_result():
    return MovementRegistrationResult(
        status="duplicate",
        message="duplicate",
        movement_id="mov-1",
        user_id="user-1",
        duplicate=True,
    )


@contextmanager
def common_patches(**overrides):
    """Default set of patches required by most dispatch paths."""
    defaults = dict(
        onboarding=known_user(),
        llm=movement_llm_result(),
        register=registered_result(),
        awaiting_rename=False,
        awaiting_reminder=False,
    )
    defaults.update(overrides)

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=defaults["onboarding"],
        ) as onboarding_mock,
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=defaults["llm"],
        ) as llm_mock,
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=defaults["register"],
        ) as register_mock,
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_rename",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_rename"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_reminder"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_last_movement",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.dispatcher._update_ultimo_mensaje",
        ),
    ):
        yield {
            "onboarding": onboarding_mock,
            "llm": llm_mock,
            "register": register_mock,
        }


# ---------------------------------------------------------------------------
# Onboarding gate
# ---------------------------------------------------------------------------

class TestOnboardingGate:
    """Dispatcher must check onboarding before any processing."""

    @pytest.mark.asyncio
    async def test_unknown_user_gets_invitation(self):
        with patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=send_invitation(),
        ):
            result = await process_incoming_message("5491199990000", "hola")

        assert "registrate" in result.reply_text.lower() or "registro" in result.reply_text.lower()
        assert result.service_invoked == "onboarding"

    @pytest.mark.asyncio
    async def test_suppress_response_returns_empty(self):
        with patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=suppress_response(),
        ):
            result = await process_incoming_message("5491199990000", "hola")

        assert result.reply_text == ""
        assert result.service_invoked == "onboarding"

    @pytest.mark.asyncio
    async def test_onboarding_error(self):
        with patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=onboarding_error(),
        ):
            result = await process_incoming_message("5491199990000", "hola")

        assert "verificar" in result.reply_text.lower() or "intentá" in result.reply_text.lower()
        assert result.service_invoked == "onboarding"


# ---------------------------------------------------------------------------
# /link command
# ---------------------------------------------------------------------------

class TestLinkCommand:
    """The /link command bypasses LLM entirely."""

    def make_link_result(self):
        return DashboardLinkResult(
            DashboardLinkDecision.SEND_LINK,
            login_url="https://example.com/login?token=abc",
            link_ttl_minutes=15,
        )

    @pytest.mark.asyncio
    async def test_link_command_sends_dashboard_link(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_link_result(),
            ),
        ):
            result = await process_incoming_message("12345", "/link")

        assert "dashboard" in result.reply_text.lower() or "login" in result.reply_text.lower()
        assert result.service_invoked == "dashboard_link"

    @pytest.mark.asyncio
    async def test_link_command_case_insensitive_with_spaces(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_link_result(),
            ),
        ):
            result = await process_incoming_message("12345", "  /link  ")

        assert result.service_invoked == "dashboard_link"

    @pytest.mark.asyncio
    async def test_link_does_not_block_a_following_expense(self):
        abandon = AsyncMock()
        render_event = AsyncMock(return_value=None)
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_link_result(),
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                abandon,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.render_event",
                render_event,
            ),
        ):
            link_result = await process_incoming_message("12345", "/link")

        with (
            common_patches(),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                abandon,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.render_event",
                render_event,
            ),
        ):
            expense_result = await process_incoming_message(
                "12345",
                "hoy gasté 3000 pesos en agua",
                "wamid.after-link",
            )

        assert link_result.service_invoked == "dashboard_link"
        assert expense_result.service_invoked == "finance"
        assert expense_result.intent == "expense"
        assert abandon.await_count == 2


# ---------------------------------------------------------------------------
# Financial movements
# ---------------------------------------------------------------------------

class TestFinancialMovement:
    """Dispatcher routes expense intents to FinanceService."""

    @pytest.mark.asyncio
    async def test_expense_registered_successfully(self):
        with common_patches():
            result = await process_incoming_message("12345", "Gasté 5000 en supermercado", "wamid.1")

        assert "✅" in result.reply_text
        assert "5000" in result.reply_text
        assert result.intent == "expense"
        assert result.service_invoked == "finance"
        assert result.raw_llm_response is not None
        assert result.raw_llm_response["intent"] == "expense"

    @pytest.mark.asyncio
    async def test_duplicate_movement_suppresses_reply(self):
        with common_patches(register=duplicate_result()):
            result = await process_incoming_message("12345", "Gasté 5000 en supermercado", "wamid.1")

        assert result.reply_text == ""

    @pytest.mark.asyncio
    async def test_category_confirmation_persists_pending_movement(self):
        needs_confirmation = MovementRegistrationResult(
            status="needs_category_confirmation",
            message="category confirmation required",
            category_name="Jardinería de prueba",
        )
        set_pending = AsyncMock()

        with (
            common_patches(
                llm=movement_llm_result(category="Jardinería de prueba"),
                register=needs_confirmation,
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_movement",
                set_pending,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.render_event",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "Gasté 5000 en semillas, ponelo en Jardinería de prueba",
                "wamid.category-confirmation",
            )

        assert isinstance(result.reply_text, str)
        assert result.event_key == "category.confirmation_required"
        assert result.event_variables == {"category": "Jardinería de prueba"}
        set_pending.assert_awaited_once()
        pending = set_pending.await_args.args[1]
        assert pending.sender_phone == "12345"
        assert pending.whatsapp_message_id == "wamid.category-confirmation"
        assert pending.inferred_category == "Jardinería de prueba"

    @pytest.mark.asyncio
    async def test_confirm_category_registers_user_approved_category(self):
        from decimal import Decimal

        from app.services.conversation import PendingMovement
        from app.services.dispatcher import _confirm_pending_category_action

        pending = PendingMovement(
            sender_phone="12345",
            whatsapp_message_id="wamid.category-confirmation",
            original_text="Gasté 2500 en semillas",
            movement_type="egreso",
            amount=Decimal("2500"),
            currency="ARS",
            description="semillas",
            inferred_category="Jardinería de prueba",
        )
        register = MagicMock(return_value=registered_result())

        with (
            patch(
                "app.services.dispatcher.ConversationService.get_pending_movement",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as clear_state,
            patch(
                "app.services.dispatcher.FinanceService.register_movement_with_category",
                register,
            ),
        ):
            result = await _confirm_pending_category_action("12345")

        assert result.service_invoked == "finance"
        assert result.event_key == "movement.registered"
        assert "Registré tu egreso" in result.reply_text
        assert register.call_args.kwargs["category_creation_confirmed"] is True
        clear_state.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_multiop_registers_two_movements(self):
        from unittest.mock import MagicMock, patch

        multiop_llm = movement_llm_result(
            movements=[
                {"movement_type": "ingreso", "amount": 50000.0, "currency": "ARS",
                 "description": "sueldo", "reply_text": ""},
                {"movement_type": "egreso", "amount": 10000.0, "currency": "ARS",
                 "description": "comida", "reply_text": ""},
            ]
        )
        register_mock = MagicMock(return_value=registered_result())
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=multiop_llm,
            ),
            patch(
                "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
                register_mock,
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
            patch(
                "app.services.dispatcher.ConversationService.set_last_movement",
                new_callable=AsyncMock,
            ),
            patch("app.services.dispatcher._update_ultimo_mensaje"),
        ):
            result = await process_incoming_message("12345", "sueldo 50k y comida 10k", "wamid.1")

        assert register_mock.call_count == 2
        assert result.service_invoked == "finance"


# ---------------------------------------------------------------------------
# Greeting / out_of_scope
# ---------------------------------------------------------------------------

class TestNonFinancialIntents:
    """Dispatcher returns LLM reply_text for non-financial intents."""

    @pytest.mark.asyncio
    async def test_greeting_returns_llm_reply(self):
        with common_patches(llm=greeting_llm_result()):
            result = await process_incoming_message("12345", "hola")

        assert result.reply_text == "¡Hola! Soy Luka."
        assert result.intent == "greeting"

    @pytest.mark.asyncio
    async def test_financial_education_example_never_invokes_llm_or_finance_write(self):
        with (
            common_patches() as mocks,
            patch(
                "app.services.dispatcher.FinanceService.update_movement",
                side_effect=AssertionError("No debe mutar movimientos"),
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                side_effect=AssertionError("No debe mutar límites"),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                side_effect=AssertionError("No debe compensar presupuestos"),
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "Si ahorro 1000 por mes, ¿cómo funciona el interés compuesto?",
                "wamid.education-example",
            )

        assert result.intent == "financial_education"
        assert result.service_invoked == "financial_education"
        assert "Ejemplo ilustrativo" in result.reply_text
        mocks["llm"].assert_not_awaited()
        mocks["register"].assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_education_intent_still_cannot_write_a_movement(self):
        llm_result = greeting_llm_result()
        llm_result.update(
            intent="financial_education",
            education_term="ahorro",
            reply_text="",
        )
        with (
            common_patches(llm=llm_result) as mocks,
            patch(
                "app.services.dispatcher.FinanceService.update_movement",
                side_effect=AssertionError("No debe mutar movimientos"),
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                side_effect=AssertionError("No debe mutar límites"),
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "Necesito educación financiera sobre ahorro para un taller",
                "wamid.education-llm",
            )

        assert result.intent == "financial_education"
        assert result.service_invoked == "financial_education"
        assert result.raw_llm_response["education_status"] == "supported"
        mocks["register"].assert_not_called()
        assert result.raw_llm_response is not None

    @pytest.mark.asyncio
    async def test_recent_history_reaches_llm_service(self):
        history = [
            {"role": "user", "content": "mostrame mis movimientos"},
            {"role": "assistant", "content": "¿Gastos, ingresos o todos?"},
        ]
        llm_mock = AsyncMock(return_value=greeting_llm_result())
        with (
            common_patches(llm=greeting_llm_result()),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                llm_mock,
            ),
        ):
            await process_incoming_message(
                "12345",
                "todos",
                conversation_history=history,
            )

        assert llm_mock.await_args.kwargs["history"] == history


# ---------------------------------------------------------------------------
# Reset de contexto (memoria conversacional)
# ---------------------------------------------------------------------------

class TestResetContext:
    @pytest.mark.asyncio
    async def test_reset_context_clears_state_and_memory(self):
        llm_result = {
            "intent": "reset_context",
            "reply_text": "Listo, arrancamos de cero.",
        }
        clear_state = AsyncMock()
        clear_pending_selection = AsyncMock()
        clear_pending_flow = AsyncMock()
        clear_last_limit = AsyncMock()
        clear_last_movement = AsyncMock()
        clear_recent_items = AsyncMock()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
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
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                clear_pending_selection,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_last_limit",
                clear_last_limit,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_last_movement",
                clear_last_movement,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_recent_items",
                clear_recent_items,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                clear_pending_flow,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message(
                "12345", "olvidá todo y arranquemos de cero"
            )

        assert result.intent == "reset_context"
        assert result.service_invoked == "conversation"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        clear_state.assert_awaited_once_with("12345")
        clear_pending_selection.assert_awaited_once_with("12345")
        clear_last_limit.assert_awaited_once_with("12345")
        clear_last_movement.assert_awaited_once_with("12345")
        clear_recent_items.assert_awaited_once_with("12345")
        clear_pending_flow.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_reset_context_interrupts_multiturn_flow(self):
        from decimal import Decimal

        from app.services.conversation import PendingReminder

        clear_state = AsyncMock()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_reminder",
                new_callable=AsyncMock,
                return_value=PendingReminder(
                    sender_phone="12345",
                    reminder_concept="cable",
                    reminder_day=None,
                    reminder_amount=Decimal("2500"),
                    reminder_currency="ARS",
                ),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.intent == "reset_context"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        clear_state.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_reset_context_wins_over_cancel_in_delete_category_flow(self):
        from app.services.conversation import PendingLimitDelete

        clear_state = AsyncMock()
        delete_limit = AsyncMock()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_limit_delete_category",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=PendingLimitDelete(
                    sender_phone="12345",
                    category_name=None,
                ),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limit",
                delete_limit,
            ),
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.intent == "reset_context"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        assert "cancel" not in result.reply_text.lower()
        clear_state.assert_awaited_once_with("12345")
        delete_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_context_wins_over_cancel_in_limit_month_flow(self):
        from app.services.conversation import PendingLimitDelete

        clear_state = AsyncMock()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_limit_month_selection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=PendingLimitDelete(
                    sender_phone="12345",
                    category_name="Comida",
                    candidates=[
                        {
                            "limit_id": "lim-1",
                            "category": "Comida",
                            "amount": "500",
                            "month": 9,
                            "year": 2026,
                            "currency": "ARS",
                        }
                    ],
                ),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.intent == "reset_context"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        assert "cancel" not in result.reply_text.lower()
        clear_state.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_reset_context_in_rename_flow(self):
        from app.services.conversation import PendingReminder

        create_reminder = MagicMock()
        clear_state = AsyncMock()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_rename",
                new_callable=AsyncMock,
                return_value=PendingReminder(
                    sender_phone="12345",
                    reminder_concept=None,
                    reminder_day=15,
                    reminder_amount=Decimal("1000"),
                    reminder_currency="ARS",
                ),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
            patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                create_reminder,
            ),
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.intent == "reset_context"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        create_reminder.assert_not_called()
        clear_state.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_reset_context_tolerates_redis_unavailable(self):
        from app.services.conversation import ConversationStateUnavailable

        clear_state = AsyncMock()
        clear_pending_flow = AsyncMock(
            side_effect=ConversationStateUnavailable("redis down")
        )

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
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
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                clear_state,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                clear_pending_flow,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."
        clear_state.assert_awaited_once_with("12345")
        clear_pending_flow.assert_awaited_once_with("12345")


# ---------------------------------------------------------------------------
# La memoria es contexto, no autorización
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    for target in (
        "app.services.dispatcher.SessionLocal",
        "app.services.finance.SessionLocal",
        "app.services.limit.SessionLocal",
        "app.models.database.SessionLocal",
    ):
        monkeypatch.setattr(target, factory)
    yield factory
    engine.dispose()


class TestMemoryIsContextNotAuthorization:
    @pytest.mark.asyncio
    async def test_history_proposal_does_not_authorize_writes(self, isolated_db):
        history = [
            {
                "role": "assistant",
                "content": (
                    "Puedo compensar $100 moviendo saldo de Comida a Transporte, "
                    "¿confirmás?"
                ),
            }
        ]
        llm_mock = AsyncMock(return_value={"intent": "confirm_limit"})
        set_pending_limit = AsyncMock()

        limit_id = uuid.uuid4()
        with isolated_db() as session:
            session.add(
                LimiteCategoria(
                    id=limit_id,
                    usuario_id=uuid.uuid4(),
                    categoria_id=uuid.uuid4(),
                    cantidad_max=Decimal("500"),
                    moneda="ARS",
                    inicio_periodo=date(2026, 9, 1),
                    fin_periodo=date(2026, 9, 30),
                )
            )
            session.commit()
            seeded = session.get(LimiteCategoria, limit_id)
            seeded_amount = seeded.cantidad_max
            seeded_updated = seeded.actualizado_en

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                llm_mock,
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
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                set_pending_limit,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "Confirmo",
                "wamid.confirm",
                conversation_history=history,
            )

        assert llm_mock.await_args.kwargs["history"] == history
        assert result.intent == "confirm_limit"
        assert result.clear_memory is False
        assert "compens" not in result.reply_text.lower()
        assert "confirm" not in result.reply_text.lower()
        set_pending_limit.assert_not_awaited()

        with isolated_db() as session:
            assert session.query(LimiteCategoria).count() == 1
            stored = session.get(LimiteCategoria, limit_id)
            assert stored.cantidad_max == seeded_amount
            assert stored.actualizado_en == seeded_updated
            assert session.query(MovimientoFinanciero).count() == 0

    @pytest.mark.asyncio
    async def test_reset_survives_movement_normalizers(self, isolated_db):
        movement_id = uuid.uuid4()
        with isolated_db() as session:
            session.add(
                MovimientoFinanciero(
                    id=movement_id,
                    usuario_id=uuid.uuid4(),
                    tipo="egreso",
                    cantidad=Decimal("500"),
                    moneda="ARS",
                    descripcion="super",
                    fecha_movimiento=date(2026, 9, 1),
                )
            )
            session.commit()

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"intent": "reset_context"},
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
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_selection",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_pending_conversation_flow",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationFlowRuntime.abandon",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "borrá el historial de gastos",
                "wamid.delete-history",
            )

        assert result.intent == "reset_context"
        assert result.clear_memory is True
        assert result.reply_text == "Listo, arrancamos de cero. Olvidé lo anterior."

        with isolated_db() as session:
            stored = session.get(MovimientoFinanciero, movement_id)
            assert stored is not None
            assert stored.anulado_en is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases the dispatcher must handle gracefully."""

    @pytest.mark.asyncio
    async def test_empty_message(self):
        with common_patches(llm=greeting_llm_result()):
            result = await process_incoming_message("12345", "")

        assert result.reply_text  # must always return something

    @pytest.mark.asyncio
    async def test_whitespace_only_message(self):
        with common_patches(llm=greeting_llm_result()):
            result = await process_incoming_message("12345", "   ")

        assert result.reply_text


# ---------------------------------------------------------------------------
# Reminder intents
# ---------------------------------------------------------------------------

class TestReminderIntents:
    """Dispatcher routes reminder intents to ReminderService."""

    @pytest.mark.asyncio
    async def test_pause_reminder_routes_to_reminder_service(self):
        from app.services.reminder import ReminderResult

        llm_result = {
            "intent": "pause_reminder",
            "reminder_id": "123e4567-e89b-12d3-a456-426614174000",
            "reply_text": "Estoy pausando el recordatorio.",
        }

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
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
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
            patch(
                "app.services.dispatcher.ReminderService.pause_reminder",
                return_value=ReminderResult(status="paused", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "Pausá el recordatorio de luz")

        assert result.service_invoked == "reminder"
        assert "pausé" in result.reply_text.lower()


# ---------------------------------------------------------------------------
# More reminder intents
# ---------------------------------------------------------------------------

def reminder_llm_result(intent, **overrides):
    result = {
        "intent": intent,
        "reminder_id": None,
        "reminder_concept": "luz",
        "reminder_day": 15,
        "reminder_amount": None,
        "reminder_currency": "ARS",
        "movement_type": None,
        "amount": None,
        "currency": "ARS",
        "category": None,
        "description": None,
        "expense": None,
        "reply_text": "procesando",
        "reminder_title": None,
        "reminder_date": None,
    }
    result.update(overrides)
    return result


@contextmanager
def reminder_patches(intent, service_patch=None, **llm_overrides):
    cm_list = [
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=reminder_llm_result(intent, **llm_overrides),
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
        patch(
            "app.services.dispatcher._update_ultimo_mensaje",
        ),
    ]
    if service_patch is not None:
        cm_list.append(service_patch)

    with contextlib.ExitStack() as stack:
        for cm in cm_list:
            stack.enter_context(cm)
        yield


class TestMoreReminderIntents:
    @pytest.mark.asyncio
    async def test_activate_reminder_routes_by_title(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "activate_reminder",
            reminder_concept="luz",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.activate_by_title",
                return_value=ReminderResult(status="activated", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "activá luz")

        assert result.service_invoked == "reminder"
        assert "reactiv" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_activate_reminder_by_id(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "activate_reminder",
            reminder_concept=None,
            reminder_id="abc-123",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.activate_reminder",
                return_value=ReminderResult(status="activated", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "activá")

        assert result.service_invoked == "reminder"

    @pytest.mark.asyncio
    async def test_delete_reminder_routes_by_title(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "delete_reminder",
            reminder_concept="luz",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.delete_by_title",
                return_value=ReminderResult(status="deleted", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "borrá luz")

        assert result.service_invoked == "reminder"
        assert "elimin" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_delete_reminder_by_id(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "delete_reminder",
            reminder_concept=None,
            reminder_id="abc-123",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.delete_reminder",
                return_value=ReminderResult(status="deleted", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "borrá")

        assert result.service_invoked == "reminder"

    @pytest.mark.asyncio
    async def test_update_reminder(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "update_reminder",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.update_reminder",
                return_value=ReminderResult(status="updated", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "cambia luz al 20")

        assert result.service_invoked == "reminder"
        assert "actualic" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_update_reminder_finds_by_title(self):
        from app.services.reminder import ReminderResult

        class FakeReminder:
            def __init__(self):
                self.id = "id-456"

        with reminder_patches(
            "update_reminder",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.update_reminder",
                return_value=ReminderResult(status="updated", message="ok"),
            ),
        ):
            with patch(
                "app.services.dispatcher.ReminderService.find_by_title",
                return_value=[FakeReminder()],
            ):
                result = await process_incoming_message("12345", "cambia luz")

        assert result.service_invoked == "reminder"

    @pytest.mark.asyncio
    async def test_list_reminders(self):

        with reminder_patches(
            "list_reminders",
            service_patch=patch(
                "app.services.dispatcher._handle_list_reminders",
                new_callable=AsyncMock,
                return_value="📌 *Tus recordatorios:*\n• Luz",
            ),
        ):
            result = await process_incoming_message("12345", "mis recordatorios")

        assert result.service_invoked == "reminder"
        assert "Luz" in result.reply_text

    @pytest.mark.asyncio
    async def test_disable_proactive_reminders(self):
        from app.services.reminder import ReminderResult

        mock_service = MagicMock(
            return_value=ReminderResult(
                status="updated",
                message="Listo, no te voy a escribir más para recordarte gastos no registrados.",
            )
        )
        with reminder_patches(
            "disable_proactive_reminders",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.set_proactive_prompts",
                mock_service,
            ),
        ):
            result = await process_incoming_message("12345", "no me escribas más")

        mock_service.assert_called_once_with("12345", enabled=False)
        assert result.service_invoked == "reminder"
        assert "no te voy a escribir" in result.reply_text

    @pytest.mark.asyncio
    async def test_enable_proactive_reminders(self):
        from app.services.reminder import ReminderResult

        mock_service = MagicMock(
            return_value=ReminderResult(
                status="updated",
                message="Listo, activé los recordatorios proactivos.",
            )
        )
        with reminder_patches(
            "enable_proactive_reminders",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.set_proactive_prompts",
                mock_service,
            ),
        ):
            result = await process_incoming_message("12345", "volvé a escribirme")

        mock_service.assert_called_once_with("12345", enabled=True)
        assert result.service_invoked == "reminder"
        assert "activé" in result.reply_text


# ---------------------------------------------------------------------------
# Category management intents
# ---------------------------------------------------------------------------

class TestCategoryIntents:
    @pytest.mark.asyncio
    async def test_delete_category(self):

        llm_result = {"intent": "delete_category", "category": "Comida", "reply_text": "x"}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
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
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
            patch(
                "app.services.dispatcher._handle_delete_category",
                new_callable=AsyncMock,
                return_value="✅ Categoría 'Comida' eliminada.",
            ),
        ):
            result = await process_incoming_message("12345", "borrá categoría comida")

        assert result.service_invoked == "finance"

    @pytest.mark.asyncio
    async def test_list_categories(self):
        llm_result = {"intent": "list_categories", "reply_text": "x"}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
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
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
            patch(
                "app.services.dispatcher._handle_list_categories",
                new_callable=AsyncMock,
                return_value="📊 *Tus categorías:*\n• Comida",
            ),
        ):
            result = await process_incoming_message("12345", "mis categorías")

        assert result.service_invoked == "finance"
        assert "Comida" in result.reply_text


# ---------------------------------------------------------------------------
# Multi-turn: reminder data
# ---------------------------------------------------------------------------

class TestReminderMultiTurn:
    @pytest.mark.asyncio
    async def test_awaiting_reminder_data_completes(self):
        from app.services.reminder import ReminderResult

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_reminder",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"reminder_day": 20},
            ),
            patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                return_value=ReminderResult(status="created", message="ok"),
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            from app.services.conversation import PendingReminder
            from decimal import Decimal

            mock_pending.return_value = PendingReminder(
                sender_phone="12345",
                reminder_concept="cable",
                reminder_day=None,
                reminder_amount=Decimal("2500"),
                reminder_currency="ARS",
            )
            result = await process_incoming_message("12345", "el 20")

        assert result.service_invoked == "conversation"
        assert "cable" in result.reply_text

    @pytest.mark.asyncio
    async def test_awaiting_reminder_data_no_pending(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_reminder",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "el 20")

        assert "contexto" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_awaiting_reminder_data_no_day_llm(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_reminder",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"reminder_day": None},
            ),
        ):
            from app.services.conversation import PendingReminder

            mock_pending.return_value = PendingReminder(
                sender_phone="12345",
                reminder_concept="cable",
                reminder_day=None,
                reminder_amount=None,
                reminder_currency="ARS",
            )
            result = await process_incoming_message("12345", "abc")

        assert "día" in result.reply_text.lower() or "dia" in result.reply_text.lower()


class TestRenameMultiTurn:
    @pytest.mark.asyncio
    async def test_awaiting_rename_completes(self):
        from app.services.reminder import ReminderResult

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_rename",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                return_value=ReminderResult(status="created", message="ok"),
            ),
        ):
            from app.services.conversation import PendingReminder
            from decimal import Decimal

            mock_pending.return_value = PendingReminder(
                sender_phone="12345",
                reminder_concept=None,
                reminder_day=15,
                reminder_amount=Decimal("1000"),
                reminder_currency="ARS",
            )
            result = await process_incoming_message("12345", "nuevo nombre")

        assert result.service_invoked == "conversation"

    @pytest.mark.asyncio
    async def test_awaiting_rename_no_pending(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_rename",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "nuevo nombre")

        assert "contexto" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_awaiting_rename_empty_text(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_rename",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            from app.services.conversation import PendingReminder

            mock_pending.return_value = PendingReminder(
                sender_phone="12345",
                reminder_concept=None,
                reminder_day=15,
                reminder_amount=None,
                reminder_currency="ARS",
            )
            result = await process_incoming_message("12345", "   ")

        assert "nombre" in result.reply_text.lower()


# ---------------------------------------------------------------------------
# Create reminder flow
# ---------------------------------------------------------------------------

class TestCreateReminder:
    @pytest.mark.asyncio
    async def test_create_reminder_with_day(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "create_reminder",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                return_value=ReminderResult(status="created", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "avisame del cable el 15")

        assert result.service_invoked == "reminder"
        assert "luz" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_create_reminder_missing_concept(self):
        with reminder_patches(
            "create_reminder",
            reminder_concept=None,
        ):
            result = await process_incoming_message("12345", "recordame")

        assert "nombre" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_create_reminder_missing_day_starts_multiturn(self):
        with reminder_patches(
            "create_reminder",
            reminder_concept="cable",
            reminder_day=None,
            service_patch=patch(
                "app.services.dispatcher.ConversationService.set_pending_reminder",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "avisame del cable")

        assert result.service_invoked == "reminder"
        assert "día" in result.reply_text.lower() or "dia" in result.reply_text.lower()


# ---------------------------------------------------------------------------
# Unknown-intent fallback
# ---------------------------------------------------------------------------

class TestUnknownIntentFallback:
    @pytest.mark.asyncio
    async def test_unknown_intent_is_not_reclassified(self):
        llm_result = {
            "intent": "some_unknown_intent",
            "reply_text": "legacy",
        }

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
                return_value=registered_result(),
            ) as mock_register,
            patch(
                "app.services.dispatcher.ConversationService.set_last_movement",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "Gasté 5000 en supermercado")

        assert result.service_invoked == "llm"
        assert result.reply_text == "legacy"
        mock_llm.assert_awaited_once()
        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_intent_uses_safe_reply(self):
        llm_result = {"intent": "unknown_thing", "reply_text": ""}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "xyz")

        assert result.reply_text
        mock_llm.assert_awaited_once()


# ---------------------------------------------------------------------------
# /link more decisions
# ---------------------------------------------------------------------------

class TestLinkMoreDecisions:
    def make_result(self, decision):
        return DashboardLinkResult(decision)

    @pytest.mark.asyncio
    async def test_link_not_eligible(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_result(DashboardLinkDecision.NOT_ELIGIBLE),
            ),
        ):
            result = await process_incoming_message("12345", "/link")

        assert result.service_invoked == "dashboard_link"
        assert "cuenta vinculada" in result.reply_text

    @pytest.mark.asyncio
    async def test_link_error(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_result(DashboardLinkDecision.ERROR),
            ),
        ):
            result = await process_incoming_message("12345", "/link")

        assert result.service_invoked == "dashboard_link"
        assert "enlace" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_link_suppress_response(self):
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.DashboardLinkService.generate_or_reuse",
                return_value=self.make_result(DashboardLinkDecision.SUPPRESS_RESPONSE),
            ),
        ):
            result = await process_incoming_message("12345", "/link")

        assert result.service_invoked == "dashboard_link"
        assert result.reply_text == ""


# ---------------------------------------------------------------------------
# More multi-turn / reminder edge cases
# ---------------------------------------------------------------------------

class TestReminderMultiTurnMore:
    @pytest.mark.asyncio
    async def test_awaiting_reminder_extracts_day_from_text(self):
        """When LLM returns no day, dispatcher extracts a number from the text."""
        from app.services.conversation import PendingReminder
        from app.services.reminder import ReminderResult

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_rename",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_reminder",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value={"reminder_day": None},
            ),
            patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                return_value=ReminderResult(status="created", message="ok"),
            ),
        ):
            mock_pending.return_value = PendingReminder(
                sender_phone="12345",
                reminder_concept="cable",
                reminder_day=None,
                reminder_amount=None,
                reminder_currency="ARS",
            )
            result = await process_incoming_message("12345", "el 20")

        assert result.service_invoked == "conversation"
        assert "20" in result.reply_text

    @pytest.mark.asyncio
    async def test_update_reminder_find_by_title_raises(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "update_reminder",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.update_reminder",
                return_value=ReminderResult(status="updated", message="ok"),
            ),
        ):
            with patch(
                "app.services.dispatcher.ReminderService.find_by_title",
                side_effect=Exception("db down"),
            ):
                result = await process_incoming_message("12345", "cambia luz")

        assert result.service_invoked == "reminder"

    @pytest.mark.asyncio
    async def test_pause_reminder_by_title(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "pause_reminder",
            reminder_concept="luz",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.pause_by_title",
                return_value=ReminderResult(status="paused", message="ok"),
            ),
        ):
            result = await process_incoming_message("12345", "pausá luz")

        assert result.service_invoked == "reminder"

    @pytest.mark.asyncio
    async def test_create_reminder_duplicate_title_sets_rename(self):
        from app.services.reminder import ReminderResult

        with reminder_patches(
            "create_reminder",
            service_patch=patch(
                "app.services.dispatcher.ReminderService.create_reminder",
                return_value=ReminderResult(status="duplicate_title", message="Ya existe"),
            ),
        ):
            with patch(
                "app.services.dispatcher.ConversationService.set_pending_rename",
                new_callable=AsyncMock,
            ) as mock_rename:
                result = await process_incoming_message("12345", "avisame del cable el 15")

        assert result.service_invoked == "reminder"
        mock_rename.assert_awaited_once()


# ---------------------------------------------------------------------------
# Single-pass dispatcher paths
# ---------------------------------------------------------------------------

class TestSinglePassDispatcher:
    @pytest.mark.asyncio
    async def test_delete_category_uses_one_classification(self):
        llm_result = {"intent": "delete_category", "category": "Comida", "reply_text": ""}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher._handle_delete_category",
                new_callable=AsyncMock,
                return_value="✅ Categoría 'Comida' eliminada.",
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "borrá comida")

        assert result.service_invoked == "finance"
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_categories_uses_one_classification(self):
        llm_result = {"intent": "list_categories", "reply_text": ""}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher._handle_list_categories",
                new_callable=AsyncMock,
                return_value="📊 *Tus categorías:*",
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "mis categorías")

        assert result.service_invoked == "finance"
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirm_category_without_pending_uses_one_classification(self):
        llm_result = {"intent": "confirm_category", "reply_text": ""}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "si")

        assert result.service_invoked == "conversation"
        assert "pendiente" in result.reply_text.lower()
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_intent_does_not_enter_a_second_movement_path(self):
        llm_result = {"intent": "unknown_intent", "reply_text": "no entendido"}

        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
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
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
            patch(
                "app.services.dispatcher.ConversationService.set_pending_movement",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "Gasté 5000", "wamid.9")

        assert result.service_invoked == "llm"
        assert result.reply_text == "no entendido"
        mock_llm.assert_awaited_once()
        mock_pending.assert_not_awaited()


# ---------------------------------------------------------------------------
# Direct category handler unit tests (with fake session)
# ---------------------------------------------------------------------------

class TestCategoryHandlers:
    """Direct tests for _handle_delete_category / _handle_list_categories."""

    @pytest.fixture
    def fake_session_factory(self):
        class FakeUser:
            id = "user-1"

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return FakeUser()

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        return lambda: FakeSession()

    @pytest.mark.asyncio
    async def test_handle_delete_category_deleted(self, fake_session_factory):
        from app.services.dispatcher import _handle_delete_category
        from app.services.finance import CategoryResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.FinanceService.delete_category",
                return_value=CategoryResult(status="deleted", message="ok", category_name="Comida"),
            ),
        ):
            reply = await _handle_delete_category("12345", {"category": "Comida"})

        assert "Comida" in reply

    @pytest.mark.asyncio
    async def test_handle_delete_category_not_found(self, fake_session_factory):
        from app.services.dispatcher import _handle_delete_category
        from app.services.finance import CategoryResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.FinanceService.delete_category",
                return_value=CategoryResult(status="not_found", message="nf"),
            ),
        ):
            reply = await _handle_delete_category("12345", {"category": "Comida"})

        assert "Comida" in reply

    @pytest.mark.asyncio
    async def test_handle_delete_category_error(self, fake_session_factory):
        from app.services.dispatcher import _handle_delete_category
        from app.services.finance import CategoryResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.FinanceService.delete_category",
                return_value=CategoryResult(status="error", message="err"),
            ),
        ):
            reply = await _handle_delete_category("12345", {"category": "Comida"})

        assert "problema" in reply

    @pytest.mark.asyncio
    async def test_handle_delete_category_missing_name(self, fake_session_factory):
        from app.services.dispatcher import _handle_delete_category

        with patch("app.models.database.SessionLocal", fake_session_factory):
            reply = await _handle_delete_category("12345", {"category": None})

        assert "categoría" in reply.lower()

    @pytest.mark.asyncio
    async def test_handle_list_categories_ok(self, fake_session_factory):
        from app.services.dispatcher import _handle_list_categories
        from app.services.finance import CategoriesListResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.FinanceService.get_categories_with_totals",
                return_value=CategoriesListResult(status="ok", message="ok"),
            ),
        ):
            reply = await _handle_list_categories("12345")

        assert "No tenés categorías" in reply

    @pytest.mark.asyncio
    async def test_handle_list_categories_error(self, fake_session_factory):
        from app.services.dispatcher import _handle_list_categories
        from app.services.finance import CategoriesListResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.FinanceService.get_categories_with_totals",
                return_value=CategoriesListResult(status="error", message="err"),
            ),
        ):
            reply = await _handle_list_categories("12345")

        assert "problema" in reply

    @pytest.mark.asyncio
    async def test_handle_list_categories_user_not_found(self):
        from app.services.dispatcher import _handle_list_categories

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        with patch("app.models.database.SessionLocal", lambda: FakeSession()):
            reply = await _handle_list_categories("12345")

        assert "cuenta" in reply

    @pytest.mark.asyncio
    async def test_handle_delete_category_user_not_found(self):
        from app.services.dispatcher import _handle_delete_category
        from app.services.finance import CategoryResult

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        with (
            patch("app.models.database.SessionLocal", lambda: FakeSession()),
            patch(
                "app.services.dispatcher.FinanceService.delete_category",
                return_value=CategoryResult(status="deleted", message="ok"),
            ),
        ):
            reply = await _handle_delete_category("12345", {"category": "Comida"})

        assert "cuenta" in reply


# ---------------------------------------------------------------------------
# _handle_list_reminders / _update_ultimo_mensaje
# ---------------------------------------------------------------------------

class TestReminderListHandler:
    @pytest.fixture
    def fake_session_factory(self):
        class FakeUser:
            id = "user-1"

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return FakeUser()

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        return lambda: FakeSession()

    @pytest.mark.asyncio
    async def test_list_reminders_success(self, fake_session_factory):
        from app.services.dispatcher import _handle_list_reminders
        from app.services.reminder import ReminderListResult

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.ReminderService.list_reminders_all",
                return_value=ReminderListResult(
                    status="ok",
                    message="ok",
                    reminders=[{"titulo": "Luz", "dia_del_mes": 15, "monto": 5000, "moneda": "ARS", "estado": "activo"}],
                ),
            ),
        ):
            reply = await _handle_list_reminders("12345")

        assert "Luz" in reply

    @pytest.mark.asyncio
    async def test_list_reminders_user_not_found(self):
        from app.services.dispatcher import _handle_list_reminders

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        with patch("app.models.database.SessionLocal", lambda: FakeSession()):
            reply = await _handle_list_reminders("12345")

        assert "cuenta" in reply

    @pytest.mark.asyncio
    async def test_list_reminders_error(self, fake_session_factory):
        from app.services.dispatcher import _handle_list_reminders

        with (
            patch("app.models.database.SessionLocal", fake_session_factory),
            patch(
                "app.services.dispatcher.ReminderService.list_reminders_all",
                side_effect=Exception("boom"),
            ),
        ):
            reply = await _handle_list_reminders("12345")

        assert "problema" in reply


class TestChangeCategoryEdgeCases:
    def make_session(self, user=None):
        class FakeUser:
            id = "user-1"

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def first(self):
                return FakeUser() if user is not None else None

        class FakeSession:
            def query(self, *a, **k):
                return FakeQuery()

            def close(self):
                pass

        return lambda: FakeSession()

class TestUpdateUltimoMensaje:
    def test_success_commits(self):
        from app.services.dispatcher import _update_ultimo_mensaje

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def update(self, *a, **k):
                return 1

        class FakeSession:
            def __init__(self):
                self.committed = False

            def query(self, *a, **k):
                return FakeQuery()

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        session = FakeSession()
        with patch("app.services.dispatcher.SessionLocal", lambda: session):
            _update_ultimo_mensaje("12345")

        assert session.committed is True

    def test_error_rolls_back(self):
        from app.services.dispatcher import _update_ultimo_mensaje

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def update(self, *a, **k):
                raise Exception("db down")

        class FakeSession:
            def __init__(self):
                self.rolled = False

            def query(self, *a, **k):
                return FakeQuery()

            def commit(self):
                pass

            def rollback(self):
                self.rolled = True

            def close(self):
                pass

        session = FakeSession()
        with patch("app.services.dispatcher.SessionLocal", lambda: session):
            _update_ultimo_mensaje("12345")

        assert session.rolled is True


class TestMovementNoCategory:
    @pytest.mark.asyncio
    async def test_expense_without_category_uses_whatsapp_text_registration(self):
        """_register_and_reply_with_hint falls back to register_movement_from_whatsapp_text."""
        with (
            patch(
                "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
                return_value=known_user(),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                new_callable=AsyncMock,
                return_value=movement_llm_result(category=None),
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
            patch(
                "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
                return_value=registered_result(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_movement",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher._update_ultimo_mensaje",
            ),
        ):
            result = await process_incoming_message("12345", "Gasté 5000 en supermercado", "wamid.10")

        assert result.service_invoked == "finance"
        assert "5000" in result.reply_text
