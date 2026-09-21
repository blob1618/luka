"""Tests del dispatcher para límites de gasto por categoría."""

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.conversation import LastCreatedLimit, PendingLimit, PendingLimitDelete
from app.services.dispatcher import process_incoming_message
from app.services.limit import LimitBatchResult, LimitResult
from app.services.onboarding import OnboardingDecision, OnboardingResult


ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def known_user():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def create_limit_llm(**overrides):
    result = {
        "intent": "create_limit",
        "limit_category": "Ropa",
        "limit_amount": 300000,
        "limit_month": None,
        "limit_year": None,
        "reply_text": "LLM dice",
    }
    result.update(overrides)
    return result


@contextmanager
def limit_flow_patches(**overrides):
    defaults = dict(
        onboarding=known_user(),
        llm=create_limit_llm(),
        awaiting_rename=False,
        awaiting_reminder=False,
        awaiting_limit_year=False,
        awaiting_limit_category=False,
        awaiting_limit_data=False,
        awaiting_limit_delete_category=False,
        awaiting_limit_month=False,
    )
    defaults.update(overrides)

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=defaults["onboarding"],
        ),
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
            "app.services.dispatcher.ConversationService.is_awaiting_limit_year_confirmation",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_limit_year"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_category_confirmation",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_limit_category"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_data",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_limit_data"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_delete_category",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_limit_delete_category"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_month_selection",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_limit_month"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_compensation_confirmation",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=defaults["llm"],
        ) as mock_llm,
        patch(
            "app.services.dispatcher._update_ultimo_mensaje",
        ),
        patch(
            "app.services.dispatcher.ConversationService.clear_state",
            new_callable=AsyncMock,
        ),
    ):
        yield {"llm": mock_llm}


def created_result(**overrides):
    result = {
        "status": "created",
        "message": "ok",
        "limit_id": "abc-123",
        "category_name": "Ropa",
        "amount": Decimal("300000"),
        "month": 7,
        "year": 2026,
    }
    result.update(overrides)
    return LimitResult(**result)


