from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.database import Billetera, MovimientoFinanciero, Usuario
from app.api.whatsapp import WhatsAppText
from app.services.conversation import ConversationService, PendingMovement, RecentItems
from app.services.dispatcher import _resume_movement_after_category, process_incoming_message
from app.services.movement_dates import argentina_today, interpret_date
from app.services.onboarding import OnboardingDecision, OnboardingResult

PHONE = "5491100000047"


@pytest.fixture
def account(isolated_db):
    user_id, wallet_id = uuid4(), uuid4()
    with isolated_db() as session:
        session.add(Usuario(id=user_id, nombre="Test", email="stk247@example.com", whatsapp_id=PHONE))
        session.add(Billetera(id=wallet_id, usuario_id=user_id, nombre="ARS", moneda="ARS"))
        session.commit()
    return isolated_db, user_id, wallet_id


def payload(kind="egreso", fecha=None):
    return {"intent": "expense", "movement_type": kind, "amount": 12,
            "currency": "ARS", "description": "prueba fecha", "category": None,
            "fecha": fecha, "reply_text": ""}


async def send(text, extracted, message_id):
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
              return_value=OnboardingResult(OnboardingDecision.KNOWN_USER)),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock,
              return_value=extracted),
    ):
        return await process_incoming_message(PHONE, text, message_id)


def movements(factory):
    with factory() as session:
        return session.query(MovimientoFinanciero).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["egreso", "ingreso"])
@pytest.mark.parametrize("model_date", [None, "2026-10-16"])
async def test_explicit_date_overrides_omitted_or_wrong_model_date(account, kind, model_date):
    factory, _, _ = account
    result = await send(f"Registrá {kind} 12 por prueba fecha el 16/09/2026", payload(kind, model_date), "original")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(2026, 9, 16)


@pytest.mark.asyncio
async def test_omitted_year_uses_current_year_and_shows_it(account):
    factory, _, _ = account
    today = argentina_today()
    text = "Registrá un gasto de 12 por prueba fecha el 01/01"
    result = await send(text, payload(fecha="2000-01-01"), "no-year")
    assert f"01/01/{today.year}" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(today.year, 1, 1)


@pytest.mark.asyncio
async def test_configured_confirmation_also_shows_interpreted_year(account):
    factory, _, _ = account
    with patch("app.services.dispatcher.ConversationFlowRuntime.render_event", new_callable=AsyncMock,
               return_value=WhatsAppText(body="Movimiento registrado.")):
        result = await send("Gasté 12 por prueba fecha el 01/01", payload(), "configured")
    assert f"01/01/{argentina_today().year}" in result.reply_message.body
    assert movements(factory)[0].fecha_movimiento == date(argentina_today().year, 1, 1)


@pytest.mark.asyncio
async def test_future_requires_confirmation_before_write_and_is_not_repeated(account):
    factory, _, _ = account
    future = argentina_today() + timedelta(days=20)
    result = await send(f"Registrá un gasto de 12 por prueba fecha el {future:%d/%m/%Y}", payload(), "future")
    assert "futura" in result.reply_text and not movements(factory)
    result = await send("quizás", {}, "ambiguous")
    assert "futura" in result.reply_text and not movements(factory)
    result = await send("sí", {}, "confirmed")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == future
    result = await send("sí", {}, "confirmed-again")
    assert "pendiente" in result.reply_text and len(movements(factory)) == 1


@pytest.mark.asyncio
async def test_invalid_can_be_corrected_or_cancelled_without_writes(account):
    factory, _, _ = account
    result = await send("Registrá un gasto de 12 por prueba fecha el 31/02/2026", payload(), "bad")
    assert "fecha válida" in result.reply_text and not movements(factory)
    result = await send("16/09/2026", {}, "corrected")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(2026, 9, 16)
    result = await send("Registrá un ingreso de 12 el 31/02/2026", payload("ingreso"), "cancel")
    assert "fecha válida" in result.reply_text
    result = await send("cancelar", {}, "cancel-answer")
    assert "cancelé" in result.reply_text and len(movements(factory)) == 1


