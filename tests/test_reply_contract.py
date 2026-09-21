"""Contrato: en intents persistidos, el reply_text del LLM nunca llega al usuario.

La verificación la genera el backend (dispatcher) tras persistir la operación.
Las frases de ejemplo del prompt nunca se muestran como confirmación de escritura.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.services.dispatcher import process_incoming_message
from app.services.finance import (
    CategoriesListResult,
    CategoryResult,
    CategoryWithTotals,
    MovementRegistrationResult,
)
from app.services.onboarding import OnboardingDecision, OnboardingResult
from app.services.reminder import ReminderListResult, ReminderResult

LLM_CANNED_REPLY = "Estoy procesando el movimiento."


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


class _FakeUser:
    id = "u1"


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return _FakeUser()


class _FakeSession:
    def query(self, *a, **k):
        return _FakeQuery()

    def close(self):
        pass


def _fake_session():
    """Los handlers de categorías/listados consultan SessionLocal directamente."""
    return patch("app.models.database.SessionLocal", lambda: _FakeSession())


def known_user():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def _llm_result(intent, **overrides):
    result = {
        "intent": intent,
        "movement_type": None,
        "amount": None,
        "currency": "ARS",
        "category": None,
        "description": None,
        "expense": None,
        "reply_text": LLM_CANNED_REPLY,
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


def _registered():
    return MovementRegistrationResult(status="registered", message="ok", movement_id="m1", user_id="u1")


def _no_redis():
    return AsyncMock(return_value=False)


async def _dispatch(llm_result, patches):
    managers = [
        patch("app.services.dispatcher._update_ultimo_mensaje", lambda phone: None),
        patch("app.services.onboarding.OnboardingService.prepare_whatsapp_message", lambda phone: known_user()),
        patch("app.services.dispatcher.ConversationService.is_awaiting_rename", _no_redis()),
        patch("app.services.dispatcher.ConversationService.is_awaiting_reminder_data", _no_redis()),
        patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_result)),
        *patches,
    ]
    with ExitStack() as stack:
        for manager in managers:
            stack.enter_context(manager)
        return await process_incoming_message("5491100000001", "mensaje de prueba", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent, overrides, patches",
    [
        (
            "expense",
            {"movement_type": "egreso", "amount": 5000, "description": "supermercado", "expense": "supermercado", "category": "supermercado"},
            [
                patch("app.services.dispatcher.FinanceService.register_movement_with_category", return_value=_registered()),
                patch("app.services.dispatcher.ConversationService.set_last_movement", AsyncMock()),
            ],
        ),
        (
            "expense",
            {"movement_type": "egreso", "amount": 5000, "description": "supermercado", "expense": "supermercado", "category": None},
            [
                patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text", return_value=_registered()),
                patch("app.services.dispatcher.ConversationService.set_last_movement", AsyncMock()),
            ],
        ),
        (
            "create_reminder",
            {"reminder_concept": "luz", "reminder_day": 15},
            [patch("app.services.dispatcher.ReminderService.create_reminder", return_value=ReminderResult(status="created", message="ok"))],
        ),
        (
            "pause_reminder",
            {"reminder_concept": "luz"},
            [patch("app.services.dispatcher.ReminderService.pause_by_title", return_value=ReminderResult(status="paused", message="ok"))],
        ),
        (
            "activate_reminder",
            {"reminder_concept": "wifi"},
            [patch("app.services.dispatcher.ReminderService.activate_by_title", return_value=ReminderResult(status="activated", message="ok"))],
        ),
        (
            "delete_reminder",
            {"reminder_concept": "luz"},
            [patch("app.services.dispatcher.ReminderService.delete_by_title", return_value=ReminderResult(status="deleted", message="ok"))],
        ),
        (
            "update_reminder",
            {"reminder_concept": "luz", "reminder_day": 10},
            [
                patch("app.services.dispatcher.ReminderService.find_by_title", return_value=None),
                patch("app.services.dispatcher.ReminderService.update_reminder", return_value=ReminderResult(status="updated", message="ok")),
            ],
        ),
        (
            "list_reminders",
            {},
            [
                _fake_session(),
                patch(
                    "app.services.dispatcher.ReminderService.list_reminders_all",
                    return_value=ReminderListResult(status="ok", message="ok", reminders=[{"titulo": "luz", "dia_del_mes": 15, "monto": None, "moneda": "ARS", "estado": "activo"}]),
                ),
            ],
        ),
        (
            "delete_category",
            {"category": "servicios"},
            [
                _fake_session(),
                patch(
                    "app.services.dispatcher.FinanceService.delete_category",
                    return_value=CategoryResult(status="deleted", message="ok", category_name="servicios"),
                ),
            ],
        ),
        (
            "list_categories",
            {},
            [
                _fake_session(),
                patch(
                    "app.services.dispatcher.FinanceService.get_categories_with_totals",
                    return_value=CategoriesListResult(
                        status="ok",
                        message="ok",
                        categories=[CategoryWithTotals(category_id="c1", category_name="servicios", es_default=False)],
                    ),
                ),
            ],
        ),
    ],
)
async def test_llm_reply_text_jamas_llega_al_usuario_en_intents_persistidos(intent, overrides, patches):
    result = await _dispatch(_llm_result(intent, **overrides), patches)

    assert result.reply_text
    assert LLM_CANNED_REPLY not in result.reply_text, (
        f"intent={intent}: el reply_text del LLM se filtró al usuario: {result.reply_text!r}"
    )
