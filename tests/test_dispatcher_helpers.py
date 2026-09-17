"""Unit tests for the pure reply/formatting helpers in the dispatcher."""

from decimal import Decimal


from app.services.dispatcher import (
    _category_confirmation_reply,
    _category_deleted_reply,
    _category_hint_reply,
    _category_not_found_reply,
    _dashboard_link_reply,
    _extract_concept_from_text,
    _format_amount,
    _format_categories_list,
    _is_create_reminder,
    _is_financial_movement,
    _movement_description,
    _onboarding_invitation_reply,
    _registered_reply,
    _registration_reply,
    _reminder_creation_reply,
    _reminder_delete_reply,
    _reminder_list_reply,
    _reminder_state_reply,
    _reminder_update_reply,
    _safe_non_stk35_reply,
    _validate_reminder_concept,
)
from app.services.finance import MovementRegistrationResult
from app.services.reminder import ReminderListResult, ReminderResult


class TestIsFinancialMovement:
    def test_expense_intent_is_financial(self):
        assert _is_financial_movement({"intent": "expense"}) is True

    def test_other_intents_are_not_financial(self):
        for intent in ("greeting", "out_of_scope", "reminder", "budget_query", "create_reminder"):
            assert _is_financial_movement({"intent": intent}) is False


class TestIsCreateReminder:
    def test_create_reminder_intent(self):
        assert _is_create_reminder({"intent": "create_reminder"}) is True

    def test_other_intents(self):
        assert _is_create_reminder({"intent": "greeting"}) is False


class TestMovementDescription:
    def test_prefers_description(self):
        data = {"description": "desc", "expense": "exp"}
        assert _movement_description(data) == "desc"

    def test_falls_back_to_expense(self):
        assert _movement_description({"expense": "exp"}) == "exp"

    def test_default_fallback(self):
        assert _movement_description({}) == "movimiento"


class TestFormatAmount:
    def test_integer_amount(self):
        assert _format_amount(5000) == "5000"

    def test_decimal_whole_number(self):
        assert _format_amount(Decimal("5000.00")) == "5000"

    def test_decimal_fractional(self):
        assert _format_amount(Decimal("1234.50")) == "1234.5"

    def test_string_amount(self):
        assert _format_amount("2500") == "2500"

    def test_invalid_amount_returns_string(self):
        assert _format_amount("abc") == "abc"

    def test_none_amount(self):
        assert _format_amount(None) == "None"


class TestRegisteredReply:
    def test_egreso(self):
        data = {
            "movement_type": "egreso",
            "description": "supermercado",
            "amount": 5000,
            "currency": "ars",
        }
        reply = _registered_reply(data)
        assert "✅" in reply
        assert "egreso" in reply
        assert "supermercado" in reply
        assert "$5000 ARS" in reply

    def test_ingreso(self):
        data = {
            "movement_type": "ingreso",
            "description": "sueldo",
            "amount": 250000,
            "currency": "ARS",
        }
        reply = _registered_reply(data)
        assert "ingreso: sueldo" in reply
        assert "$250000 ARS" in reply

    def test_default_movement_type(self):
        reply = _registered_reply({"amount": 10, "currency": "ARS"})
        assert "tu movimiento" in reply


class TestCategoryReplies:
    def test_hint(self):
        assert "categoría" in _category_hint_reply()

    def test_confirmation(self):
        reply = _category_confirmation_reply("Comida")
        assert "Comida" in reply
        assert "confirmar" in reply.lower()

    def test_deleted(self):
        reply = _category_deleted_reply("Comida")
        assert "Comida" in reply
        assert "eliminada" in reply

    def test_not_found(self):
        reply = _category_not_found_reply("Comida")
        assert "Comida" in reply