class TestCreateLimitFlow:
    @pytest.mark.asyncio
    async def test_create_limit_registers_and_stores_last(self):
        with (
            limit_flow_patches(),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ) as mock_set_last,
        ):
            result = await process_incoming_message("12345", "mi límite máximo para ropa será de 300.000")

        assert result.intent == "create_limit"
        assert result.service_invoked == "limit"
        assert "Registré tu límite para" in result.reply_text
        assert "Categoría: Ropa" in result.reply_text
        assert "300.000,00" in result.reply_text
        assert "cambiamos" in result.reply_text
        mock_set_last.assert_awaited_once()
        saved = mock_set_last.await_args.args[1]
        assert saved.category_name == "Ropa"

    @pytest.mark.asyncio
    async def test_create_limit_past_month_asks_year_confirmation(self):
        with (
            limit_flow_patches(
                llm=create_limit_llm(limit_month=1, limit_amount=None, limit_category=None),
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=LimitResult(
                    status="needs_year_confirmation",
                    message="past",
                    proposed_month=1,
                    proposed_year=2027,
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            result = await process_incoming_message("12345", "establece un límite maximo para enero")

        assert "¿Quieres crear un límite de gastos para Enero de 2027?" in result.reply_text
        assert result.service_invoked == "limit"
        mock_pending.assert_awaited_once()
        assert mock_pending.await_args.kwargs["step"] == "awaiting_limit_year_confirmation"

    @pytest.mark.asyncio
    async def test_create_limit_missing_amount_asks(self):
        with (
            limit_flow_patches(llm=create_limit_llm(limit_amount=None)),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=LimitResult(
                    status="needs_amount",
                    message="amount",
                    category_name="Ropa",
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            result = await process_incoming_message("12345", "poné un límite para ropa")

        assert "monto" in result.reply_text.lower()
        mock_pending.assert_awaited_once()
        assert mock_pending.await_args.kwargs["step"] == "awaiting_limit_data"

    @pytest.mark.asyncio
    async def test_create_limit_missing_category_asks(self):
        with (
            limit_flow_patches(llm=create_limit_llm(limit_category=None)),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=LimitResult(
                    status="needs_category",
                    message="category",
                    amount=Decimal("300000"),
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "límite de 300000")

        assert "categoría" in result.reply_text.lower()


class TestChangeLimitFlow:
    @pytest.mark.asyncio
    async def test_change_limit_edits_last_created(self):
        last_limit = LastCreatedLimit(
            limit_id="abc-123",
            sender_phone="12345",
            category_name="Ropa",
            amount=Decimal("300000"),
            month=7,
            year=2026,
        )
        with (
            limit_flow_patches(
                llm=create_limit_llm(intent="change_limit", limit_month=8),
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_last_limit",
                new_callable=AsyncMock,
                return_value=last_limit,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(month=8),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "mejor que sea para agosto")

        assert result.intent == "change_limit"
        assert "se Registró" in result.reply_text
        assert "Ropa" in result.reply_text
        assert "300.000,00" in result.reply_text
        call_data = mock_create.call_args.args[1]
        assert call_data["limit_month"] == 8

    @pytest.mark.asyncio
    async def test_change_limit_without_matching_limit_guides_user(self):
        with (
            limit_flow_patches(llm=create_limit_llm(intent="change_limit")),
            patch(
                "app.services.dispatcher.ConversationService.get_last_limit",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.dispatcher.LimitService.find_limit_candidates",
                return_value=[],
            ),
        ):
            result = await process_incoming_message("12345", "mejor que sea para agosto")

        assert "No encontré un límite vigente de Ropa" in result.reply_text


class TestListLimitsFlow:
    @pytest.mark.asyncio
    async def test_list_limits_formats_entries(self):
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

        from app.services.limit import LimitEntry, LimitListResult

        with (
            limit_flow_patches(llm={"intent": "list_limits", "reply_text": "consultando"}),
            patch("app.models.database.SessionLocal", lambda: FakeSession()),
            patch(
                "app.services.dispatcher.LimitService.list_limits",
                return_value=LimitListResult(
                    status="ok",
                    message="ok",
                    limits=[LimitEntry("Comida", Decimal("300000"), 7, 2026)],
                ),
            ),
        ):
            result = await process_incoming_message("12345", "mostrame mis límites")

        assert result.intent == "list_limits"
        assert "Comida" in result.reply_text
        assert "300.000,00" in result.reply_text

    @pytest.mark.asyncio
    async def test_list_limits_empty(self):
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

        from app.services.limit import LimitListResult

        with (
            limit_flow_patches(llm={"intent": "list_limits", "reply_text": "consultando"}),
            patch("app.models.database.SessionLocal", lambda: FakeSession()),
            patch(
                "app.services.dispatcher.LimitService.list_limits",
                return_value=LimitListResult(status="ok", message="ok", limits=[]),
            ),
        ):
            result = await process_incoming_message("12345", "mostrame mis límites")

        assert "No tenés límites" in result.reply_text


class TestDeleteLimitFlow:
    @pytest.mark.asyncio
    async def test_delete_limit_single(self):
        with (
            limit_flow_patches(
                llm={
                    "intent": "delete_limit",
                    "limit_category": "Comida",
                    "limit_month": None,
                    "limit_year": None,
                    "reply_text": "procesando",
                },
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limit",
                return_value=LimitResult(
                    status="deleted",
                    message="ok",
                    category_name="Comida",
                    month=7,
                    year=2026,
                ),
            ),
        ):
            result = await process_incoming_message("12345", "eliminá el límite de comida")

        assert result.intent == "delete_limit"
        assert "eliminé el límite de Comida" in result.reply_text

    @pytest.mark.asyncio
    async def test_delete_limit_asks_month_selection(self):
        candidates = [
            {"limit_id": "a", "month": 7, "year": 2026, "amount": Decimal("300000")},
            {"limit_id": "b", "month": 11, "year": 2026, "amount": Decimal("400000")},
        ]
        with (
            limit_flow_patches(
                llm={
                    "intent": "delete_limit",
                    "limit_category": "Comida",
                    "limit_month": None,
                    "limit_year": None,
                    "reply_text": "procesando",
                },
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limit",
                return_value=LimitResult(
                    status="needs_month_selection",
                    message="select",
                    category_name="Comida",
                    candidates=candidates,
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit_delete",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            result = await process_incoming_message("12345", "eliminá el límite de comida")

        assert "¿A cuál te referís?" in result.reply_text
        assert "300.000,00" in result.reply_text
        mock_pending.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_limit_without_category_asks(self):
        with (
            limit_flow_patches(
                llm={
                    "intent": "delete_limit",
                    "limit_category": None,
                    "limit_month": None,
                    "limit_year": None,
                    "reply_text": "procesando",
                },
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit_delete_category",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            result = await process_incoming_message("12345", "eliminá un límite")

        assert "categoría" in result.reply_text.lower()
        mock_pending.assert_awaited_once()


class TestLimitMultiTurn:
    @pytest.mark.asyncio
    async def test_year_confirmation_confirm_creates(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Transporte",
            amount=Decimal("80000"),
            month=1,
            year=2027,
        )
        with (
            limit_flow_patches(
                awaiting_limit_year=True,
                llm={"intent": "confirm_limit", "reply_text": "dale"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Transporte",
                    amount=Decimal("80000"),
                    month=1,
                    year=2027,
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "si")

        assert result.service_invoked == "conversation"
        assert "Registré tu límite para" in result.reply_text
        assert "Transporte" in result.reply_text
        assert "80.000,00" in result.reply_text
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_year_confirmation_affirmative_fallback_creates(self):
        """Si el LLM no devuelve confirm_limit para 'si' (lo clasifica como
        out_of_scope), un 'si' textual debe confirmar y crear el límite."""
        pending = PendingLimit(
            sender_phone="12345",
            category="Fiestas",
            amount=Decimal("450000"),
            month=6,
            year=2027,
        )
        with (
            limit_flow_patches(
                awaiting_limit_year=True,
                llm={"intent": "out_of_scope", "reply_text": "x"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Fiestas",
                    amount=Decimal("450000"),
                    month=6,
                    year=2027,
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "si")

        assert result.service_invoked == "conversation"
        assert "Registré tu límite para" in result.reply_text
        assert "Fiestas" in result.reply_text
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_year_confirmation_reject_cancels(self):
        pending = PendingLimit(
            sender_phone="12345",
            category=None,
            amount=None,
            month=1,
            year=2027,
        )
        with (
            limit_flow_patches(
                awaiting_limit_year=True,
                llm={"intent": "reject_limit", "reply_text": "no"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "no")

        assert "no creé ningún límite" in result.reply_text
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_year_confirmation_unrelated_message_falls_through(self):
        """Mensaje no relacionado mientras se espera confirmar el año no debe
        secuestrar todos los mensajes: se abandona el flujo del límite, se limpia
        el estado y el mensaje se procesa con normalidad."""
        pending = PendingLimit(
            sender_phone="12345",
            category=None,
            amount=None,
            month=1,
            year=2027,
        )
        with (
            limit_flow_patches(
                awaiting_limit_year=True,
                llm={"intent": "greeting", "reply_text": "hola"},
            ) as mocks,
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "no sé")

        assert "¿Quieres crear un límite de gastos para Enero de 2027?" not in result.reply_text
        assert result.service_invoked == "llm"
        mocks["llm"].assert_awaited_once()
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_year_confirmation_no_pending(self):
        with (
            limit_flow_patches(awaiting_limit_year=True),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "si")

        assert "contexto" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_awaiting_limit_data_completes_with_amount(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Comida",
            amount=None,
            month=7,
            year=2026,
        )
        with (
            limit_flow_patches(
                awaiting_limit_data=True,
                llm={"intent": "create_limit", "limit_amount": 80000, "limit_month": None, "limit_year": None},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Comida",
                    amount=Decimal("80000"),
                ),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "que sea de 80000")

        assert "Registré tu límite para" in result.reply_text
        assert "80.000,00" in result.reply_text
        call_data = mock_create.call_args.args[1]
        assert call_data["limit_amount"] == 80000
        assert call_data["limit_category"] == "Comida"

    @pytest.mark.asyncio
    async def test_awaiting_limit_data_missing_both(self):
        pending = PendingLimit(
            sender_phone="12345",
            category=None,
            amount=None,
            month=1,
            year=2027,
        )
        with (
            limit_flow_patches(
                awaiting_limit_data=True,
                llm={"intent": "create_limit", "limit_amount": None, "limit_month": None, "limit_year": None},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=LimitResult(status="needs_category", message="cat"),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "no sé")

        assert "categoría" in result.reply_text.lower()

    @pytest.mark.asyncio
    async def test_awaiting_limit_month_selection_deletes(self):
        pending_delete = PendingLimitDelete(
            sender_phone="12345",
            category_name="Comida",
            candidates=[{"limit_id": "b", "month": 11, "year": 2026, "amount": "400000"}],
        )
        with (
            limit_flow_patches(
                awaiting_limit_month=True,
                llm={"intent": "create_limit", "limit_month": 11, "limit_year": None},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=pending_delete,
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limits_by_ids",
                return_value=LimitBatchResult(
                    "deleted", [{"limit_id": "b", "month": 11, "year": 2026}]
                ),
            ) as mock_delete,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "noviembre")

        assert "eliminé el límite de Comida" in result.reply_text
        assert mock_delete.call_args.args[1][0]["month"] == 11

    @pytest.mark.asyncio
    async def test_month_selection_reasks_when_currency_is_ambiguous(self):
        pending_delete = PendingLimitDelete(
            sender_phone="12345",
            category_name="Comida",
            candidates=[
                {
                    "limit_id": "ars",
                    "month": 11,
                    "year": 2026,
                    "amount": "400000",
                    "currency": "ARS",
                },
                {
                    "limit_id": "usd",
                    "month": 11,
                    "year": 2026,
                    "amount": "500",
                    "currency": "USD",
                },
            ],
        )
        with (
            limit_flow_patches(
                awaiting_limit_month=True,
                llm={
                    "intent": "delete_limit",
                    "limit_month": 11,
                    "limit_year": 2026,
                    "limit_currency": None,
                },
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=pending_delete,
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limits_by_ids",
            ) as delete_limit,
        ):
            result = await process_incoming_message("12345", "noviembre de 2026")

        delete_limit.assert_not_called()
        assert "¿A cuál te referís?" in result.reply_text
        assert "ARS" in result.reply_text
        assert "USD" in result.reply_text

    @pytest.mark.asyncio
    async def test_awaiting_limit_month_selection_no_month_reasks(self):
        pending_delete = PendingLimitDelete(
            sender_phone="12345",
            category_name="Comida",
            candidates=[{"limit_id": "b", "month": 11, "year": 2026, "amount": "400000"}],
        )
        with (
            limit_flow_patches(
                awaiting_limit_month=True,
                llm={"intent": "greeting", "limit_month": None, "reply_text": "hola"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=pending_delete,
            ),
        ):
            result = await process_incoming_message("12345", "no sé")

        assert "¿A cuál te referís?" in result.reply_text


class TestLimitMultiTurnFixes:
    @pytest.mark.asyncio
    async def test_awaiting_limit_data_extracts_amount_from_plain_number(self):
        """Flujo 1: '100000' suelto debe completar el monto sin depender del LLM."""
        pending = PendingLimit(
            sender_phone="12345",
            category="Ocio",
            amount=None,
            month=8,
            year=2026,
        )
        with (
            limit_flow_patches(
                awaiting_limit_data=True,
                llm={"intent": "expense", "amount": 100000, "limit_amount": None},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Ocio",
                    amount=Decimal("100000"),
                ),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "100000")

        assert "Registré tu límite para" in result.reply_text
        call_data = mock_create.call_args.args[1]
        assert call_data["limit_amount"] == 100000
        assert call_data["limit_category"] == "Ocio"

    @pytest.mark.asyncio
    async def test_awaiting_limit_data_extracts_amount_when_llm_ignores(self):
        """Flujo 1: 'el limite es 100000' sin limit_amount del LLM."""
        pending = PendingLimit(
            sender_phone="12345",
            category="Ocio",
            amount=None,
            month=8,
            year=2026,
        )
        with (
            limit_flow_patches(
                awaiting_limit_data=True,
                llm={"intent": "out_of_scope", "limit_amount": None, "reply_text": "x"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Ocio",
                    amount=Decimal("100000"),
                ),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "el limite es 100000")

        assert "Registré tu límite para" in result.reply_text
        assert mock_create.call_args.args[1]["limit_amount"] == 100000

    @pytest.mark.asyncio
    async def test_awaiting_limit_data_cancel(self):
        """Flujo 1: 'cancelalo' debe cancelar el flujo y limpiar el estado."""
        pending = PendingLimit(
            sender_phone="12345",
            category="Ocio",
            amount=None,
            month=8,
            year=2026,
        )
        with (
            limit_flow_patches(
                awaiting_limit_data=True,
                llm={"intent": "reject_limit", "reply_text": "no"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "cancelalo")

        assert "cancelé" in result.reply_text.lower()
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_limit_delete_category_provides_category(self):
        """Flujo 2: tras pedir la categoría, 'ocio' completa la eliminación por mes."""
        pending_delete = PendingLimitDelete(
            sender_phone="12345",
            category_name=None,
            month=9,
            year=2026,
        )
        with (
            limit_flow_patches(
                awaiting_limit_delete_category=True,
                llm={"intent": "out_of_scope", "limit_category": None, "reply_text": "x"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=pending_delete,
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limit",
                return_value=LimitResult(
                    status="deleted", message="ok", category_name="Ocio",
                    month=9, year=2026,
                ),
            ) as mock_delete,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "ocio")

        assert "eliminé el límite de Ocio" in result.reply_text
        assert mock_delete.call_args.args[1] == "ocio"
        assert mock_delete.call_args.kwargs["month"] == 9

    @pytest.mark.asyncio
    async def test_awaiting_limit_month_selection_month_name_fallback(self):
        """Flujo 2: 'el de septiembre' resuelve el mes 9 aunque el LLM no lo devuelva."""
        pending_delete = PendingLimitDelete(
            sender_phone="12345",
            category_name="Ocio",
            candidates=[{"limit_id": "b", "month": 9, "year": 2026, "amount": "300000"}],
        )
        with (
            limit_flow_patches(
                awaiting_limit_month=True,
                llm={"intent": "greeting", "limit_month": None, "reply_text": "hola"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit_delete",
                new_callable=AsyncMock,
                return_value=pending_delete,
            ),
            patch(
                "app.services.dispatcher.LimitService.delete_limits_by_ids",
                return_value=LimitBatchResult(
                    "deleted", [{"limit_id": "b", "month": 9, "year": 2026}]
                ),
            ) as mock_delete,
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "el de septiembre")

        assert "eliminé el límite de Ocio" in result.reply_text
        assert mock_delete.call_args.args[1][0]["month"] == 9

    @pytest.mark.asyncio
    async def test_change_limit_month_edits_existing_limit(self):
        """Flujo 2: 'cambia el mes por agosto' debe editar el último límite, no crear otro."""
        last_limit = LastCreatedLimit(
            limit_id="abc-123",
            sender_phone="12345",
            category_name="Ocio",
            amount=Decimal("300000"),
            month=9,
            year=2026,
        )
        with (
            limit_flow_patches(
                llm=create_limit_llm(intent="change_limit", limit_month=8),
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_last_limit",
                new_callable=AsyncMock,
                return_value=last_limit,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(month=8),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "cambia el mes por agosto")

        assert result.intent == "change_limit"
        assert "Agosto" in result.reply_text
        assert mock_create.call_args.kwargs["last_limit"] is last_limit
        assert mock_create.call_args.args[1]["limit_month"] == 8

    @pytest.mark.asyncio
    async def test_year_confirmation_edit_reuses_last_limit(self):
        """Flujo 2: confirmar el año de una edición debe conservar el limit_id."""
        pending = PendingLimit(
            sender_phone="12345",
            category="Ocio",
            amount=Decimal("300000"),
            month=1,
            year=2027,
            is_edit=True,
            limit_id="abc-123",
        )
        with (
            limit_flow_patches(
                awaiting_limit_year=True,
                llm={"intent": "confirm_limit", "reply_text": "dale"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(month=1, year=2027),
            ) as mock_create,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "si")

        assert "Registró tu límite para" in result.reply_text
        assert mock_create.call_args.kwargs["last_limit"].limit_id == "abc-123"
        assert mock_create.call_args.kwargs["last_limit"].month == 1


class TestLimitCategoryConfirmation:
    @pytest.mark.asyncio
    async def test_unknown_category_is_not_created_without_confirmation(self):
        with (
            limit_flow_patches(),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=LimitResult(
                    status="needs_category_confirmation",
                    message="confirm",
                    category_name="Ropa",
                    amount=Decimal("300000"),
                    month=9,
                    year=2026,
                    currency="ARS",
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_limit",
                new_callable=AsyncMock,
            ) as set_pending,
        ):
            result = await process_incoming_message(
                "12345",
                "poné un límite de 300000 para ropa",
            )

        assert "¿Querés crearla y aplicar el límite?" in result.reply_text
        assert set_pending.await_args.kwargs["step"] == (
            "awaiting_limit_category_confirmation"
        )

    @pytest.mark.asyncio
    async def test_confirmation_authorizes_category_creation(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Ropa",
            amount=Decimal("300000"),
            month=9,
            year=2026,
            currency="ARS",
        )
        with (
            limit_flow_patches(
                awaiting_limit_category=True,
                llm={"intent": "confirm_limit", "reply_text": "dale"},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(month=9),
            ) as create_limit,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "sí")

        assert "Registré tu límite" in result.reply_text
        assert create_limit.call_args.kwargs["allow_category_creation"] is True

    @pytest.mark.asyncio
    async def test_creala_authorizes_dynamic_category_creation(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Viajes",
            amount=Decimal("10000"),
            month=9,
            year=2026,
            currency="ARS",
        )
        with (
            limit_flow_patches(
                awaiting_limit_category=True,
                llm={"intent": "out_of_scope", "reply_text": ""},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Viajes",
                    amount=Decimal("10000"),
                    month=9,
                ),
            ) as create_limit,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message("12345", "creala")

        assert "Registré tu límite" in result.reply_text
        assert create_limit.call_args.kwargs["allow_category_creation"] is True

    @pytest.mark.asyncio
    async def test_alternative_category_keeps_pending_limit_data(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Viajes",
            amount=Decimal("10000"),
            month=9,
            year=2026,
            currency="ARS",
        )
        with (
            limit_flow_patches(
                awaiting_limit_category=True,
                llm={"intent": "out_of_scope", "reply_text": ""},
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.LimitService.create_limit",
                return_value=created_result(
                    category_name="Vacaciones",
                    amount=Decimal("10000"),
                    month=9,
                ),
            ) as create_limit,
            patch(
                "app.services.dispatcher.ConversationService.set_last_limit",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_incoming_message(
                "12345",
                "mejor usa vacaciones",
            )

        assert "Vacaciones" in result.reply_text
        passed_data = create_limit.call_args.args[1]
        assert passed_data["limit_category"].casefold() == "vacaciones"
        assert passed_data["limit_amount"] == 10000.0

    @pytest.mark.asyncio
    async def test_unrelated_message_falls_through_with_one_llm_call(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Ropa",
            amount=Decimal("300000"),
            month=9,
            year=2026,
            currency="ARS",
        )
        with (
            limit_flow_patches(
                awaiting_limit_category=True,
                llm={"intent": "greeting", "reply_text": "hola"},
            ) as mocks,
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "hola, otra consulta")

        assert result.reply_text == "hola"
        assert result.service_invoked == "llm"
        mocks["llm"].assert_awaited_once()
        mock_clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_error_preserves_pending_confirmation(self):
        pending = PendingLimit(
            sender_phone="12345",
            category="Ropa",
            amount=Decimal("300000"),
            month=9,
            year=2026,
            currency="ARS",
        )
        llm_error = {
            "intent": "out_of_scope",
            "reply_text": "No he podido analizar tu mensaje en este momento.",
            "error": "HTTPStatusError: 429 Too Many Requests",
        }
        with (
            limit_flow_patches(
                awaiting_limit_category=True,
                llm=llm_error,
            ) as mocks,
            patch(
                "app.services.dispatcher.ConversationService.get_pending_limit",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            result = await process_incoming_message("12345", "quizás")

        assert result.reply_text == llm_error["reply_text"]
        assert result.raw_llm_response == llm_error
        assert result.service_invoked == "llm"
        mocks["llm"].assert_awaited_once()
        mock_clear.assert_not_awaited()


class TestLimitIntentCorrections:
    @pytest.mark.asyncio
    async def test_plain_limites_routes_to_list_even_if_llm_misses(self):
        with (
            limit_flow_patches(
                llm={"intent": "out_of_scope", "reply_text": "ayuda"},
            ),
            patch(
                "app.services.dispatcher._handle_list_limits",
                new_callable=AsyncMock,
                return_value="lista",
            ) as list_limits,
        ):
            result = await process_incoming_message("12345", "límites")

        assert result.reply_text == "lista"
        list_limits.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_limit_status_routes_to_budget_even_if_llm_lists(self):
        with (
            limit_flow_patches(
                llm={"intent": "list_limits", "reply_text": "lista"},
            ),
            patch(
                "app.services.dispatcher._handle_budget_query",
                new_callable=AsyncMock,
                return_value="estado",
            ) as budget_query,
        ):
            result = await process_incoming_message(
                "12345",
                "muéstrame el estado de mis límites",
            )

        assert result.reply_text == "estado"
        budget_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_current_month_phrase_routes_to_recent_limit_change(self):
        last_limit = LastCreatedLimit(
            limit_id="abc-123",
            sender_phone="12345",
            category_name="Comida",
            amount=Decimal("40000"),
            month=10,
            year=2026,
            currency="ARS",
        )
        with (
            limit_flow_patches(
                llm={
                    "intent": "create_limit",
                    "limit_month": None,
                    "limit_year": None,
                    "reply_text": "",
                },
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_last_limit",
                new_callable=AsyncMock,
                return_value=last_limit,
            ),
            patch(
                "app.services.dispatcher._handle_change_limit",
                new_callable=AsyncMock,
                return_value="actualizado",
            ) as change_limit,
        ):
            result = await process_incoming_message(
                "12345",
                "que sea para el mes actual",
            )

        assert result.reply_text == "actualizado"
        changed_data = change_limit.await_args.args[1]
        assert changed_data["limit_month"] == datetime.now(ARGENTINA_TZ).month
