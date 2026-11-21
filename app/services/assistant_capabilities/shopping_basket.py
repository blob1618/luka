"""Confirmed personal basket and read-only offer presentation."""
import asyncio
from datetime import date
from decimal import Decimal
import json
from typing import Literal

from pydantic import Field
from sqlalchemy import func

from app.api.whatsapp import WhatsAppReplyButton, WhatsAppReplyButtons
from app.models.database import SepaPrice, ShoppingBasket, ShoppingBasketItem, SessionLocal
from app.services.assistant_contracts import FieldRequest, FrozenModel, IncompleteOperation, ReferenceToken
from app.services.assistant_capabilities.common import data, read_tool, operation_tool, prepared, committed
from app.services.llm_providers import create_provider
from app.services.supermarket_prices import SOURCE, _money, _snapshot, basket_candidates, compare_prices, normalize_province, present_comparison


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
    label: str | None = None


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


async def _semantic_candidates(query, request, candidates):
    """Use the existing LLM to select catalog names, never prices or product IDs."""
    prompt = ("Sos un filtro de relevancia de productos de supermercado. Devolvé sólo JSON con "
        "matching_indices (hasta cinco números) y uncertain (booleano). Identificá el tipo de "
        "producto que la persona quiso comprar. Un ingrediente o alimento pedido por su nombre "
        "sin más detalles se refiere al producto base, no a platos, snacks, postres, cosméticos "
        "u otros derivados que lo contienen o lo usan como sabor. Si el pedido no permite "
        "distinguir el tipo con seguridad, devolvé uncertain=true y matching_indices=[]. "
        "Si ninguno de los nombres de esta lista corresponde, devolvé uncertain=false y "
        "matching_indices=[]. Que el catálogo no incluya el producto pedido no vuelve "
        "ambiguo el pedido; no marques uncertain=true sólo por esa ausencia. "
        "Elegí sólo números de la lista y mantené el orden de precio recibido. No inventes productos.")
    payload = {"pedido": request, "producto": query,
        "candidatos": [{"numero": index, "nombre": item["name"]}
            for index, item in enumerate(candidates, 1)]}
    try:
        response = await asyncio.wait_for(create_provider().generate_json(
            prompt, json.dumps(payload, ensure_ascii=False)), timeout=15)
    except Exception:
        return None
    indices = response.get("matching_indices") if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get("uncertain") is not False or not isinstance(indices, list):
        return None
    if any(not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= len(candidates)
            for index in indices):
        return None
    return [candidates[index - 1] for index in sorted(set(indices))[:5]]


async def show_basket(context, args):
    basket, items = await asyncio.to_thread(_basket, context.turn.owner_id)
    if not items:
        return data(context, [], ["Todavía no tenés una canasta guardada. Decime qué productos querés agregar y en qué localidad comprás."])
    result = await compare_prices([ean for ean, _, _ in items], locality=basket.locality,
        province=basket.province, today=context.turn.reference_day)
    lines = [f"Tu canasta para {basket.locality}:"]
    if result["status"] == "ok":
        lines.append(f"Menor precio publicado en {SOURCE} del "
            f"{date.fromisoformat(result['date']):%d/%m/%Y}; no es una compra registrada.")
    for index, (_, name, quantity) in enumerate(items, 1):
        line = f"{index}. {name} × {quantity}"
        if result["status"] == "ok":
            quoted = result["items"][index - 1]
            if quoted["status"] == "ok" and quoted["options"]:
                option = quoted["options"][0]
                line += (f" — {_money(option['price'])} por paquete en "
                    f"{option['chain']}, {option['branch']}")
            else:
                line += " — sin precio publicado en esa localidad"
        lines.append(line)
    if result["status"] != "ok":
        lines.append(f"No pude consultar precios actuales: {result['reason']}")
    context.outbound.append(WhatsAppReplyButtons(body="¿Qué querés consultar de tu canasta?",
        buttons=(WhatsAppReplyButton(id="supermarket:basket_prices", title="Comparar precios"),
            WhatsAppReplyButton(id="supermarket:basket", title="Consultar ofertas"))))
    return data(context, [{"name": name, "quantity": quantity} for _, name, quantity in items], lines)


async def compare_basket(context, args):
    basket, items = await asyncio.to_thread(_basket, context.turn.owner_id)
    if not items:
        return data(context, [], ["Todavía no tenés una canasta guardada."])
    locality = args.locality or basket.locality
    result = await compare_prices([ean for ean, _, _ in items], locality=locality,
        province=args.province or basket.province, today=context.turn.reference_day)
    return data(context, [result], present_comparison(result, locality=locality))


async def find_basket_offers(context, args):
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
    return "\n".join(present_comparison(result, locality=basket.locality))


