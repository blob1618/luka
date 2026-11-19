"""Saved-basket and budget-alert integration with synthetic delivery."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.api.whatsapp import WhatsAppReplyButtons
from app.models.database import LimiteCategoria, SepaPrice, SepaSnapshot, ShoppingBasketItem, Usuario, MovimientoFinanciero
from app.services.assistant_capabilities.movement_effects import append_budget_threshold_alerts
from app.services.assistant_capabilities.shopping_basket import present_basket_offers
from app.services.budget import BudgetStatus
from app.services.dispatch_contract import DispatchResult
from app.services.supermarket_prices import compare_prices
from tests.test_assistant_pilot import NOW
from tests import test_assistant_stage5 as scenarios

pytest_plugins = ["tests.test_supermarket_prices"]

accounts = scenarios.accounts
ingress = scenarios.ingress


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_offer_only_on_food_crossing_with_saved_basket(sepa_db, monkeypatch):
    from uuid import uuid4
    from app.models.database import ShoppingBasket, ShoppingBasketItem
    from app.services.assistant_capabilities import movement_effects
    owner = uuid4()
    with sepa_db() as session:
        session.add(Usuario(id=owner, nombre="Test", email="test@example.com"))
        session.add(ShoppingBasket(usuario_id=owner, locality="Resistencia", province="AR-H"))
        session.add(ShoppingBasketItem(usuario_id=owner, ean="7790895000997", product_name="Coca Cola", quantity=1))
        session.commit()
    async def no_flow(**kwargs):
        return None
    monkeypatch.setattr(movement_effects.ConversationFlowRuntime, "render_event", no_flow)
    budget = BudgetStatus(limit_id=str(uuid4()), user_id=str(owner), category_id=str(uuid4()),
        category_name="Comida", currency="ARS", period_start=date(2026,11,1), period_end=date(2026,11,30),
        limit_amount=Decimal("1000"), spent_amount=Decimal("800"), remaining_amount=Decimal("200"),
        exceeded_amount=Decimal("0"), percentage=Decimal("80"), state="available")
    result = DispatchResult("Registrado", budget_threshold_alerts=[budget])
    await append_budget_threshold_alerts("5491111111111", result)
    assert len(result.followup_messages) == 2
    assert isinstance(result.followup_messages[1], WhatsAppReplyButtons)
    assert result.followup_messages[1].buttons[0].id == "supermarket:basket"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirmed_basket_and_button_are_read_only(ingress, accounts):
    day = (NOW - timedelta(hours=3)).date()
    with accounts[0]() as session:
        session.add(LimiteCategoria(usuario_id=accounts[1], categoria_id=accounts[4],
            cantidad_max=Decimal("10000"), moneda="ARS", inicio_periodo=day.replace(day=1),
            fin_periodo=day.replace(day=31)))
        snapshot = SepaSnapshot(data_date=day, source_url="https://example.test/sepa.zip", status="ready")
        session.add(snapshot)
        session.flush()
        session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id="1", flag_id="1", branch_id="1",
            product_id="7790895000997", report_date=day, is_ean=True, description="Coca Cola 2,25 L", brand="Coca Cola",
            quantity=Decimal("2.25"), unit="l", chain="Coto", branch="Centro", province="AR-H",
            locality="Resistencia", list_price=Decimal("6100"), promo1_price=Decimal("5900"),
            promo1_text=f"Descuento. Desde el {day:%d/%m/%Y} Hasta el {day:%d/%m/%Y}"))
        session.commit()
        before = session.query(MovimientoFinanciero).count()
    preview = await ingress.tool("save_shopping_basket", {
        "items": [{"ean": "7790895000997", "quantity": 2}], "locality": "Resistencia", "province": "AR-H"})
    assert isinstance(preview.reply_message, WhatsAppReplyButtons)
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).count() == 0
    confirmed = await ingress.confirm(preview)
    assert "Guardé 1 productos" in confirmed.reply_text
    shown = await ingress.tool("show_shopping_basket")
    button = next(message.buttons[0].id for message in shown.followup_messages
                  if isinstance(message, WhatsAppReplyButtons))
    offers = await ingress.send("Consultar ofertas", option_id=button)
    assert "Coto" in offers.reply_text and "$5.900,00" in offers.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count() == before
        assert session.query(ShoppingBasketItem).one().quantity == 2
        assert session.query(LimiteCategoria).one().cantidad_max == Decimal("10000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_basket_without_valid_promotion_reports_no_offer(sepa_db):
    result = await compare_prices(["7790895000997"], locality="Resistencia", today=date(2026, 11, 21))
    assert "No hay ofertas verificadas" in present_basket_offers(result, locality="Resistencia")[0]
