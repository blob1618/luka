"""Saved-basket and budget-alert integration with synthetic delivery."""
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest

from app.api.whatsapp import WhatsAppReplyButtons
from app.models.database import LimiteCategoria, SepaPrice, SepaSnapshot, ShoppingBasketItem, Usuario, MovimientoFinanciero
from app.services.assistant_capabilities.movement_effects import append_budget_threshold_alerts
from app.services.assistant_capabilities import shopping_basket
from app.services.assistant_capabilities.shopping_basket import present_basket_offers
from app.services.budget import BudgetStatus
from app.services.dispatch_contract import DispatchResult
from app.services.supermarket_prices import compare_prices
from tests.test_assistant_pilot import NOW, PHONE
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
async def test_confirmed_basket_and_button_are_read_only(ingress, accounts, monkeypatch):
    class CatalogProvider:
        async def generate_json(self, prompt, user_message):
            return {"matching_indices": [1], "uncertain": False}
    monkeypatch.setattr(shopping_basket, "create_provider", CatalogProvider)
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
        "items": [{"product": "Coca Cola", "quantity": 2}], "locality": "Resistencia", "province": "AR-H"})
    assert isinstance(preview.reply_message, WhatsAppReplyButtons)
    assert "EAN" not in preview.reply_text
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).count() == 0
    confirmed = await ingress.confirm(preview)
    assert "Guardé 1 productos" in confirmed.reply_text
    shown = await ingress.tool("show_shopping_basket")
    assert "EAN" not in shown.reply_text
    assert "Tu canasta para Resistencia:\nMenor precio publicado" in shown.reply_text
    assert "1. Coca Cola 2,25 L × 2 — $5.900,00 por paquete en Coto, Centro" in shown.reply_text
    assert "no es una compra registrada" in shown.reply_text
    button = next(message.buttons[0].id for message in shown.followup_messages
                  if isinstance(message, WhatsAppReplyButtons))
    assert button == "supermarket:basket_prices"
    prices = await ingress.send("Comparar precios", option_id=button)
    assert "Coto" in prices.reply_text and "$5.900,00" in prices.reply_text
    offers = await ingress.tool("find_shopping_basket_offers")
    assert "ahorro estimado $200,00" in offers.reply_text
    alert_offers = await ingress.send("Consultar ofertas", option_id="supermarket:basket")
    assert "ahorro estimado $200,00" in alert_offers.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count() == before
        assert session.query(ShoppingBasketItem).one().quantity == 2
        assert session.query(LimiteCategoria).one().cantidad_max == Decimal("10000")
    added = await ingress.tool("save_shopping_basket", {"items": [{"product": "Coca Cola"}]},
        "agregá una Coca Cola a mi canasta")
    assert "Coca Cola 2,25 L × 3" in added.reply_text
    await ingress.confirm(added)
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).one().quantity == 3
        assert session.query(MovimientoFinanciero).count() == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_basket_without_valid_promotion_reports_no_offer(sepa_db):
    result = await compare_prices(["7790895000997"], locality="Resistencia", today=date(2026, 11, 21))
    message = present_basket_offers(result, locality="Resistencia")[0]
    assert "No hay promociones verificadas" in message
    assert "no significa que no haya precios" in message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_arroz_by_name_asks_locality_then_numbered_product_and_saves_after_confirmation(
        ingress, accounts, monkeypatch):
    monkeypatch.setattr(shopping_basket, "SessionLocal", accounts[0])
    class CatalogProvider:
        async def generate_json(self, prompt, user_message):
            assert "Arroz con leche listo" in user_message
            return {"matching_indices": [2, 3], "uncertain": False}
    monkeypatch.setattr(shopping_basket, "create_provider", CatalogProvider)
    day = (NOW - timedelta(hours=3)).date()
    with accounts[0]() as session:
        snapshot = SepaSnapshot(data_date=day, source_url="https://example.test/sepa.zip", status="ready")
        session.add(snapshot)
        session.flush()
        for ean, description, prices in [
                ("7790000000001", "Arroz largo fino Marca A 1 kg", (("Coto", "Centro", "2000"),
                    ("Vea", "Norte", "2100"))),
                ("7790000000002", "Arroz integral Marca B 1 kg", (("Maxi", "Sur", "1500"),
                    ("Coto", "Centro", "1700"))),
                ("7790000000003", "Arroz con leche listo 180 gr", (("Coto", "Centro", "500"),))]:
            for index, (chain, branch, price) in enumerate(prices, 1):
                session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id=chain,
                    flag_id="1", branch_id=str(index), product_id=ean, report_date=day,
                    is_ean=True, description=description, chain=chain, branch=branch,
                    province="AR-H", locality="Resistencia", list_price=Decimal(price)))
        session.commit()
    first = await ingress.tool("save_shopping_basket", {"items": [{"product": "arroz"}]},
        "quiero poner arroz en mi canasta")
    assert "¿En qué localidad" in first.reply_text
    assert "EAN" not in first.reply_text
    state = await scenarios.assistant_state.read(accounts[1])
    operation = state["workspace"]["operations"][state["workspace"]["active_operation_id"]]
    ref = next(ref for ref, record in state["references"].items()
        if record["kind"] == "operation" and record["payload"]["operation_id"] == operation["operation_id"])
    second = await ingress.tool("operation_amend", {"ref": ref, "expected_revision": operation["revision"],
        "changes": {"locality": "Resistencia", "province": "AR-H"}}, "Resistencia, Chaco")
    assert "1. Arroz integral Marca B 1 kg — $1.500,00 en Maxi, Sur" in second.reply_text
    assert "2. Arroz largo fino Marca A 1 kg — $2.000,00 en Coto, Centro" in second.reply_text
    assert "$1.700,00" not in second.reply_text and "$2.100,00" not in second.reply_text
    assert "Arroz con leche" not in second.reply_text
    assert "Precios de SEPA / Precios Claros" in second.reply_text
    assert "EAN" not in second.reply_text
    selected = await ingress.send("2")
    assert "¿Confirmás esta propuesta?" in selected.reply_text
    assert "Arroz" in selected.reply_text and "EAN" not in selected.reply_text
    assert "Guardaría esta canasta para Resistencia:\n1. Arroz largo fino Marca A 1 kg × 1" in selected.reply_text
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).count() == 0
    await ingress.confirm(selected)
    with accounts[0]() as session:
        item = session.query(ShoppingBasketItem).one()
        assert item.ean == "7790000000001"
        assert item.quantity == 1
    shown = await ingress.tool("show_shopping_basket")
    assert "Arroz largo fino Marca A" in shown.reply_text and "EAN" not in shown.reply_text
    assert "$2.000,00 por paquete en Coto, Centro" in shown.reply_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_semantic_product_filter_only_accepts_catalog_indices(monkeypatch):
    candidates = [{"ean": "1", "name": "Banana fresca"},
        {"ean": "2", "name": "Helado de banana"}]

    class CatalogProvider:
        async def generate_json(self, prompt, user_message):
            assert json.loads(user_message)["candidatos"][1]["nombre"] == "Helado de banana"
            return {"matching_indices": [1], "uncertain": False}

    monkeypatch.setattr(shopping_basket, "create_provider", CatalogProvider)
    assert await shopping_basket._semantic_candidates("banana", "quiero banana", candidates) == candidates[:1]

    class InvalidProvider:
        async def generate_json(self, prompt, user_message):
            return {"matching_indices": [3], "uncertain": False}

    monkeypatch.setattr(shopping_basket, "create_provider", InvalidProvider)
    assert await shopping_basket._semantic_candidates("banana", "quiero banana", candidates) is None

    class UnsureProvider:
        async def generate_json(self, prompt, user_message):
            return {"matching_indices": [], "uncertain": True}

    monkeypatch.setattr(shopping_basket, "create_provider", UnsureProvider)
    assert await shopping_basket._semantic_candidates("banana", "quiero banana", candidates[:1]) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_derived_product_is_not_proposed_as_base_food(ingress, accounts, monkeypatch):
    monkeypatch.setattr(shopping_basket, "SessionLocal", accounts[0])

    class CatalogProvider:
        async def generate_json(self, prompt, user_message):
            return {"matching_indices": [], "uncertain": False}

    monkeypatch.setattr(shopping_basket, "create_provider", CatalogProvider)
    day = (NOW - timedelta(hours=3)).date()
    with accounts[0]() as session:
        snapshot = SepaSnapshot(data_date=day, source_url="https://example.test/sepa.zip", status="ready")
        session.add(snapshot)
        session.flush()
        session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id="1", flag_id="1", branch_id="1",
            product_id="7790000000011", report_date=day, is_ean=True,
            description="Arroz con leche listo 180 gr", chain="Coto", branch="Centro",
            province="AR-H", locality="Resistencia", list_price=Decimal("500")))
        session.commit()
    reply = await ingress.tool("save_shopping_basket", {"items": [{"product": "arroz"}],
        "locality": "Resistencia", "province": "AR-H"}, "quiero agregar arroz")
    assert "No puedo distinguir con seguridad" in reply.reply_text
    assert "Arroz con leche" not in reply.reply_text
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).count() == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_basket_accepts_chaco_name_and_shows_real_rice_options(ingress, accounts, monkeypatch):
    from app.models.database import ShoppingBasket
    monkeypatch.setattr(shopping_basket, "SessionLocal", accounts[0])
    class CatalogProvider:
        async def generate_json(self, prompt, user_message):
            return {"matching_indices": [1, 2], "uncertain": False}
    monkeypatch.setattr(shopping_basket, "create_provider", CatalogProvider)
    day = (NOW - timedelta(hours=3)).date()
    with accounts[0]() as session:
        snapshot = SepaSnapshot(data_date=day, source_url="https://example.test/sepa.zip", status="ready")
        session.add(snapshot)
        session.flush()
        for index, description in enumerate(("ALFAJOR DE ARROZ X 28 G",
                "ARROZ LARGO FINO MARCA A 1 KG", "ARROZ INTEGRAL MARCA B 1 KG"), 1):
            session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id="1", flag_id="1",
                branch_id="1", product_id=f"779000000000{index}", report_date=day,
                is_ean=True, description=description, chain="Coto", branch="Centro",
                province="AR-H", locality="Resistencia", list_price=Decimal("2000")))
        session.commit()
    reply = await ingress.tool("save_shopping_basket", {"items": [{"product": "arroz"}],
        "locality": "Resistencia", "province": "Chaco"}, "quiero poner arroz en mi canasta")
    assert "1. ARROZ" in reply.reply_text and "2. ARROZ" in reply.reply_text
    assert "ALFAJOR" not in reply.reply_text and "EAN" not in reply.reply_text
    with accounts[0]() as session:
        assert session.query(ShoppingBasketItem).count() == 0
    selected = await ingress.send("1")
    assert "¿Confirmás esta propuesta?" in selected.reply_text
    await ingress.confirm(selected)
    with accounts[0]() as session:
        assert session.query(ShoppingBasket).one().province == "AR-H"
        assert session.query(ShoppingBasketItem).one().product_name.startswith("ARROZ ")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_named_basket_explains_missing_catalog_without_asking_for_codes(ingress, accounts, monkeypatch):
    monkeypatch.setattr(shopping_basket, "SessionLocal", accounts[0])
    result = await ingress.tool("save_shopping_basket", {"items": [{"product": "arroz"}],
        "locality": "Resistencia", "province": "AR-H"}, "quiero poner arroz en mi canasta")
    assert "No hay precios SEPA cargados" in result.reply_text
    assert "EAN" not in result.reply_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_old_code_based_basket_draft_is_retired_without_blocking_conversation(ingress, accounts):
    await ingress.tool("save_shopping_basket", {"items": [{"product": "arroz"}]},
        "quiero poner arroz en mi canasta")
    state = await scenarios.assistant_state.read(accounts[1])
    active = state["workspace"]["active_operation_id"]
    state["workspace"]["operations"][active]["schema_version"] = 1
    state["workspace"]["operations"][active]["draft_payload"] = {
        "items": [{"ean": "7790000000001", "quantity": 1}], "locality": "Resistencia", "province": "AR-H"}
    context = scenarios.assistant_runtime.make_context(accounts[1], PHONE, "next", "hola", NOW, state)
    workspace = context.operations.workspace(context)
    assert active not in workspace.operations
    assert workspace.active_operation_id is None
    assert "pending_question" not in state
