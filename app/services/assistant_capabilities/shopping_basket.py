"""Confirmed personal basket and read-only offer presentation."""
import asyncio
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import func

from app.api.whatsapp import WhatsAppReplyButton, WhatsAppReplyButtons
from app.models.database import SepaPrice, ShoppingBasket, ShoppingBasketItem, SessionLocal
from app.services.assistant_contracts import FieldRequest, FrozenModel, IncompleteOperation, ReferenceToken
from app.services.assistant_capabilities.common import data, read_tool, operation_tool, prepared, committed
from app.services.supermarket_prices import SOURCE, _money, _query_rows, _snapshot, compare_prices, normalize_province


class BasketSearch(FrozenModel):
    locality: str | None = Field(default=None, min_length=2, max_length=120,
        description="Localidad indicada por el usuario; si falta se usa la de su canasta")
    province: str | None = Field(default=None, max_length=80)


class BasketItemInput(FrozenModel):
    product: str = Field(min_length=2, max_length=120, description="Nombre del producto dicho por el usuario, por ejemplo arroz")
    quantity: int = Field(default=1, ge=1, le=20)
    product_ref: ReferenceToken | None = None


class BasketDraft(FrozenModel):
    items: tuple[BasketItemInput, ...] = Field(min_length=1, max_length=10)
    locality: str | None = Field(default=None, min_length=2, max_length=120)
    province: str | None = Field(default=None, max_length=80)
    mode: Literal["add", "replace"] = "add"


class BasketProductChoice(FrozenModel):
    id: str  # Internal catalog identity; never shown as a user instruction.
    snapshot_id: str
    operation_id: str
    item_index: int
    query: str
    locality: str
    province: str | None = None
    visible: str


class BasketPrepared(FrozenModel):
    items: tuple[dict, ...]
    locality: str
    province: str | None = None


def _basket(owner):
    with SessionLocal() as session:
        basket = session.query(ShoppingBasket).filter(ShoppingBasket.usuario_id == owner).first()
        if basket is None:
            return None, []
        items = [(row.ean, row.product_name, row.quantity) for row in session.query(ShoppingBasketItem)
            .filter(ShoppingBasketItem.usuario_id == owner).order_by(ShoppingBasketItem.product_name)]
        return basket, items


def has_basket(owner):
    with SessionLocal() as session:
        return session.query(ShoppingBasketItem.ean).filter(ShoppingBasketItem.usuario_id == UUID(str(owner))).first() is not None


async def show_basket(context, args):
    basket, items = await asyncio.to_thread(_basket, context.turn.owner_id)
    if not items:
        return data(context, [], ["Todavía no tenés una canasta guardada. Decime qué productos querés agregar y en qué localidad comprás."])
    lines = [f"Tu canasta para {basket.locality}: " + "; ".join(f"{name} × {quantity}" for _, name, quantity in items)]
    context.outbound.append(WhatsAppReplyButtons(body="¿Querés consultar precios de tu canasta?",
        buttons=(WhatsAppReplyButton(id="supermarket:basket", title="Consultar ofertas"),)))
    return data(context, [{"name": name, "quantity": quantity} for _, name, quantity in items], lines)


async def compare_basket(context, args):
    basket, items = await asyncio.to_thread(_basket, context.turn.owner_id)
    if not items:
        return data(context, [], ["Todavía no tenés una canasta guardada."])
    locality = args.locality or basket.locality
    result = await compare_prices([ean for ean, _, _ in items], locality=locality,
        province=args.province or basket.province, today=context.turn.reference_day)
    return data(context, [result], present_basket_offers(result, locality=locality))


async def compare_saved_basket(owner, today):
    basket, items = await asyncio.to_thread(_basket, owner)
    if not items:
        return "Todavía no tenés una canasta guardada."
    result = await compare_prices([ean for ean, _, _ in items], locality=basket.locality,
        province=basket.province, today=today)
    return "\n".join(present_basket_offers(result, locality=basket.locality))