@pytest.mark.asyncio
async def test_redis_failure_never_saves_a_future_movement(account):
    factory, _, _ = account
    future = argentina_today() + timedelta(days=10)
    with patch.object(ConversationService, "save_pending_date", new_callable=AsyncMock, return_value=False):
        result = await send(f"Gasté 12 el {future:%d/%m/%Y}", payload(), "redis-down")
    assert "contexto" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_update_date_preserves_other_fields_and_confirms_real_date(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    await ConversationService.set_recent_items(PHONE, RecentItems("movement", [{
        "id": str(movement_id), "description": "taxi", "label": "taxi",
        "amount": "123", "currency": "ARS", "date": "2026-10-16"}]))
    data = {"intent": "update_movement", "reference": "last_registered",
            "changes": {"fecha": "2026-10-16"}}
    result = await send("era de 16/09/2026", data, "edit")
    assert "16/09/2026" in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad, updated.moneda, updated.descripcion, updated.billetera_id) == (
        date(2026, 9, 16), Decimal("123"), "ARS", "taxi", wallet_id)


@pytest.mark.asyncio
async def test_future_update_rechecks_snapshot_before_mutating(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 9, 16)))
        session.commit()
    await ConversationService.set_recent_items(PHONE, RecentItems("movement", [{
        "id": str(movement_id), "description": "taxi", "label": "taxi",
        "amount": "123", "currency": "ARS", "date": "2026-09-16"}]))
    future = argentina_today() + timedelta(days=15)
    data = {"intent": "update_movement", "reference": "last_registered", "changes": {"fecha": future.isoformat()}}
    result = await send(f"Cambiá la fecha de ese movimiento a {future:%d/%m/%Y}", data, "future-edit")
    assert "futura" in result.reply_text
    with factory() as session:
        session.get(MovimientoFinanciero, movement_id).cantidad = Decimal("200")
        session.commit()
    result = await send("sí", {}, "future-edit-yes")
    assert "cambió" in result.reply_text.lower()
    assert movements(factory)[0].fecha_movimiento == date(2026, 9, 16)


@pytest.mark.asyncio
async def test_future_update_applies_only_after_confirmation(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="ingreso", cantidad=Decimal("123"), moneda="ARS", descripcion="sueldo",
                    fecha_movimiento=date(2026, 9, 16)))
        session.commit()
    await ConversationService.set_recent_items(PHONE, RecentItems("movement", [{
        "id": str(movement_id), "description": "sueldo", "label": "sueldo",
        "amount": "123", "currency": "ARS", "date": "2026-09-16"}]))
    future = argentina_today() + timedelta(days=15)
    data = {"intent": "update_movement", "reference": "last_registered", "changes": {"fecha": future.isoformat()}}
    result = await send(f"Cambiá la fecha de ese movimiento a {future:%d/%m/%Y}", data, "confirm-edit")
    assert "futura" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(2026, 9, 16)
    result = await send("sí", {}, "confirm-edit-yes")
    assert future.strftime("%d/%m/%Y") in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad, updated.billetera_id) == (future, Decimal("123"), wallet_id)


@pytest.mark.asyncio
async def test_wallet_choice_preserves_and_shows_interpreted_date(account):
    factory, user_id, _ = account
    with factory() as session:
        session.add(Billetera(id=uuid4(), usuario_id=user_id, nombre="Otro", moneda="ARS"))
        session.commit()
    result = await send("Gasté 12 por prueba fecha el 01/01", payload(fecha="2000-01-01"), "wallet")
    assert "billetera" in result.reply_text and not movements(factory)
    result = await send("2", {}, "wallet-selected")
    assert f"01/01/{argentina_today().year}" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(argentina_today().year, 1, 1)


