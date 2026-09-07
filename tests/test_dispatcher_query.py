import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services.dispatcher import (
    _format_query_movements_reply,
    _handle_query_movements,
    process_incoming_message,
)
from app.services.finance import MovementItem, MovementQueryResult
from app.services.onboarding import OnboardingDecision, OnboardingResult


def make_movement(
    tipo="egreso",
    cantidad=Decimal("1500.50"),
    moneda="ARS",
    descripcion="Supermercado",
    fecha=date(2026, 9, 5),
    categoria="Comida",
):
    return MovementItem(
        id=str(uuid.uuid4()),
        tipo=tipo,
        cantidad=cantidad,
        moneda=moneda,
        descripcion=descripcion,
        fecha_movimiento=fecha,
        categoria_nombre=categoria,
    )


class TestFormatQueryMovementsReply:
    def test_format_empty_movements(self):
        res = MovementQueryResult(status="ok", message="ok", movements=[], total_found=0)
        text = _format_query_movements_reply(res, {})
        assert "No tenés movimientos registrados todavía" in text

        text_egreso = _format_query_movements_reply(res, {"movement_type": "egreso"})
        assert "No encontré gastos registrados" in text_egreso

        text_ingreso = _format_query_movements_reply(res, {"movement_type": "ingreso"})
        assert "No encontré ingresos registrados" in text_ingreso

        text_cat = _format_query_movements_reply(res, {"category_name": "Comida"})
        assert "No encontré movimientos registrados en la categoría *Comida*" in text_cat

    def test_format_movements_with_correct_signs_and_data(self):
        m1 = make_movement(tipo="egreso", cantidad=Decimal("1500"), descripcion="Super", categoria="Comida")
        m2 = make_movement(tipo="ingreso", cantidad=Decimal("50000"), descripcion="Sueldo", categoria=None)
        res = MovementQueryResult(status="ok", message="ok", movements=[m1, m2], total_found=2)

        text = _format_query_movements_reply(res, {})
        assert "📋 *Tus últimos movimientos:*" in text
        assert "• 05/09/2026 - Super: -$1500 ARS (Comida)" in text
        assert "• 05/09/2026 - Sueldo: +$50000 ARS" in text
        assert "Mostrando" not in text

    def test_format_movements_with_footer_when_more_found(self):
        movements = [make_movement(descripcion=f"Gasto {i}") for i in range(5)]
        res = MovementQueryResult(status="ok", message="ok", movements=movements, total_found=12)

        text = _format_query_movements_reply(res, {"movement_type": "egreso"})
        assert "📋 *Tus últimos gastos:*" in text
        assert "Mostrando los últimos 5 de 12 movimientos." in text

    def test_format_user_not_found_and_errors(self):
        res_user = MovementQueryResult(status="user_not_found", message="not found")
        assert "No encontré una cuenta" in _format_query_movements_reply(res_user, {})

        res_inv = MovementQueryResult(status="invalid_filters", message="fecha inválida")
        assert "No pude realizar la consulta: fecha inválida." in _format_query_movements_reply(res_inv, {})

    def test_format_movements_defensively_slices_to_five(self):
        movements = [make_movement(descripcion=f"Gasto {i}") for i in range(8)]
        res = MovementQueryResult(status="ok", message="ok", movements=movements, total_found=8)
        text = _format_query_movements_reply(res, {})
        # Only first 5 should appear in bullet list
        assert "Gasto 4" in text
        assert "Gasto 5" not in text
        assert "Mostrando los últimos 5 de 8 movimientos." in text


