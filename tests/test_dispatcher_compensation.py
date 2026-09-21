"""Tests del dispatcher para el flujo de compensación de presupuesto."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.budget import BudgetEvaluation, BudgetStatus, BudgetStatusResult
from app.services.compensation import (
    CompensationAllocation,
    CompensationApplyResult,
    CompensationProposal,
    CompensationProposalResult,
)
from app.services.conversation import ConversationService, ConversationState, PendingCompensation
from app.services.dispatcher import (
    _handle_configured_action,
    _movement_budget_after_change,
    _register_multiop,
    _register_single_with_hint,
    process_incoming_message,
)
from app.services.finance import MovementRegistrationResult
from app.services.onboarding import OnboardingDecision, OnboardingResult


def known_user():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def make_allocation(*, category, before, after, spent="0"):
    return CompensationAllocation(
        limit_id=str(uuid4()),
        category_id=str(uuid4()),
        category_name=category,
        before_limit=Decimal(before),
        after_limit=Decimal(after),
        spent_amount=Decimal(spent),
        available_before=Decimal("0"),
    )


def make_proposal():
    target = make_allocation(
        category="Comida", before="2000", after="2500", spent="2500"
    )
    donor = make_allocation(
        category="Transporte", before="1000", after="500", spent="500"
    )
    return CompensationProposal(
        proposal_id="prop-1",
        user_id=str(uuid4()),
        currency="ARS",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        amount=Decimal("500"),
        target=target,
        donors=[donor],
        created_at="2026-09-20T10:00:00-03:00",
        expires_at="2099-09-20T10:30:00-03:00",
        snapshot={target.limit_id: "2000", donor.limit_id: "1000"},
    )


def budget_status(*, category="Comida", state="exceeded"):
    return BudgetStatus(
        limit_id=str(uuid4()),
        user_id=str(uuid4()),
        category_id=str(uuid4()),
        category_name=category,
        currency="ARS",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        limit_amount=Decimal("2000"),
        spent_amount=Decimal("2500"),
        remaining_amount=Decimal("-500"),
        exceeded_amount=Decimal("500"),
        percentage=Decimal("125.0"),
        state=state,
    )


def movement(*, tipo="egreso", category="Comida", currency="ARS"):
    return SimpleNamespace(
        tipo=tipo,
        categoria_nombre=category,
        fecha_movimiento=date(2026, 9, 20),
        moneda=currency,
    )


def compensation_llm(**overrides):
    result = {
        "intent": "compensate_budget",
        "compensation_target": "Comida",
        "compensation_source": "Transporte",
        "compensation_amount": 500,
        "limit_currency": "ARS",
        "reply_text": "Evaluando tu presupuesto.",
    }
    result.update(overrides)
    return result


@contextmanager
def compensation_flow_patches(**overrides):
    defaults = dict(
        llm=compensation_llm(),
        awaiting_compensation=False,
        pending_compensation=None,
    )
    defaults.update(overrides)

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
            "app.services.dispatcher.ConversationService.is_awaiting_limit_year_confirmation",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_category_confirmation",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_data",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_delete_category",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_month_selection",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_compensation_confirmation",
            new_callable=AsyncMock,
            return_value=defaults["awaiting_compensation"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.get_pending_compensation",
            new_callable=AsyncMock,
            return_value=defaults["pending_compensation"],
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_pending_compensation",
            new_callable=AsyncMock,
        ) as mock_set_pending,
        patch(
            "app.services.dispatcher.ConversationService.clear_state",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=defaults["llm"],
        ) as mock_llm,
        patch("app.services.dispatcher._update_ultimo_mensaje"),
    ):
        yield {
            "set_pending": mock_set_pending,
            "clear_state": mock_clear,
            "llm": mock_llm,
        }


class TestUserRequestedCompensation:
    @pytest.mark.asyncio
    async def test_user_request_stores_pending_without_writes(self):
        proposal = make_proposal()
        stored = PendingCompensation(
            sender_phone="12345", proposal=proposal.to_dict()
        )
        with (
            compensation_flow_patches(pending_compensation=stored) as mocks,
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", proposal),
            ) as mock_build,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "compensá mi presupuesto")

        assert result.intent == "compensate_budget"
        assert result.service_invoked == "compensation"
        assert result.event_key == "budget.compensation_proposed"
        assert (
            result.event_variables["summary"]
            == "Compensación de $500 ARS para Comida"
        )
        assert "Muevo $500 ARS" in result.reply_text
        target_line = "Comida: $2000 → $2500 ARS"
        assert result.reply_text.count(target_line) == 1
        donors_block = result.reply_text.split("Donantes:\n", 1)[1].split("\n\n", 1)[0]
        assert "• Transporte: $1000 → $500 ARS" in donors_block
        assert target_line not in donors_block
        assert "El total de tus límites se mantiene" in result.reply_text
        assert "30 minutos" in result.reply_text
        assert "confirmar compensación" in result.reply_text
        assert "no por ahora" in result.reply_text
        mocks["set_pending"].assert_awaited_once()
        saved = mocks["set_pending"].await_args.args[1]
        assert saved["proposal_id"] == "prop-1"
        assert mock_build.call_args.kwargs == {
            "target_category": "Comida",
            "source_category": "Transporte",
            "requested_amount": Decimal("500"),
            "currency": "ARS",
        }
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_request_reports_soft_failure_when_storage_lost(self):
        with (
            compensation_flow_patches(),
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult(
                    "ok", "created", make_proposal()
                ),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message(
                "12345", "compensá mi presupuesto"
            )

        assert result.reply_text == (
            "No pude preparar la propuesta de compensación. Intentá nuevamente."
        )
        assert result.event_key is None
        assert result.service_invoked == "compensation"
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_funds_reply_is_constructive(self):
        with (
            compensation_flow_patches() as mocks,
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult(
                    "no_funds", "no category has available budget"
                ),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "compensá mi presupuesto")

        assert result.service_invoked == "compensation"
        assert "No hay otras categorías con saldo disponible" in result.reply_text
        assert "ajustar el límite" in result.reply_text
        mocks["set_pending"].assert_not_awaited()
        mock_apply.assert_not_called()


class TestCompensationConfirmation:
    @pytest.mark.asyncio
    async def test_text_confirmation_applies_and_clears_state(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={"intent": "confirm_compensation", "reply_text": "Confirmando."},
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                return_value=CompensationApplyResult(
                    "applied", "compensation applied", proposal
                ),
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "confirmar compensación")

        assert result.service_invoked == "compensation"
        assert "Comida: $2000 → $2500 ARS" in result.reply_text
        assert "Transporte: $1000 → $500 ARS" in result.reply_text
        assert "No se modificó ningún movimiento" in result.reply_text
        mock_apply.assert_called_once_with(proposal.to_dict())
        mocks["clear_state"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirmation_when_proposal_stale_asks_for_new_calculation(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={"intent": "confirm_compensation", "reply_text": "Confirmando."},
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                return_value=CompensationApplyResult("stale", "limits changed"),
            ),
        ):
            result = await process_incoming_message("12345", "dale")

        assert result.reply_text == (
            "Los saldos cambiaron desde que armé la propuesta. "
            "Pedime un nuevo cálculo."
        )
        mocks["clear_state"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejection_writes_nothing(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={"intent": "reject_compensation", "reply_text": "Rechazando."},
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "no por ahora")

        assert result.reply_text == "Listo, no cambié ningún límite."
        mocks["clear_state"].assert_awaited_once()
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrelated_message_discards_pending(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={"intent": "greeting", "reply_text": "¡Hola!"},
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "hola")

        assert result.reply_text == "¡Hola!"
        assert result.service_invoked == "llm"
        mocks["clear_state"].assert_awaited_once()
        mocks["llm"].assert_awaited_once()
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_error_rejection_still_rejects_without_writes(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={
                    "intent": "out_of_scope",
                    "error": "ReadTimeout: ",
                    "reply_text": "No he podido analizar tu mensaje en este momento.",
                },
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "no, dejalo")

        assert result.reply_text == "Listo, no cambié ningún límite."
        mock_apply.assert_not_called()
        mocks["clear_state"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_error_confirmation_still_applies(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={
                    "intent": "out_of_scope",
                    "error": "ReadTimeout: ",
                    "reply_text": "No he podido analizar tu mensaje en este momento.",
                },
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                return_value=CompensationApplyResult(
                    "applied", "compensation applied", proposal
                ),
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "sí")

        mock_apply.assert_called_once_with(proposal.to_dict())
        mocks["clear_state"].assert_awaited_once()
        assert "✅ Compensé *Comida*" in result.reply_text
        assert "No se modificó ningún movimiento" in result.reply_text

    @pytest.mark.asyncio
    async def test_llm_error_unrelated_message_keeps_pending(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={
                    "intent": "out_of_scope",
                    "error": "ReadTimeout: ",
                    "reply_text": "No he podido analizar tu mensaje en este momento.",
                },
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message(
                "12345", "¿cómo viene el clima?"
            )

        assert result.reply_text == (
            "No pude procesar tu respuesta. "
            "Respondé *confirmar compensación* o *no por ahora*."
        )
        assert result.service_invoked == "llm"
        assert result.intent == "out_of_scope"
        mocks["clear_state"].assert_not_awaited()
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "intent", ["confirm_compensation", "reject_compensation"]
    )
    async def test_confirmation_without_pending_explains_and_does_not_apply(
        self, intent
    ):
        with (
            compensation_flow_patches(
                llm={"intent": intent, "reply_text": "Estoy procesando."}
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message(
                "12345", "confirmar compensación"
            )

        assert result.reply_text == (
            "No tengo una propuesta de compensación vigente. "
            "Pedime que evalúe tu presupuesto."
        )
        assert result.service_invoked == "conversation"
        assert result.intent == intent
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_context_clears_pending(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            compensation_flow_patches(
                awaiting_compensation=True,
                pending_compensation=pending,
                llm={"intent": "reset_context", "reply_text": "Reseteando."},
            ) as mocks,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await process_incoming_message("12345", "olvidá todo")

        assert result.intent == "reset_context"
        assert "arrancamos de cero" in result.reply_text
        mocks["clear_state"].assert_awaited_once()
        mock_apply.assert_not_called()


class TestAutomaticCompensationProposal:
    @pytest.mark.asyncio
    async def test_exceeded_movement_auto_proposes_and_stores_pending(self):
        proposal = make_proposal()
        with (
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetService.get_status",
                return_value=BudgetStatusResult("ok", "found", budget_status()),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", proposal),
            ) as mock_build,
        ):
            reply = await _movement_budget_after_change("12345", movement())

        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que *Comida* superó su límite." in reply
        assert "💡 Podés compensarlo moviendo $500 ARS:" in reply
        assert "• Transporte: $1000 → $500 ARS" in reply
        assert "• Comida: $2000 → $2500 ARS" in reply
        assert "Respondé *confirmar compensación* o *no por ahora*." in reply
        assert mock_build.call_count == 1
        assert mock_build.call_args.kwargs == {
            "target_category": "Comida",
            "reference_date": date(2026, 9, 1),
            "currency": "ARS",
        }
        pending = await ConversationService.get_pending_compensation("12345")
        assert pending is not None
        assert pending.proposal == proposal.to_dict()

    @pytest.mark.asyncio
    async def test_auto_proposal_not_offered_when_storage_fails(self):
        with (
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetService.get_status",
                return_value=BudgetStatusResult("ok", "found", budget_status()),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", make_proposal()),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_pending_compensation",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.ConversationService.get_pending_compensation",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            reply = await _movement_budget_after_change("12345", movement())

        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que" not in reply
        assert "confirmar compensación" not in reply

    @pytest.mark.asyncio
    async def test_auto_proposal_skips_when_another_flow_pending(self):
        await ConversationService.set_state(
            "12345", ConversationState(step="awaiting_limit_data")
        )
        with (
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetService.get_status",
                return_value=BudgetStatusResult("ok", "found", budget_status()),
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", make_proposal()),
            ) as mock_build,
        ):
            reply = await _movement_budget_after_change("12345", movement())

        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que" not in reply
        mock_build.assert_not_called()
        assert await ConversationService.get_pending_compensation("12345") is None
        state = await ConversationService.get_state("12345")
        assert state.step == "awaiting_limit_data"

    @pytest.mark.asyncio
    async def test_auto_proposal_ignores_ingresos_and_annulled(self):
        with (
            patch(
                "app.services.dispatcher._user_id_by_phone",
                return_value=uuid4(),
            ),
            patch(
                "app.services.dispatcher.BudgetService.get_status",
            ) as mock_status,
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
            ) as mock_build,
        ):
            reply = await _movement_budget_after_change(
                "12345",
                movement(tipo="ingreso", category="Sueldo"),
                movement(category=None),
                None,
            )

        assert reply == ""
        mock_status.assert_not_called()
        mock_build.assert_not_called()


class TestRegistrationAutoProposal:
    @staticmethod
    def registered(movement_id, user_id):
        return MovementRegistrationResult(
            status="registered",
            message="ok",
            movement_id=movement_id,
            user_id=user_id,
        )

    @pytest.mark.asyncio
    async def test_exceeded_registration_auto_proposes_and_stores_pending(self):
        proposal = make_proposal()
        movement_id = str(uuid4())
        user_id = str(uuid4())
        register_data = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 2500,
            "currency": "ARS",
            "description": "supermercado",
            "category": "Comida",
        }
        with (
            patch(
                "app.services.dispatcher.FinanceService.register_movement_with_category",
                return_value=self.registered(movement_id, user_id),
            ),
            patch(
                "app.services.dispatcher.BudgetService.evaluate_movement",
                return_value=BudgetEvaluation(
                    status="ok",
                    message="evaluated",
                    movement_id=movement_id,
                    has_limit=True,
                    should_alert=True,
                    budget=budget_status(),
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_movement",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", proposal),
            ) as mock_build,
        ):
            reply = await _register_single_with_hint(
                "12345", "wamid.1", "gasté 2500 en supermercado",
                register_data, register_data,
            )

        assert "Registré tu egreso" in reply
        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que *Comida* superó su límite." in reply
        assert mock_build.call_count == 1
        assert mock_build.call_args.kwargs == {
            "target_category": "Comida",
            "reference_date": date(2026, 9, 1),
            "currency": "ARS",
        }
        pending = await ConversationService.get_pending_compensation("12345")
        assert pending is not None
        assert pending.proposal == proposal.to_dict()

    @pytest.mark.asyncio
    async def test_exceeded_registration_skips_when_another_flow_pending(self):
        await ConversationService.set_state(
            "12345", ConversationState(step="awaiting_limit_data")
        )
        register_data = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 2500,
            "currency": "ARS",
            "description": "supermercado",
            "category": "Comida",
        }
        with (
            patch(
                "app.services.dispatcher.FinanceService.register_movement_with_category",
                return_value=self.registered(str(uuid4()), str(uuid4())),
            ),
            patch(
                "app.services.dispatcher.BudgetService.evaluate_movement",
                return_value=BudgetEvaluation(
                    status="ok",
                    message="evaluated",
                    has_limit=True,
                    should_alert=True,
                    budget=budget_status(),
                ),
            ),
            patch(
                "app.services.dispatcher.ConversationService.set_last_movement",
                new_callable=AsyncMock,
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
            ) as mock_build,
        ):
            reply = await _register_single_with_hint(
                "12345", "wamid.2", "gasté 2500 en supermercado",
                register_data, register_data,
            )

        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que" not in reply
        mock_build.assert_not_called()
        assert await ConversationService.get_pending_compensation("12345") is None
        assert (await ConversationService.get_state("12345")).step == "awaiting_limit_data"

    @pytest.mark.asyncio
    async def test_multiop_proposes_once_for_first_exceeded_limit(self):
        proposal = make_proposal()
        user_id = str(uuid4())
        exceeded = budget_status()
        movements = [
            {"movement_type": "egreso", "amount": 1000, "currency": "ARS",
             "description": "pan", "category": "Comida"},
            {"movement_type": "egreso", "amount": 1500, "currency": "ARS",
             "description": "leche", "category": "Comida"},
        ]
        evaluations = [
            BudgetEvaluation(
                status="ok", message="evaluated", has_limit=True, budget=exceeded,
            ),
            BudgetEvaluation(
                status="ok", message="evaluated", has_limit=True, budget=exceeded,
            ),
        ]
        with (
            patch(
                "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
                side_effect=[
                    self.registered(str(uuid4()), user_id),
                    self.registered(str(uuid4()), user_id),
                ],
            ),
            patch(
                "app.services.dispatcher.BudgetService.evaluate_movements",
                return_value=evaluations,
            ),
            patch(
                "app.services.dispatcher.BudgetCompensationService.build_proposal",
                return_value=CompensationProposalResult("ok", "created", proposal),
            ) as mock_build,
        ):
            reply = await _register_multiop(
                "12345", "wamid.multi", "compré pan y leche", {}, movements,
            )

        assert "Registré los 2 movimientos." in reply
        assert "Superaste el límite en $500,00 ARS." in reply
        assert "Detecté que *Comida* superó su límite." in reply
        assert mock_build.call_count == 1
        pending = await ConversationService.get_pending_compensation("12345")
        assert pending is not None
        assert pending.proposal == proposal.to_dict()


class TestConfiguredCompensationActions:
    @pytest.mark.asyncio
    async def test_confirm_action_applies_pending_proposal(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            patch(
                "app.services.dispatcher.ConversationService.get_pending_compensation",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                return_value=CompensationApplyResult(
                    "applied", "compensation applied", proposal
                ),
            ) as mock_apply,
        ):
            result = await _handle_configured_action("12345", "confirm_compensation")

        assert result.service_invoked == "compensation"
        assert "Comida: $2000 → $2500 ARS" in result.reply_text
        assert "Transporte: $1000 → $500 ARS" in result.reply_text
        assert "No se modificó ningún movimiento" in result.reply_text
        mock_apply.assert_called_once_with(proposal.to_dict())
        mock_clear.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_confirm_action_with_stale_proposal_asks_for_new_calculation(self):
        proposal = make_proposal()
        pending = PendingCompensation(sender_phone="12345", proposal=proposal.to_dict())
        with (
            patch(
                "app.services.dispatcher.ConversationService.get_pending_compensation",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
                return_value=CompensationApplyResult("stale", "limits changed"),
            ),
        ):
            result = await _handle_configured_action("12345", "confirm_compensation")

        assert result.reply_text == (
            "Los saldos cambiaron desde que armé la propuesta. "
            "Pedime un nuevo cálculo."
        )
        mock_clear.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_confirm_action_without_pending_proposal(self):
        with (
            patch(
                "app.services.dispatcher.ConversationService.get_pending_compensation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await _handle_configured_action("12345", "confirm_compensation")

        assert result.reply_text == (
            "No encontré una propuesta de compensación pendiente."
        )
        assert result.service_invoked == "conversation_flow"
        mock_apply.assert_not_called()
        mock_clear.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_action_clears_without_writing(self):
        with (
            patch(
                "app.services.dispatcher.ConversationService.clear_state",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "app.services.dispatcher.BudgetCompensationService.apply",
            ) as mock_apply,
        ):
            result = await _handle_configured_action("12345", "reject_compensation")

        assert result.reply_text == "Listo, no hice ningún cambio."
        assert result.service_invoked == "conversation_flow"
        mock_apply.assert_not_called()
        mock_clear.assert_awaited_once_with("12345")