@pytest.mark.asyncio
async def test_confirmed_future_survives_wallet_and_category_selection(account):
    factory, user_id, _ = account
    with factory() as session:
        session.add(Billetera(id=uuid4(), usuario_id=user_id, nombre="Otro", moneda="ARS"))
        session.commit()
    future = argentina_today() + timedelta(days=20)
    data = payload(fecha=future.isoformat())
    data["category"] = "Parapente"
    result = await send(f"Gasté 12 en parapente el {future:%d/%m/%Y}", data, "future-wallet")
    assert "futura" in result.reply_text and not movements(factory)
    result = await send("sí", {}, "future-yes")
    assert "billetera" in result.reply_text and not movements(factory)
    result = await send("2", {}, "future-wallet-2")
    assert "crearla" in result.reply_text and not movements(factory)
    result = await send("sí", {"intent": "confirm_category"}, "future-category-yes")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == future


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["wallet", "category"])
async def test_old_pending_future_without_confirmation_never_writes(account, step):
    factory, _, wallet_id = account
    pending = PendingMovement(
        sender_phone=PHONE, whatsapp_message_id="old-pending", original_text="gasto",
        movement_type="egreso", amount=Decimal("12"), currency="ARS", description="a",
        inferred_category="Parapente", wallet_id=str(wallet_id),
        wallet_options=[{"ordinal": "1", "wallet_id": str(wallet_id), "name": "ARS"}],
        resolved_fecha_movimiento=argentina_today() + timedelta(days=2),
    )
    if step == "wallet":
        await ConversationService.set_pending_wallet_selection(PHONE, pending)
        result = await send("1", {}, "old-wallet")
    else:
        await ConversationService.set_pending_movement(PHONE, pending)
        result = await _resume_movement_after_category(PHONE)
    assert "confirmación" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_category_confirmation_preserves_interpreted_date(account):
    factory, _, _ = account
    data = payload(fecha="2000-01-01")
    data["category"] = "Parapente"
    result = await send("Gasté 12 en parapente el 01/01", data, "category")
    assert "crearla" in result.reply_text and not movements(factory)
    result = await send("sí", {"intent": "confirm_category"}, "category-yes")
    assert f"01/01/{argentina_today().year}" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == date(argentina_today().year, 1, 1)


@pytest.mark.asyncio
async def test_multioperation_invalid_or_ambiguous_dates_do_not_partially_write(account):
    factory, _, _ = account
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": "2026-09-16"},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": None},
    ]}
    result = await send("Gasté 10 en a y 20 en b el 16/09/2026", data, "multi")
    assert "separado" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_multioperation_common_date_is_kept_for_both(account):
    factory, _, _ = account
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": "2026-09-16"},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": "2026-09-16"},
    ]}
    result = await send("Ambos movimientos del 16/09/2026: gasté 10 en a y 20 en b", data, "multi-valid")
    assert "Registré" in result.reply_text
    assert [m.fecha_movimiento for m in movements(factory)] == [date(2026, 9, 16)] * 2


@pytest.mark.asyncio
async def test_multioperation_dates_can_be_associated_with_items(account):
    factory, _, _ = account
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "taxi", "fecha": "2026-09-16"},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "cafe", "fecha": "2026-09-17"},
    ]}
    result = await send("Gasté 10 en taxi el 16/09/2026 y 20 en cafe el 17/09/2026", data, "multi-individual")
    assert "Registré" in result.reply_text
    assert [m.fecha_movimiento for m in movements(factory)] == [date(2026, 9, 16), date(2026, 9, 17)]