def _resolve_items(owner, draft, today, choices, operation_id):
    with SessionLocal() as session:
        basket = session.query(ShoppingBasket).filter(ShoppingBasket.usuario_id == owner).first()
        locality = draft.locality or (basket.locality if basket and draft.mode == "add" else None)
        province = normalize_province(draft.province or (basket.province if basket and draft.mode == "add" else None))
        if not locality:
            return {"status": "needs_locality"}
        snapshot, reason = _snapshot(session, today)
        if snapshot is None:
            return {"status": "unavailable", "reason": reason}
        resolved = []
        for index, item in enumerate(draft.items):
            choice = choices.get(index)
            if choice:
                if (choice.snapshot_id != str(snapshot.id) or choice.operation_id != operation_id
                        or choice.item_index != index or choice.query.casefold() != item.product.casefold()
                        or choice.locality.casefold() != locality.casefold()
                        or (choice.province or "").casefold() != (province or "").casefold()):
                    return {"status": "stale_choice", "index": index}
                query = session.query(SepaPrice).filter(SepaPrice.snapshot_id == snapshot.id,
                    SepaPrice.product_id == choice.id, SepaPrice.is_ean.is_(True),
                    func.lower(SepaPrice.locality) == locality.strip().lower())
                if province:
                    query = query.filter(func.lower(SepaPrice.province) == province.strip().lower())
                row = query.first()
                if row is None:
                    return {"status": "stale_choice", "index": index}
                resolved.append({"ean": choice.id, "name": choice.visible, "quantity": item.quantity})
                continue
            _, identities = _query_rows(session, snapshot, name=item.product, locality=locality, province=province)
            if not identities:
                return {"status": "missing", "index": index, "product": item.product, "locality": locality}
            if len(identities) > 1:
                return {"status": "ambiguous", "index": index, "query": item.product,
                    "choices": list(identities.items())[:5], "snapshot_id": str(snapshot.id),
                    "locality": locality, "province": province}
            ean, name = next(iter(identities.items()))
            resolved.append({"ean": ean, "name": name, "quantity": item.quantity})
        existing = []
        if basket and draft.mode == "add":
            existing = [{"ean": row.ean, "name": row.product_name, "quantity": row.quantity}
                for row in session.query(ShoppingBasketItem).filter(ShoppingBasketItem.usuario_id == owner).all()]
        by_ean = {item["ean"]: item for item in existing}
        for item in resolved:
            if item["ean"] in by_ean:
                by_ean[item["ean"]]["quantity"] += item["quantity"]
                if by_ean[item["ean"]]["quantity"] > 20:
                    return {"status": "too_many_units"}
            else:
                by_ean[item["ean"]] = item
        if len(by_ean) > 10:
            return {"status": "too_many_products"}
        return {"status": "ok", "items": sorted(by_ean.values(), key=lambda item: (item["name"], item["ean"])),
            "locality": locality, "province": province}


async def prepare_basket(context, operation):
    draft = BasketDraft.model_validate(operation.draft_payload)
    choices = {}
    for index, item in enumerate(draft.items):
        if item.product_ref:
            choices[index] = await context.references.resolve(context.turn, item.product_ref,
                kind="basket_product", payload_model=BasketProductChoice)
    result = await asyncio.to_thread(_resolve_items, context.turn.owner_id, draft,
        context.turn.reference_day, choices, operation.operation_id)
    if result["status"] == "needs_locality":
        return IncompleteOperation(missing_fields=(FieldRequest(field="locality", reason="missing",
            question="¿En qué localidad comprás? Así busco los productos para tu canasta."),))
    if result["status"] == "unavailable":
        return IncompleteOperation(missing_fields=(FieldRequest(field="catalog", reason="invalid",
            question=f"Ahora no puedo buscar productos para la canasta: {result['reason']} Probá más tarde."),))
    if result["status"] == "missing":
        return IncompleteOperation(missing_fields=(FieldRequest(field=f"items.{result['index']}.product", reason="missing",
            question=f"No encontré «{result['product']}» en {result['locality']}. ¿Me decís la marca o presentación?"),))
    if result["status"] == "stale_choice":
        return IncompleteOperation(missing_fields=(FieldRequest(field=f"items.{result['index']}.product_ref", reason="stale",
            question="La lista de productos cambió. Volvé a indicar el producto para buscar opciones actuales."),))
    if result["status"] in {"too_many_units", "too_many_products"}:
        raise ValueError("La canasta admite hasta 10 productos y 20 unidades de cada uno")
    if result["status"] == "ambiguous":
        refs = []
        for ean, name in result["choices"]:
            refs.append(await context.reference("basket_product", BasketProductChoice(id=ean,
                snapshot_id=result["snapshot_id"], operation_id=operation.operation_id,
                item_index=result["index"], query=result["query"], locality=result["locality"],
                province=result["province"], visible=name), expires_at=operation.expires_at))
        return IncompleteOperation(missing_fields=(FieldRequest(field=f"items.{result['index']}.product_ref",
            reason="ambiguous", candidate_refs=tuple(refs),
            question=f"Encontré varias opciones de {result['query']}. ¿Cuál querés agregar? Respondé con el número o decime la marca y presentación para afinar la búsqueda."),))
    items = result["items"]
    return prepared(BasketPrepared(items=tuple(items), locality=result["locality"], province=result["province"]),
        f"Guardaría esta canasta para {result['locality']}: " + "; ".join(
        f"{item['name']} × {item['quantity']}" for item in items))