class TestRegistrationReply:
    def make_result(self, status):
        return MovementRegistrationResult(status=status, message=status)

    def test_registered_uses_registered_reply(self):
        data = {"movement_type": "egreso", "description": "x", "amount": 1, "currency": "ARS"}
        reply = _registration_reply(self.make_result("registered"), data)
        assert reply.startswith("✅")

    def test_duplicate(self):
        reply = _registration_reply(self.make_result("duplicate"), {})
        assert "ya hab" in reply.lower() or "dupli" in reply.lower()

    def test_user_not_found(self):
        reply = _registration_reply(self.make_result("user_not_found"), {})
        assert "cuenta vinculada" in reply

    def test_invalid_data(self):
        reply = _registration_reply(self.make_result("invalid_data"), {})
        assert "monto" in reply

    def test_persistence_error(self):
        reply = _registration_reply(self.make_result("persistence_error"), {})
        assert "problema" in reply

    def test_not_a_movement(self):
        reply = _registration_reply(self.make_result("not_a_movement"), {})
        assert "movimiento financiero" in reply

    def test_unknown_status_falls_back_to_llm(self):
        reply = _registration_reply(self.make_result("weird"), {"reply_text": "fallback"})
        assert reply == "fallback"

    def test_unknown_status_empty_llm(self):
        reply = _registration_reply(self.make_result("weird"), {})
        assert "No pude interpretar" in reply


class TestSafeNonStk35Reply:
    def test_returns_reply_text(self):
        assert _safe_non_stk35_reply({"intent": "greeting", "reply_text": "hola"}) == "hola"

    def test_handles_stk39_intents(self):
        for intent in ("confirm_category", "reject_category", "delete_category", "list_categories"):
            assert _safe_non_stk35_reply({"intent": intent, "reply_text": "x"}) == "x"

    def test_empty_reply_uses_fallback(self):
        reply = _safe_non_stk35_reply({"intent": "greeting", "reply_text": ""})
        assert "reformular" in reply

    def test_none_reply_uses_fallback(self):
        reply = _safe_non_stk35_reply({"intent": "greeting"})
        assert "reformular" in reply


class TestReminderCreationReply:
    def make_result(self, status, message="ok"):
        return ReminderResult(status=status, message=message)

    def test_created_with_amount(self):
        data = {
            "reminder_concept": "luz",
            "reminder_day": 15,
            "reminder_amount": 5000,
            "reminder_currency": "ARS",
        }
        reply = _reminder_creation_reply(self.make_result("created"), data)
        assert "luz" in reply
        assert "15" in reply
        assert "$5000" in reply

    def test_created_without_amount(self):
        data = {"reminder_concept": "luz", "reminder_day": 15, "reminder_amount": None}
        reply = _reminder_creation_reply(self.make_result("created"), data)
        assert "luz" in reply
        assert "$5000" not in reply

    def test_duplicate_title(self):
        reply = _reminder_creation_reply(self.make_result("duplicate_title", "Ya existe"), {})
        assert reply == "Ya existe"

    def test_user_not_found(self):
        reply = _reminder_creation_reply(self.make_result("user_not_found"), {})
        assert "WhatsApp" in reply

    def test_invalid_data(self):
        reply = _reminder_creation_reply(self.make_result("invalid_data", "Datos mal"), {})
        assert reply == "Datos mal"

    def test_persistence_error(self):
        reply = _reminder_creation_reply(self.make_result("persistence_error"), {})
        assert "Intentá" in reply

    def test_unknown_status(self):
        reply = _reminder_creation_reply(self.make_result("weird"), {})
        assert "recordatorio" in reply.lower()


class TestReminderListReply:
    def test_empty(self):
        result = ReminderListResult(status="ok", message="ok", reminders=[])
        assert "No tenés recordatorios" in _reminder_list_reply(result)

    def test_with_reminders(self):
        result = ReminderListResult(
            status="ok",
            message="ok",
            reminders=[
                {"titulo": "Luz", "dia_del_mes": 15, "monto": 5000, "moneda": "ARS", "estado": "activo"},
                {"titulo": "Cable", "dia_del_mes": 10, "monto": None, "moneda": "ARS", "estado": "pausado"},
            ],
        )
        reply = _reminder_list_reply(result)
        assert "Luz" in reply
        assert "$5000" in reply
        assert "Cable" in reply
        assert "⏸" in reply


class TestReminderUpdateReply:
    def make_result(self, status, message="ok"):
        return ReminderResult(status=status, message=message)

    def test_updated(self):
        assert "actualicé" in _reminder_update_reply(self.make_result("updated"))

    def test_user_not_found(self):
        assert "WhatsApp" in _reminder_update_reply(self.make_result("user_not_found"))

    def test_not_found(self):
        assert "Chequeá" in _reminder_update_reply(self.make_result("not_found"))

    def test_not_owned(self):
        assert "Chequeá" in _reminder_update_reply(self.make_result("not_owned"))

    def test_invalid_data(self):
        assert _reminder_update_reply(self.make_result("invalid_data", "mal")) == "mal"

    def test_persistence_error(self):
        assert "Intentá" in _reminder_update_reply(self.make_result("persistence_error"))

    def test_unknown(self):
        assert "edición" in _reminder_update_reply(self.make_result("weird"))