@pytest.mark.asyncio
async def test_multioperation_two_dates_without_clear_item_association_does_not_write(account):
    factory, _, _ = account
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "taxi", "fecha": "2026-09-16"},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "cafe", "fecha": "2026-09-17"},
    ]}
    result = await send("Gasté 10 en taxi y 20 en cafe los días 16/09/2026 y 17/09/2026", data, "multi-no-mapping")
    assert "separado" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_multioperation_future_common_date_correction_is_applied_to_both(account):
    factory, _, _ = account
    future = argentina_today() + timedelta(days=10)
    date_iso = future.isoformat()
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": date_iso},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": date_iso},
    ]}
    result = await send(f"Ambos movimientos del {future:%d/%m/%Y}: gasté 10 en a y 20 en b", data, "multi-future")
    assert "futura" in result.reply_text and not movements(factory)
    result = await send("16/09/2026", {}, "multi-corrected")
    assert "Registré" in result.reply_text
    assert [m.fecha_movimiento for m in movements(factory)] == [date(2026, 9, 16)] * 2


@pytest.mark.asyncio
async def test_missing_clarification_state_does_not_use_history_as_authority(account):
    factory, _, _ = account
    result = await send("sí", payload(), "lost")
    assert "pendiente" in result.reply_text and not movements(factory)
    result = await send("sí", {"intent": "create_reminder", "reminder_concept": "imaginario"}, "lost-again")
    assert "pendiente" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["el próximo lunes", "el lunes que viene"])
async def test_next_weekday_never_silently_becomes_a_past_weekday(account, phrase):
    factory, _, _ = account
    today = argentina_today()
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    result = await send(f"Gasté 12 por prueba fecha {phrase}", payload(fecha=next_monday.isoformat()), "next-weekday")
    assert next_monday.strftime("%d/%m/%Y") in result.reply_text
    assert not movements(factory)
    result = await send("sí", {}, "next-weekday-yes")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (next_monday, Decimal("12"))


@pytest.mark.asyncio
async def test_this_morning_is_today_not_tomorrow(account):
    factory, _, _ = account
    result = await send("Gasté 12 por prueba fecha esta mañana", payload(fecha=argentina_today().isoformat()), "morning")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == argentina_today()


@pytest.mark.asyncio
async def test_multioperation_different_relative_and_numeric_dates_never_get_same_date(account):
    factory, _, _ = account
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": "2026-09-16"},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": "2026-09-16"},
    ]}
    result = await send("Gasté 10 en a el 16/09/2026 y 20 en b hoy", data, "multi-mixed")
    assert "fecha" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase,model_date", [("ayer", None), ("el 16/09/2026", "2026-09-16")])
async def test_multioperation_single_date_is_not_assumed_common(account, phrase, model_date):
    factory, _, _ = account
    model_date = model_date or (argentina_today() - timedelta(days=1)).isoformat()
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": model_date},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": model_date},
    ]}
    result = await send(f"Gasté 10 en a {phrase} y 20 en b", data, "multi-not-common")
    assert "separado" in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_multioperation_explicit_common_relative_date_is_respected(account):
    factory, _, _ = account
    yesterday = argentina_today() - timedelta(days=1)
    data = {"intent": "expense", "movements": [
        {"movement_type": "egreso", "amount": 10, "currency": "ARS", "description": "a", "fecha": yesterday.isoformat()},
        {"movement_type": "egreso", "amount": 20, "currency": "ARS", "description": "b", "fecha": yesterday.isoformat()},
    ]}
    result = await send("Ambos movimientos de ayer: gasté 10 en a y 20 en b", data, "multi-relative-common")
    assert "Registré" in result.reply_text
    assert [m.fecha_movimiento for m in movements(factory)] == [yesterday] * 2


@pytest.mark.asyncio
async def test_tomorrow_morning_is_future_and_yesterday_morning_is_past(account):
    factory, _, _ = account
    today = argentina_today()
    result = await send("Gasté 12 por prueba fecha mañana por la mañana", payload(fecha=(today + timedelta(days=1)).isoformat()), "tomorrow")
    assert (today + timedelta(days=1)).strftime("%d/%m/%Y") in result.reply_text
    assert not movements(factory)
    result = await send("cancelar", {}, "tomorrow-cancel")
    assert "cancelé" in result.reply_text
    result = await send("Gasté 12 por prueba fecha ayer a la mañana", payload(fecha=(today - timedelta(days=1)).isoformat()), "yesterday")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == today - timedelta(days=1)