async def commit_basket(context, operation, confirmation):
    value = BasketPrepared.model_validate(operation.prepared_payload)
    # The operation service revalidates the prepared payload before commit.
    def save():
        with SessionLocal() as session:
            session.query(ShoppingBasketItem).filter(ShoppingBasketItem.usuario_id == context.turn.owner_id).delete()
            session.query(ShoppingBasket).filter(ShoppingBasket.usuario_id == context.turn.owner_id).delete()
            session.add(ShoppingBasket(usuario_id=context.turn.owner_id, locality=value.locality,
                province=value.province))
            session.add_all(ShoppingBasketItem(usuario_id=context.turn.owner_id, ean=item["ean"],
                product_name=item["name"], quantity=item["quantity"]) for item in value.items)
            session.commit()
    await asyncio.to_thread(save)
    return committed(context, f"Guardé {len(value.items)} productos en tu canasta.")


def shopping_basket_contracts():
    return (
        read_tool("show_shopping_basket", "Sólo muestra la canasta existente. No usar si el usuario quiere agregar, poner o guardar un producto.", FrozenModel, show_basket),
        read_tool("compare_shopping_basket", "Compara precios de los EAN guardados en la canasta en la localidad indicada. No modifica presupuesto ni movimientos.", BasketSearch, compare_basket),
        operation_tool("save_shopping_basket", "Agrega por NOMBRE productos a la canasta frecuente, o la reemplaza si mode=replace. Para 'quiero poner arroz en mi canasta' usar esta herramienta con product='arroz'; nunca pedir EAN al usuario. Si faltan localidad u opción concreta, el backend pregunta. Requiere propuesta y confirmación posterior.", BasketDraft, BasketPrepared, prepare_basket, commit_basket, schema_version=2),
    )


def present_basket_offers(result, *, locality):
    if result["status"] != "ok":
        return [result["reason"]]
    available = []
    missing = []
    for item in result["items"]:
        if item["status"] != "ok":
            missing.append(item["query"])
            continue
        available.extend(option for option in item["options"] if option.get("promotion"))
    if not available:
        return [f"No hay ofertas verificadas para tu canasta en {locality} con el dato SEPA del "
                f"{date.fromisoformat(result['date']):%d/%m/%Y}."]
    lines = [f"Archivo de {SOURCE} publicado el {date.fromisoformat(result['date']):%d/%m/%Y} "
             f"para {locality}. Confirmá las condiciones y el precio en la sucursal."]
    for option in available[:10]:
        saving = Decimal(option["list_price"]) - Decimal(option["price"])
        lines.append(f"{option['name'][:120]} — {option['chain']}, {option['branch']}: {_money(option['price'])} "
            f"por presentación (dato {option['date']}); lista {_money(option['list_price'])}, "
            f"ahorro estimado {_money(saving)}. "
            f"Condiciones: {option['promotion']}.")
    if missing:
        lines.append("Sin precio para: " + ", ".join(missing) + ".")
    return lines
