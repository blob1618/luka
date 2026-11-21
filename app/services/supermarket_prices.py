"""Source-bound price lookup and conservative same-product comparisons."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal
import json
import re

from sqlalchemy import func

from app.models.database import SepaPrice, SepaSnapshot, SessionLocal
from app.services import assistant_state

SOURCE = "SEPA / Precios Claros"
MAX_AGE_DAYS = 2
CACHE_SECONDS = 1800
_PROVINCE_ALIASES = {"chaco": "AR-H"}
_DATE_RANGE = re.compile(r"(?:desde\s+el\s+)?(\d{1,2}/\d{1,2}/\d{2,4}).{0,100}?(?:hasta\s+el\s+)(\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def normalize_province(province):
    if not province:
        return None
    value = province.strip()
    name = re.sub(r"^provincia\s+(?:de(?:l)?\s+)?", "", value, flags=re.I)
    return _PROVINCE_ALIASES.get(name.casefold(), value)


def _money(value):
    return f"${Decimal(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _verified_promo(price, legend, day):
    if not price or not legend or len(legend) > 180:
        return None
    legend = " ".join(legend.split())
    match = _DATE_RANGE.search(legend)
    if not match:
        return None
    try:
        start, end = (date.fromisoformat("-".join(reversed(value.split("/")))) for value in match.groups())
    except ValueError:
        return None
    if not (start <= day <= end):
        return None
    # Unit prices attached to multi-buy or conditional promotions cannot be
    # presented as the price for one item without meeting their conditions.
    if re.search(r"\b[2-9]\s*[xX]\s*[1-9]\b|segunda unidad|medio de pago|tarjeta|fidelidad|jubilad|estudiant", legend, re.I):
        return None
    return Decimal(price), legend


def _snapshot(session, today):
    row = session.query(SepaSnapshot).filter(SepaSnapshot.status == "ready").order_by(
        SepaSnapshot.data_date.desc()).first()
    if row is None:
        return None, "No hay precios SEPA cargados en este momento."
    if row.data_date > today or today - row.data_date > timedelta(days=MAX_AGE_DAYS):
        return None, f"El último dato SEPA es del {row.data_date:%d/%m/%Y}; no hay precios recientes para comparar."
    return row, None


def _matched_prices(session, snapshot, *, ean=None, name=None, locality, province=None):
    province = normalize_province(province)
    base = session.query(SepaPrice).filter(SepaPrice.snapshot_id == snapshot.id,
        func.lower(SepaPrice.locality) == locality.strip().lower())
    if province:
        base = base.filter(func.lower(SepaPrice.province) == province.strip().lower())
    if ean:
        base = base.filter(SepaPrice.product_id == ean, SepaPrice.is_ean.is_(True))
    elif name:
        words = [part for part in re.findall(r"\w+", name) if len(part) >= 2]
        if not words:
            return None
        if session.get_bind().dialect.name == "postgresql":
            base = base.filter(func.to_tsvector("spanish", SepaPrice.description).op("@@")(
                func.plainto_tsquery("spanish", name)))
        else:
            for word in words:
                base = base.filter(SepaPrice.description.ilike(f"%{word}%"))
        first_word = words[0].lower()
        starts_with_name = func.lower(SepaPrice.description).like(f"{first_word}%")
        if base.filter(starts_with_name).first() is not None:
            base = base.filter(starts_with_name)
    return base


def _query_rows(session, snapshot, *, ean=None, name=None, locality, province=None):
    base = _matched_prices(session, snapshot, ean=ean, name=name,
        locality=locality, province=province)
    if base is None:
        return [], {}
    if name:
        matches = base.filter(SepaPrice.is_ean.is_(True)).with_entities(
            SepaPrice.product_id, func.min(SepaPrice.description)).group_by(SepaPrice.product_id).order_by(
                func.min(SepaPrice.description), SepaPrice.product_id).limit(6).all()
        identities = {key: description for key, description in matches}
        if len(identities) != 1:
            return [], identities
        base = base.filter(SepaPrice.product_id == next(iter(identities)), SepaPrice.is_ean.is_(True))
    else:
        identities = {ean: ""} if ean else {}
    return base.order_by(SepaPrice.list_price).limit(200).all(), identities


def basket_candidates(session, snapshot, *, name, locality, province, today):
    """Price-ranked named presentations for semantic selection in the basket."""
    # Clarifications can contain details absent from catalog names. Search the
    # longest matching prefix, then let the semantic filter validate relevance.
    words = name.split()
    base = None
    for length in range(len(words), 0, -1):
        base = _matched_prices(session, snapshot, name=" ".join(words[:length]),
            locality=locality, province=province)
        if base is not None and base.filter(SepaPrice.is_ean.is_(True)).first() is not None:
            break
    if base is None:
        return []
    names = {}
    cheapest = {}
    for row in base.filter(SepaPrice.is_ean.is_(True)).all():
        names[row.product_id] = min(names.get(row.product_id, row.description), row.description)
        promo = _verified_promo(row.promo1_price, row.promo1_text, today)
        price = min(row.list_price, promo[0]) if promo else row.list_price
        candidate = {"ean": row.product_id, "price": price, "list_price": row.list_price,
            "chain": row.chain, "branch": row.branch, "promotion": promo[1] if promo and price < row.list_price else None}
        previous = cheapest.get(row.product_id)
        if previous is None or (candidate["price"], candidate["chain"], candidate["branch"]) < (
                previous["price"], previous["chain"], previous["branch"]):
            cheapest[row.product_id] = candidate
    return [{**candidate, "name": names[ean]} for ean, candidate in sorted(cheapest.items(),
        key=lambda item: (item[1]["price"], names[item[0]], item[0]))[:60]]


def _lookup(session, snapshot, query, locality, province, today):
    ean = query if re.fullmatch(r"\d{8}|\d{12}|\d{13}", query) else None
    rows, identities = _query_rows(session, snapshot, ean=ean, name=None if ean else query,
        locality=locality, province=province)
    if not ean and len(identities) > 1:
        return {"status": "ambiguous", "query": query,
                "options": [{"ean": key, "name": value} for key, value in list(identities.items())[:5]]}
    if not rows:
        return {"status": "missing", "query": query}
    chosen = ean or next(iter(identities))
    rows = [row for row in rows if row.product_id == chosen and row.is_ean]
    options = []
    for row in rows:
        promo = _verified_promo(row.promo1_price, row.promo1_text, today)
        effective = min(row.list_price, promo[0]) if promo else row.list_price
        options.append({"ean": chosen, "name": row.description, "chain": row.chain,
            "branch": row.branch, "price": str(effective), "list_price": str(row.list_price),
            "quantity": str(row.quantity) if row.quantity is not None else None, "unit": row.unit,
            "promotion": promo[1] if promo and effective < row.list_price else None,
            "date": row.report_date.isoformat(), "commerce_id": row.commerce_id,
            "branch_id": row.branch_id})
    options.sort(key=lambda value: (Decimal(value["price"]), value["chain"], value["branch"]))
    reference = max((Decimal(item["price"]) for item in options), default=None)
    presentations = {(item["quantity"], item["unit"], item["date"]) for item in options}
    if (len(options) >= 2 and len(presentations) == 1 and all(q is not None and u for q, u, _ in presentations)
            and reference is not None and reference > Decimal(options[0]["price"])):
        options[0]["saving_vs_highest"] = str(reference - Decimal(options[0]["price"]))
        options[0]["reference_price"] = str(reference)
    return {"status": "ok", "query": query, "ean": chosen, "options": options[:5]}


def _lookup_sync(queries, locality, province, today):
    with SessionLocal() as session:
        snapshot, reason = _snapshot(session, today)
        if snapshot is None:
            return {"status": "unavailable", "reason": reason}
        return {"status": "ok", "snapshot_id": str(snapshot.id), "date": snapshot.data_date.isoformat(),
                "items": [_lookup(session, snapshot, query, locality, province, today) for query in queries]}


async def compare_prices(queries, *, locality, province=None, today=None):
    if not locality or len(locality.strip()) < 2:
        return {"status": "needs_locality", "reason": "Decime la localidad donde querés comparar precios."}
    queries = [query.strip() for query in queries if query and query.strip()][:10]
    if not queries:
        return {"status": "needs_products", "reason": "Decime qué productos querés comparar."}
    today = today or date.today()
    province = normalize_province(province)
    # Snapshot id in the key ensures a new publication cannot reuse stale prices.
    with SessionLocal() as session:
        snapshot, reason = _snapshot(session, today)
        if snapshot is None:
            return {"status": "unavailable", "reason": reason}
        snapshot_id = str(snapshot.id)
    key = "sepa:compare:" + json.dumps([snapshot_id, today.isoformat(), locality.casefold(),
        province.casefold() if province else "", queries], ensure_ascii=False)
    try:
        client = await assistant_state.client()
        cached = await client.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        client = None
    result = await asyncio.to_thread(_lookup_sync, queries, locality, province, today)
    if client and result["status"] == "ok":
        try:
            await client.set(key, json.dumps(result, ensure_ascii=False), ex=CACHE_SECONDS)
        except Exception:
            pass
    return result


def present_comparison(result, *, locality):
    if result["status"] != "ok":
        return [result["reason"]]
    lines = [f"Archivo de {SOURCE} publicado el {date.fromisoformat(result['date']):%d/%m/%Y} para {locality}. No garantiza precio en góndola."]
    for item in result["items"]:
        if item["status"] == "missing":
            lines.append(f"{item['query']}: sin precio encontrado en esa localidad.")
        elif item["status"] == "ambiguous":
            lines.append(f"{item['query']}: encontré varias presentaciones:")
            lines.extend(f"{index}. {option['name'][:120]}" for index, option in enumerate(item["options"], 1))
            lines.append("Decime la marca o presentación que buscás.")
        else:
            options = item["options"]
            for option in options[:3]:
                line = (f"{option['name'][:120]} — {option['chain']}, {option['branch']}: "
                        f"{_money(option['price'])} por presentación (dato {option['date']}).")
                if option.get("promotion"):
                    line += f" Promoción informada: {option['promotion']}."
                if option.get("saving_vs_highest"):
                    line += f" Ahorro estimado {_money(option['saving_vs_highest'])} frente a {_money(option['reference_price'])} para el mismo producto y presentación."
                lines.append(line)
    return lines