@pytest.mark.asyncio
async def test_explicit_date_update_does_not_write_spurious_amount(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"}, "changes": {"amount": 16}}
    result = await send("Cambiá la fecha del taxi a 16/09/2026", data, "edit-wrong-amount")
    assert "16/09/2026" in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad, updated.billetera_id) == (date(2026, 9, 16), Decimal("123"), wallet_id)


@pytest.mark.asyncio
async def test_stated_date_update_does_not_write_spurious_amount(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"}, "changes": {"amount": 16}}
    result = await send("La fecha del taxi era 16/09/2026", data, "stated-date")
    assert "16/09/2026" in result.reply_text or "aclar" in result.reply_text.lower()
    updated = movements(factory)[0]
    assert updated.cantidad == Decimal("123")
    assert updated.fecha_movimiento in {date(2026, 9, 16), date(2026, 10, 16)}


@pytest.mark.asyncio
async def test_stated_date_and_real_amount_are_not_silently_misapplied(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"}, "changes": {"amount": 16}}
    result = await send("La fecha del taxi era 16/09/2026 y el importe era $50", data, "stated-both")
    assert "separado" in result.reply_text or "aclar" in result.reply_text.lower()
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad) == (date(2026, 10, 16), Decimal("123"))


@pytest.mark.asyncio
async def test_stated_date_and_currency_amount_without_field_name_are_not_misapplied(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"}, "changes": {"amount": 16}}
    result = await send("La fecha del taxi era 16/09/2026 y fueron $50", data, "stated-currency")
    assert "separado" in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad) == (date(2026, 10, 16), Decimal("123"))


@pytest.mark.asyncio
async def test_combined_date_and_amount_change_requires_separate_requests(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"},
            "changes": {"amount": 16, "fecha": "2026-09-16"}}
    result = await send("Cambiá la fecha y el monto del taxi a 16/09/2026 y $50", data, "combined")
    assert "separado" in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad) == (date(2026, 10, 16), Decimal("123"))