async def find_saved_basket_offers(owner, today):
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
            candidates = basket_candidates(session, snapshot, name=item.product,
                locality=locality, province=province, today=today)
            if not candidates:
                return {"status": "missing", "index": index, "product": item.product, "locality": locality}
            return {"status": "ambiguous", "index": index, "query": item.product,
                "choices": candidates, "snapshot_id": str(snapshot.id),
                "date": snapshot.data_date,
                "locality": locality, "province": province}
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
    single_candidate_uncertain = False
    while result["status"] == "ambiguous" and len(result["choices"]) == 1:
        selected = await _semantic_candidates(result["query"], operation.original_text, result["choices"])
        if not selected:
            single_candidate_uncertain = selected is None
            result["choices"] = []
            break
        candidate = selected[0]
        choices[result["index"]] = BasketProductChoice(id=candidate["ean"],
            snapshot_id=result["snapshot_id"], operation_id=operation.operation_id,
            item_index=result["index"], query=result["query"], locality=result["locality"],
            province=result["province"], visible=candidate["name"])
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
            question=(f"No encontré «{result['product']}» entre los productos publicados para "
                f"{result['locality']}. No agregué nada a tu canasta. "
                "Podés probar con otro nombre o presentación.")),))
    if result["status"] == "stale_choice":
        return IncompleteOperation(missing_fields=(FieldRequest(field=f"items.{result['index']}.product_ref", reason="stale",
            question="La lista de productos cambió. Volvé a indicar el producto para buscar opciones actuales."),))
    if result["status"] in {"too_many_units", "too_many_products"}:
        raise ValueError("La canasta admite hasta 10 productos y 20 unidades de cada uno")
    if result["status"] == "ambiguous":
        choices = []
        uncertain = single_candidate_uncertain
        for start in range(0, len(result["choices"]), 30):
            selected = await _semantic_candidates(result["query"], operation.original_text,
                result["choices"][start:start + 30])
            if selected is None:
                uncertain = True
                break
            choices.extend(selected)
            if len(choices) >= 5:
                break
        result["choices"] = choices[:5]
        if not result["choices"]:
            return IncompleteOperation(missing_fields=(FieldRequest(
                field=f"items.{result['index']}.product", reason="ambiguous" if uncertain else "missing",
                question=((f"No puedo distinguir con seguridad qué tipo de {result['query']} buscás. "
                    "Decime el tipo, la marca o la presentación para afinar la búsqueda.") if uncertain else
                    (f"No encontré una presentación de «{result['query']}» que corresponda "
                    f"a tu pedido en el catálogo de {result['locality']}. "
                    "No agregué otro producto a tu canasta."))),))
        refs = []
        for candidate in result["choices"]:
            label = (f"{candidate['name'][:120]} — {_money(candidate['price'])} en "
                f"{candidate['chain'][:60]}, {candidate['branch'][:60]}")
            if candidate["promotion"]:
                label += (f" (promoción informada; lista {_money(candidate['list_price'])}. "
                    f"Condiciones: {candidate['promotion']})")
            refs.append(await context.reference("basket_product", BasketProductChoice(id=candidate["ean"],
                snapshot_id=result["snapshot_id"], operation_id=operation.operation_id,
                item_index=result["index"], query=result["query"], locality=result["locality"],
                province=result["province"], visible=candidate["name"], label=label),
                expires_at=operation.expires_at))
        return IncompleteOperation(missing_fields=(FieldRequest(field=f"items.{result['index']}.product_ref",
            reason="ambiguous", candidate_refs=tuple(refs),
            question=(f"Encontré varias opciones de {result['query']}, ordenadas por el menor precio "
                f"del paquete. Precios de {SOURCE} del {result['date']:%d/%m/%Y} en "
                f"{result['locality']}; confirmá el precio en la sucursal. ¿Cuál querés agregar? "
                "Respondé con el número o decime la marca y presentación para afinar la búsqueda.")),))
    items = result["items"]
    preview = "\n".join([f"Guardaría esta canasta para {result['locality']}:",
        *(f"{index}. {item['name']} × {item['quantity']}" for index, item in enumerate(items, 1))])
    return prepared(BasketPrepared(items=tuple(items), locality=result["locality"], province=result["province"]), preview)


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
        read_tool("find_shopping_basket_offers", "Consulta a pedido las promociones verificadas para los productos de la canasta, sin depender de alertas o límites de presupuesto; no confundir con comparar precios normales.", BasketSearch, find_basket_offers),
        operation_tool("save_shopping_basket", "Agrega por NOMBRE productos a la canasta frecuente, o la reemplaza si mode=replace. Usar también si pide 'agregar arroz a mi canasta, mostrame opciones con precio antes de guardar': product='arroz', locality y province si las dijo. Esta herramienta muestra opciones con precio y pide elegir antes de proponer guardar; nunca usar compare_supermarket_prices para este pedido ni pedir EAN. Si faltan localidad u opción concreta, el backend pregunta. Requiere propuesta y confirmación posterior.", BasketDraft, BasketPrepared, prepare_basket, commit_basket, schema_version=2, timeout_seconds=35),
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
        return [f"No hay promociones verificadas para tu canasta en {locality} con el dato SEPA del "
                f"{date.fromisoformat(result['date']):%d/%m/%Y}. Esto no significa que no haya precios: "
                "son consultas diferentes. Pedime comparar precios de mi canasta si querés verlos por sucursal."]
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