class TestHandleQueryMovements:
    @pytest.mark.asyncio
    async def test_handle_query_movements_user_not_found(self):
        with patch("app.services.dispatcher._user_id_by_phone", return_value=None):
            reply = await _handle_query_movements("5491100000000", {})
            assert "No encontré una cuenta vinculada a este WhatsApp." in reply

    @pytest.mark.asyncio
    async def test_handle_query_movements_returns_clarification_if_ambiguous(self):
        extracted = {
            "reply_text": "¿Querés consultar tus últimos gastos, tus ingresos o todos los movimientos?",
            "movement_type": None,
            "category": None,
            "date_from": None,
            "date_to": None,
        }
        with patch("app.services.dispatcher._user_id_by_phone", return_value=uuid.uuid4()):
            reply = await _handle_query_movements("5491100000001", extracted)
            assert reply == "¿Querés consultar tus últimos gastos, tus ingresos o todos los movimientos?"

    @pytest.mark.asyncio
    async def test_handle_query_movements_executes_query(self):
        u_id = uuid.uuid4()
        m1 = make_movement(tipo="egreso", cantidad=Decimal("3500"), descripcion="Nafta", categoria="Transporte")
        fake_result = MovementQueryResult(status="ok", message="ok", movements=[m1], total_found=1)

        with (
            patch("app.services.dispatcher._user_id_by_phone", return_value=u_id),
            patch("app.services.dispatcher.FinanceService.query_movements", return_value=fake_result) as mock_query,
        ):
            extracted = {
                "movement_type": "egreso",
                "category": "Transporte",
                "date_from": "2026-09-01",
                "date_to": "2026-09-07",
                "limit": 5,
                "reply_text": "Consultando tus movimientos.",
            }
            reply = await _handle_query_movements("5491100000001", extracted)

            mock_query.assert_called_once_with(
                u_id,
                movement_type="egreso",
                category_name="Transporte",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 7),
                limit=5,
            )
            assert "📋 *Tus últimos gastos en Transporte:*" in reply
            assert "-$3500 ARS" in reply

    @pytest.mark.asyncio
    async def test_handle_query_movements_invalid_start_date(self):
        u_id = uuid.uuid4()
        with (
            patch("app.services.dispatcher._user_id_by_phone", return_value=u_id),
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_query,
        ):
            extracted = {
                "movement_type": "egreso",
                "date_from": "not-a-date",
            }
            reply = await _handle_query_movements("5491100000001", extracted)
            assert "No pude interpretar la fecha inicial de la consulta." in reply
            mock_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_query_movements_invalid_end_date(self):
        u_id = uuid.uuid4()
        with (
            patch("app.services.dispatcher._user_id_by_phone", return_value=u_id),
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_query,
        ):
            extracted = {
                "movement_type": "egreso",
                "date_from": "2026-09-01",
                "date_to": "invalido",
            }
            reply = await _handle_query_movements("5491100000001", extracted)
            assert "No pude interpretar la fecha final de la consulta." in reply
            mock_query.assert_not_called()


class TestDispatcherQueryIntegration:
    @pytest.fixture(autouse=True)
    def no_pending_conversation_flows(self, monkeypatch):
        for method in (
            "is_awaiting_rename",
            "is_awaiting_reminder_data",
            "is_awaiting_limit_year_confirmation",
            "is_awaiting_limit_category_confirmation",
            "is_awaiting_limit_data",
            "is_awaiting_limit_delete_category",
            "is_awaiting_limit_month_selection",
        ):
            monkeypatch.setattr(
                f"app.services.dispatcher.ConversationService.{method}",
                AsyncMock(return_value=False),
            )

    @pytest.mark.asyncio
    async def test_process_incoming_message_routes_query_movements(self):
        u_id = uuid.uuid4()
        m1 = make_movement(tipo="egreso", cantidad=Decimal("2000"), descripcion="Cena")
        fake_result = MovementQueryResult(status="ok", message="ok", movements=[m1], total_found=1)

        llm_output = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
            "reply_text": "Consultando tus movimientos.",
        }

        with (
            patch("app.services.dispatcher._update_ultimo_mensaje"),
            patch(
                "app.services.onboarding.OnboardingService.prepare_whatsapp_message",
                return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
            ),
            patch(
                "app.services.dispatcher.LLMService.process_message",
                AsyncMock(return_value=llm_output),
            ),
            patch("app.services.dispatcher._user_id_by_phone", return_value=u_id),
            patch("app.services.dispatcher.FinanceService.query_movements", return_value=fake_result),
            patch(
                "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
            ) as mock_register,
        ):
            res = await process_incoming_message("5491100000001", "mis ultimos gastos", "wamid.123")

            assert res.service_invoked == "finance"
            assert res.intent == "query_movements"
            assert "📋 *Tus últimos gastos:*" in res.reply_text
            assert "Cena" in res.reply_text
            # Muy importante: la consulta no debe registrar movimientos
            mock_register.assert_not_called()