@pytest.mark.asyncio
async def test_date_update_with_unresolved_reference_does_not_edit_recent_item(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="comida",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    await ConversationService.set_recent_items(PHONE, RecentItems("movement", [{
        "id": str(movement_id), "description": "comida", "label": "comida",
        "amount": "123", "currency": "ARS", "date": "2026-10-16"}]))
    data = {"intent": "expense", "amount": 16, "currency": "ARS"}
    result = await send("Cambiá la fecha del taxi a 16/09/2026", data, "unknown-reference")
    assert "movimiento" in result.reply_text
    updated = movements(factory)[0]
    assert (updated.fecha_movimiento, updated.cantidad) == (date(2026, 10, 16), Decimal("123"))


def test_invalid_date_and_year_inference_are_explicit():
    assert interpret_date("el 31/02/2026", None, date(2026, 10, 27))[0] is None
    assert interpret_date("el 16/09", "2025-09-16", date(2026, 10, 27)) == (date(2026, 9, 16), True)
    assert interpret_date("gasté ayer", "2026-10-27", date(2026, 10, 27)) == (None, False)
    assert interpret_date("el 16 de septiembre de 2026", "2026-10-16", date(2026, 10, 27)) == (date(2026, 9, 16), False)
    assert interpret_date("gasté el martes", None, date(2026, 10, 28)) == (date(2026, 10, 27), False)
    assert interpret_date("el próximo lunes", None, date(2026, 10, 26)) == (date(2026, 11, 2), False)


def test_today_uses_argentina_civil_date_at_utc_midnight(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 1, 2, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("app.services.movement_dates.datetime", FixedDateTime)
    assert argentina_today() == date(2026, 12, 31)


@pytest.mark.asyncio
async def test_this_weekday_when_today_matches_keeps_today(account, monkeypatch):
    factory, _, _ = account

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("app.services.movement_dates.datetime", FixedDateTime)
    result = await send("Gasté 12 en prueba fecha este martes", payload(fecha="2026-10-27"), "this-tuesday")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (date(2026, 10, 27), Decimal("12"))


@pytest.mark.asyncio
async def test_update_without_word_fecha_never_changes_amount_from_date(account):
    factory, user_id, wallet_id = account
    movement_id = uuid4()
    with factory() as session:
        session.add(MovimientoFinanciero(id=movement_id, usuario_id=user_id, billetera_id=wallet_id,
                    tipo="egreso", cantidad=Decimal("123"), moneda="ARS", descripcion="taxi",
                    fecha_movimiento=date(2026, 10, 16)))
        session.commit()
    data = {"intent": "update_movement", "reference": {"description": "taxi"}, "changes": {"amount": 16}}
    result = await send("El taxi fue el 16/09/2026", data, "taxi-date")
    assert "16/09/2026" in result.reply_text or "aclar" in result.reply_text.lower()
    updated = movements(factory)[0]
    assert updated.cantidad == Decimal("123")
    assert updated.fecha_movimiento in {date(2026, 9, 16), date(2026, 10, 16)}


@pytest.mark.asyncio
async def test_named_date_two_digit_year_is_ambiguous_even_if_llm_infers_year(account):
    factory, _, _ = account
    result = await send("Gasté 12 en prueba fecha el 16 de septiembre de 25", payload(fecha="2026-09-16"), "two-digit")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("16/09/2025", {}, "four-digit-year")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (date(2025, 9, 16), Decimal("12"))


@pytest.mark.asyncio
async def test_period_without_day_requires_clarification_even_if_llm_infers_a_day(account):
    factory, _, _ = account
    last_month = argentina_today().replace(day=1) - timedelta(days=1)
    model_date = last_month.replace(day=1).isoformat()
    data = payload(fecha=model_date)
    data["fecha_precision"] = "dia"  # Wrongly inferred by the model.
    result = await send("Gasté 12 en prueba fecha el mes pasado", data, "last-month")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    specified = last_month.replace(day=16)
    result = await send(specified.strftime("%d/%m/%Y"), {}, "last-month-day")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (specified, Decimal("12"))


@pytest.mark.asyncio
async def test_month_without_day_requires_clarification_even_if_model_invents_day(account):
    factory, _, _ = account
    result = await send("Gasté 12 en prueba fecha durante septiembre", payload(fecha="2026-09-01"), "september")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_period_precision_from_model_prevents_write_for_unrecognized_period(account):
    factory, _, _ = account
    data = payload(fecha="2026-09-01")
    data["fecha_precision"] = "periodo"
    result = await send("Gasté 12 en prueba fecha durante la cosecha pasada", data, "period-precision")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_period_precision_survives_llm_normalization():
    from tests.test_llm import _process_message_with_mock_response

    result = await _process_message_with_mock_response({
        "intent": "expense", "amount": 12, "fecha": "2026-09-01", "fecha_precision": "periodo",
    })
    assert result["movements"][0]["fecha_precision"] == "periodo"


@pytest.mark.asyncio
async def test_inconsistent_absent_precision_and_date_does_not_write(account):
    factory, _, _ = account
    data = payload(fecha="2026-09-01")
    data["fecha_precision"] = "ausente"
    result = await send("Gasté 12 en prueba", data, "absent-precision")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_invented_day_precision_without_textual_day_does_not_write(account):
    factory, _, _ = account
    data = payload(fecha="2026-09-01")
    data["fecha_precision"] = "dia"
    result = await send("Gasté 12 en prueba durante la temporada anterior", data, "invented-day")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_no_date_defaults_to_today_when_model_reports_absent(account):
    factory, _, _ = account
    data = payload()
    data["fecha_precision"] = "ausente"
    result = await send("Gasté 12 en prueba", data, "no-date")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (argentina_today(), Decimal("12"))


@pytest.mark.asyncio
async def test_weekday_in_an_imprecise_month_does_not_use_current_week(account):
    factory, _, _ = account
    result = await send("Gasté 12 el martes del mes pasado", payload(fecha="2026-09-01"), "weekday-period")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_duration_without_temporal_reference_defaults_to_today(account):
    factory, _, _ = account
    data = payload()
    data["amount"] = 100
    data["fecha_precision"] = "ausente"
    result = await send("Pagué 100 por una semana de vacaciones", data, "duration")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (argentina_today(), Decimal("100"))


@pytest.mark.asyncio
async def test_duration_does_not_override_yesterday(account):
    factory, _, _ = account
    yesterday = argentina_today() - timedelta(days=1)
    data = payload(fecha=yesterday.isoformat())
    data["amount"] = 100
    data["fecha_precision"] = "dia"
    result = await send("Pagué 100 ayer la suscripción por un mes", data, "duration-yesterday")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (yesterday, Decimal("100"))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Pagué 100 ayer la cuota de este mes",
    "Ayer pagué 100 la factura del mes pasado",
])
async def test_period_describes_the_item_and_does_not_override_explicit_day(account, text):
    factory, _, _ = account
    yesterday = argentina_today() - timedelta(days=1)
    data = payload(fecha=yesterday.isoformat())
    data.update(amount=100, fecha_precision="dia")
    result = await send(text, data, "period-item")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (yesterday, Decimal("100"))


