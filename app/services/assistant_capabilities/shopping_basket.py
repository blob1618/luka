"""Confirmed personal basket and read-only offer presentation."""
import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.api.whatsapp import WhatsAppReplyButton, WhatsAppReplyButtons
from app.models.database import SepaPrice, SepaSnapshot, ShoppingBasket, ShoppingBasketItem, SessionLocal
from app.services.assistant_contracts import FrozenModel
from app.services.assistant_capabilities.common import data, read_tool, operation_tool, prepared, committed
from app.services.supermarket_prices import SOURCE, _money, compare_prices


class BasketSearch(FrozenModel):
    locality: str | None = Field(default=None, min_length=2, max_length=120,
        description="Localidad indicada por el usuario; si falta se usa la de su canasta")
    province: str | None = Field(default=None, max_length=80)


class BasketItemInput(FrozenModel):
    ean: str = Field(pattern=r"^(?:\d{8}|\d{12}|\d{13})$")
    quantity: int = Field(default=1, ge=1, le=20)


class BasketDraft(FrozenModel):
    items: tuple[BasketItemInput, ...] = Field(min_length=1, max_length=10)
    locality: str = Field(min_length=2, max_length=120)
    province: str | None = Field(default=None, max_length=80)


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
        return data(context, [], ["Todavía no tenés una canasta guardada. Decime los productos y buscá sus EAN para guardarlos."])
    lines = [f"Tu canasta para {basket.locality}: " + "; ".join(f"{name} (EAN {ean}, {quantity} unidad/es)" for ean, name, quantity in items)]
    context.outbound.append(WhatsAppReplyButtons(body="¿Querés consultar precios de tu canasta?",
        buttons=(WhatsAppReplyButton(id="supermarket:basket", title="Consultar ofertas"),)))
    return data(context, [{"ean": ean, "name": name, "quantity": quantity} for ean, name, quantity in items], lines)


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


def _resolve_items(items):
    with SessionLocal() as session:
        snapshot = session.query(SepaSnapshot).filter(SepaSnapshot.status == "ready").order_by(
            SepaSnapshot.data_date.desc()).first()
        if snapshot is None:
            raise ValueError("No hay catálogo SEPA cargado para verificar los EAN")
        if len({item.ean for item in items}) != len(items):
            raise ValueError("La canasta contiene EAN repetidos")
        names = []
        for item in items:
            row = session.query(SepaPrice.description).filter(SepaPrice.snapshot_id == snapshot.id,
                SepaPrice.is_ean.is_(True), SepaPrice.product_id == item.ean).first()
            if row is None:
                raise ValueError(f"No encontré el EAN {item.ean} en el catálogo")
            names.append({"ean": item.ean, "name": row.description, "quantity": item.quantity})
        return names


async def prepare_basket(context, operation):
    draft = BasketDraft.model_validate(operation.draft_payload)
    items = await asyncio.to_thread(_resolve_items, draft.items)
    return prepared(BasketPrepared(items=tuple(items), locality=draft.locality, province=draft.province),
        f"Guardaría esta canasta para {draft.locality}: " + "; ".join(
        f"{item['name']} × {item['quantity']} (EAN {item['ean']})" for item in items))


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
        read_tool("show_shopping_basket", "Muestra la canasta frecuente guardada y ofrece consultar ofertas. No exige alerta de presupuesto.", FrozenModel, show_basket),
        read_tool("compare_shopping_basket", "Compara precios de los EAN guardados en la canasta en la localidad indicada. No modifica presupuesto ni movimientos.", BasketSearch, compare_basket),
        operation_tool("save_shopping_basket", "Reemplaza la canasta frecuente con hasta 10 EAN verificados. Requiere propuesta completa y confirmación posterior.", BasketDraft, BasketPrepared, prepare_basket, commit_basket),
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