class TestReminderStateReply:
    def make_result(self, status, message="ok"):
        return ReminderResult(status=status, message=message)

    def test_paused(self):
        reply = _reminder_state_reply(self.make_result("paused"), "paused")
        assert "pausé" in reply

    def test_activated(self):
        reply = _reminder_state_reply(self.make_result("activated"), "activated")
        assert "reactiv" in reply

    def test_user_not_found(self):
        assert "WhatsApp" in _reminder_state_reply(self.make_result("user_not_found"), "paused")

    def test_not_found(self):
        assert "Chequeá" in _reminder_state_reply(self.make_result("not_found"), "paused")

    def test_invalid_data(self):
        assert _reminder_state_reply(self.make_result("invalid_data", "mal"), "paused") == "mal"

    def test_persistence_error(self):
        assert "Intentá" in _reminder_state_reply(self.make_result("persistence_error"), "paused")

    def test_unknown(self):
        assert "estado" in _reminder_state_reply(self.make_result("weird"), "paused")


class TestReminderDeleteReply:
    def make_result(self, status, message="ok"):
        return ReminderResult(status=status, message=message)

    def test_deleted(self):
        assert "eliminé" in _reminder_delete_reply(self.make_result("deleted"))

    def test_user_not_found(self):
        assert "WhatsApp" in _reminder_delete_reply(self.make_result("user_not_found"))

    def test_not_found(self):
        assert "Chequeá" in _reminder_delete_reply(self.make_result("not_found"))

    def test_invalid_data(self):
        assert _reminder_delete_reply(self.make_result("invalid_data", "mal")) == "mal"

    def test_persistence_error(self):
        assert "Intentá" in _reminder_delete_reply(self.make_result("persistence_error"))

    def test_unknown(self):
        assert "eliminación" in _reminder_delete_reply(self.make_result("weird"))


class TestInvitationAndDashboardReplies:
    def test_onboarding_invitation(self):
        reply = _onboarding_invitation_reply("https://example.com/r", 30)
        assert "https://example.com/r" in reply
        assert "30 minutos" in reply

    def test_dashboard_link(self):
        reply = _dashboard_link_reply("https://example.com/login", 15)
        assert "https://example.com/login" in reply
        assert "15 minutos" in reply


class TestConceptExtraction:
    def test_avisame_extracts_concept(self):
        assert _extract_concept_from_text("avisame del cable") == "cable"

    def test_recordatorio_extracts_concept(self):
        assert _extract_concept_from_text("recordatorio de luz") == "luz"

    def test_no_match_returns_none(self):
        assert _extract_concept_from_text("hola") is None

    def test_validate_uses_llm_concept(self):
        assert _validate_reminder_concept("luz", "avisame de la luz") == "luz"

    def test_validate_falls_back_to_text(self):
        assert _validate_reminder_concept(None, "avisame del cable") == "cable"

    def test_validate_rejects_sentence(self):
        assert _validate_reminder_concept("pagar la factura de luz mañana temprano", "avisame del cable") == "cable"

    def test_validate_rejects_verb_concept(self):
        assert _validate_reminder_concept("pagar luz", "avisame del cable") == "cable"

    def test_validate_rejects_long_concept_over_32_chars(self):
        long_concept = "x" * 40
        assert _validate_reminder_concept(long_concept, "avisame del cable") == "cable"


class TestFormatCategoriesList:
    def test_empty(self):
        class FakeResult:
            categories = []

        assert "No tenés categorías" in _format_categories_list(FakeResult())

    def test_with_categories(self):
        class FakeCategory:
            category_name = "Comida"
            es_default = False
            total_ingresos = Decimal("0")
            total_egresos = Decimal("5000")

        class FakeResult:
            categories = [FakeCategory()]

        reply = _format_categories_list(FakeResult())
        assert "Comida" in reply
        assert "$5000" in reply

    def test_default_tag(self):
        class FakeCategory:
            category_name = "Comida"
            es_default = True
            total_ingresos = Decimal("0")
            total_egresos = Decimal("0")

        class FakeResult:
            categories = [FakeCategory()]

        assert "por defecto" in _format_categories_list(FakeResult())