@pytest.mark.asyncio
async def test_period_qualifying_weekday_remains_ambiguous(account):
    factory, _, _ = account
    today = argentina_today()
    previous_tuesday = today - timedelta(days=(today.weekday() - 1) % 7 or 7)
    data = payload(fecha=previous_tuesday.isoformat())
    data["fecha_precision"] = "dia"
    result = await send("Gasté 12 el martes de la semana pasada", data, "weekday-other-week")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_value", ["missing", "most_recent"])
async def test_modified_weekday_with_period_does_not_use_most_recent_weekday(account, model_value):
    factory, _, _ = account
    today = argentina_today()
    previous_tuesday = today - timedelta(days=(today.weekday() - 1) % 7 or 7)
    model_date = previous_tuesday.isoformat() if model_value == "most_recent" else None
    data = payload(fecha=model_date)
    data["fecha_precision"] = "dia"
    result = await send("Gasté 12 el primer martes del mes pasado", data, "ordinal-weekday")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_no_date_wallet_selection_persists_today_and_amount(account):
    factory, user_id, _ = account
    with factory() as session:
        session.add(Billetera(id=uuid4(), usuario_id=user_id, nombre="Otra", moneda="ARS"))
        session.commit()
    data = payload()
    data["fecha_precision"] = "ausente"
    result = await send("Gasté 12 en prueba", data, "no-date-wallet")
    assert "billetera" in result.reply_text and not movements(factory)
    result = await send("2", {}, "no-date-wallet-selected")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (argentina_today(), Decimal("12"))


@pytest.mark.asyncio
async def test_before_yesterday_does_not_match_yesterday(account):
    factory, _, _ = account
    target = argentina_today() - timedelta(days=2)
    result = await send("Gasté 12 antes de ayer", payload(fecha=target.isoformat()), "before-yesterday")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (target, Decimal("12"))


@pytest.mark.asyncio
async def test_day_after_tomorrow_does_not_match_tomorrow(account):
    factory, _, _ = account
    target = argentina_today() + timedelta(days=2)
    result = await send("Gasté 12 pasado mañana", payload(fecha=target.isoformat()), "after-tomorrow")
    assert target.strftime("%d/%m/%Y") in result.reply_text and not movements(factory)


@pytest.mark.asyncio
async def test_relative_date_disagreement_requires_clarification(account):
    factory, _, _ = account
    result = await send("Gasté 12 antes de ayer", payload(fecha=(argentina_today() - timedelta(days=1)).isoformat()), "relative-conflict")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_compound_relative_date_can_clarify_pending_operation(account):
    factory, _, _ = account
    result = await send("Gasté 12 el 31/02/2026", payload(), "bad-compound")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("antes de ayer", {}, "compound-reply")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == argentina_today() - timedelta(days=2)


@pytest.mark.asyncio
async def test_compound_future_date_clarification_still_requires_confirmation(account):
    factory, _, _ = account
    result = await send("Gasté 12 el 31/02/2026", payload(), "bad-compound-future")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("pasado mañana", {}, "compound-future")
    target = argentina_today() + timedelta(days=2)
    assert target.strftime("%d/%m/%Y") in result.reply_text and not movements(factory)
    result = await send("sí", {}, "compound-future-confirmed")
    assert "Registré" in result.reply_text
    assert movements(factory)[0].fecha_movimiento == target


@pytest.mark.asyncio
async def test_temporal_reference_to_subordinate_action_requires_clarification(account):
    factory, _, _ = account
    data = payload("ingreso")
    data["description"] = "sueldo"
    result = await send("Cobré 12 de sueldo del trabajo que terminé ayer", data, "subordinate-work")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("hoy", {}, "subordinate-work-date")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (argentina_today(), Decimal("12"))


@pytest.mark.asyncio
async def test_subordinate_date_claimed_by_provider_requires_clarification(account):
    factory, _, _ = account
    data = payload("ingreso", (argentina_today() - timedelta(days=1)).isoformat())
    data["description"] = "sueldo"
    result = await send("Cobré 12 de sueldo del trabajo que terminé ayer", data, "subordinate-conflict")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_temporal_subordinate_may_refer_to_same_movement_and_is_not_guessed(account):
    factory, _, _ = account
    data = payload()
    data["description"] = "taxi"
    result = await send("Gasté 12 en taxi que tomé ayer", data, "subordinate-same")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_subordinate_date_is_not_discarded_when_llm_description_differs(account):
    factory, _, _ = account
    data = payload()
    data.update(description="medicamentos", fecha_precision="ausente")
    result = await send("Registrá mi gasto de 12 en farmacia que pagué ayer", data, "pharmacy-date")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("ayer", {}, "pharmacy-confirmed")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (
        argentina_today() - timedelta(days=1), Decimal("12"),
    )


@pytest.mark.asyncio
async def test_repeated_action_in_subordinate_clause_is_not_assumed_distinct(account):
    factory, _, _ = account
    data = payload("ingreso")
    data["description"] = "sueldo"
    result = await send("Cobré 12 de sueldo del trabajo que cobré ayer", data, "same-verb")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_dated_participle_without_relative_marker_is_not_assumed_main_date(account):
    factory, _, _ = account
    data = payload("ingreso")
    data["description"] = "sueldo"
    result = await send("Cobré 12 de sueldo del trabajo realizado ayer", data, "dated-participle")
    assert "fecha" in result.reply_text.lower() and not movements(factory)


@pytest.mark.asyncio
async def test_explicit_main_date_is_preserved_when_other_action_has_a_period(account):
    factory, _, _ = account
    yesterday = argentina_today() - timedelta(days=1)
    data = payload("ingreso", yesterday.isoformat())
    data["description"] = "sueldo"
    result = await send("Cobré ayer 12 de sueldo del trabajo que terminé el mes pasado", data, "main-date-subordinate")
    assert "fecha" in result.reply_text.lower() and not movements(factory)
    result = await send("ayer", {}, "main-date-confirmed")
    assert "Registré" in result.reply_text
    assert (movements(factory)[0].fecha_movimiento, movements(factory)[0].cantidad) == (yesterday, Decimal("12"))


@pytest.mark.asyncio
async def test_invalid_precision_does_not_disappear_in_normalization():
    from tests.test_llm import _process_message_with_mock_response

    result = await _process_message_with_mock_response({
        "intent": "expense", "amount": 12, "fecha": "2026-09-01", "fecha_precision": ["unexpected"],
    })
    assert result["movements"][0]["fecha_precision"] == "ambigua"
