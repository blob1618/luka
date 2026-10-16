"""Ruta educativa determinista y de solo lectura (STK-243).

Reconoce pedidos explícitos de sobrante del mes cerrado y de orientación
general de ahorro/inversión. La clasificación es pura: no toca red, base de
datos ni LLM. Las cifras salen exclusivamente de
``monthly_balance.query_monthly_balance`` y el texto educativo del catálogo
versionado de ``investment_guidance``. Esta ruta nunca escribe movimientos,
billeteras ni presupuestos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.services.intent_routing import is_dashboard_access_candidate
from app.services.investment_guidance import (
    InvestmentGuidanceError,
    render_overview,
    render_topic,
)
from app.services.monthly_balance import MonthlyBalanceResult, query_monthly_balance


SERVICE_NAME = "educational_route"
INTENT_NAME = "educational_route"

DISCLAIMER = (
    "Estas cifras surgen únicamente de los movimientos registrados: pueden "
    "faltar movimientos no cargados o pagos futuros, y no demuestran que el "
    "dinero esté disponible."
)
POSITIVE_OFFER = (
    "Si querés, puedo ampliar con orientación general sobre ahorro e inversión "
    "(por ejemplo, reserva para imprevistos, plazo fijo o FCI)."
)
GUIDANCE_UNAVAILABLE = (
    "No pude cargar el contenido educativo en este momento. Intentá nuevamente "
    "en unos minutos."
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


# Señales de sobrante: los sustantivos y el verbo "sobrar" son inequívocos; el
# resto exige un período explícito para no confundirse con respuestas breves,
# importes sueltos, consultas de presupuesto o el registro de movimientos.
_SURPLUS_NOUNS = re.compile(r"\b(?:sobrante|excedente)\b")
_SURPLUS_VERBS = re.compile(r"\b(?:me\s+)?(?:sobro|sobraron|sobra|sobraba)\b")
_BALANCE_TERMS = re.compile(
    r"\b(?:balance|saldo|ahorro|ahorros|ahorre|ahorramos|quedo|quedaron)\b"
)
_PERIOD_TERMS = re.compile(
    r"\b(?:mes pasado|ultimo mes|mes anterior|"
    r"periodo pasado|mes cerrado|mensual)\b"
)
_EXPLICIT_MONTH_TERMS = re.compile(
    r"\b(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)\b"
)
_CATEGORY_BUDGET_TERMS = re.compile(
    r"\b(?:presupuestos?|limites?|topes?|categorias?)\b"
)

# Pedidos de un período abierto/actual o de varios meses: query_monthly_balance
# sólo devuelve el último mes cerrado, así que se dejan al flujo existente.
_OPEN_PERIOD_TERMS = re.compile(
    r"\b(?:este mes|mes actual|mes en curso|mes corriente|"
    r"en lo que va del mes|lo que va del mes|"
    r"proximo mes|mes que viene|ultimos meses|varios meses|meses)\b"
)

# Órdenes, registros o correcciones: un pedido con verbo de escritura sigue su
# flujo habitual y nunca se interpreta como consulta educativa.
_WRITE_VERBS = re.compile(
    r"\b(?:gaste|pague|compre|comprar|compro|compra|cobre|recibi|transferi|"
    r"envie|anota|anote|registra|registre|ingresa|ingrese|deposita|deposite|"
    r"extrae|extrai|inverti|invirtio|suscri|suscribi|vende|vendi|puse|pongo|"
    r"poner|coloque|coloca|borra|borre|elimina|elimine|modifica|modifique|"
    r"actualiza|actualice|cancela|cancele|rescata|rescate)\b"
)

# Una consulta educativa necesita una señal explícita de pregunta o pedido; el
# sustantivo o el tema sueltos ("mi sobrante", "plazo fijo") no alcanzan.
_EDUCATIONAL_SIGNALS = re.compile(
    r"\b(?:cuanto|cuanta|cuantos|cuantas|que|cual|cuales|como|donde|"
    r"quiero|quisiera|queria|necesito|revisar|reviso|ver|veo|mostrame|mostrar|"
    r"decime|deci|pasame|pasa|contame|contar|explicame|explicar|explica|"
    r"dame|da|compar\w*|diferencia|ayudame|ayuda|orientacion|opciones|ideas|"
    r"saber|entender|informacion|informame|calcular|calculame|significa|conviene)\b"
)

# Acciones de inversión sobre un instrumento concreto. Se aplican solo cuando el
# mensaje nombra un tema del catálogo, para no bloquear la orientación general
# ("quiero invertir") que no pide ejecutar nada.
_ACTION_INTENT = re.compile(
    r"\b(?:comprar|compro|compra|invertir|invierto|colocar|coloque|poner|pongo|"
    r"puse|suscribir|suscri|suscribo|adherir|adhiero|contratar|contrato|"
    r"solicitar|solicito|depositar|deposite)\b"
)

_COMPARISON_TERMS = re.compile(
    r"\bcompar\w*\b|\bdiferencia\b|\bconviene\b|\bmejor\b"
)
_PLAZO_FIJO = re.compile(r"\bplazo\s+fijo\b")
_FCI = re.compile(
    r"\b(?:fci|fondos?\s+comun(?:es)?\s+de\s+inversion|fondos?\s+de\s+inversion)\b"
)
_RESERVA = re.compile(r"\b(?:reserva|colchon|fondo)\b")
_IMPREVISTOS = re.compile(
    r"\b(?:imprevisto|imprevistos|emergencia|emergencias)\b"
)
_META = re.compile(
    r"\b(?:meta|objetivo)\s+(?:de\s+)?ahorro\b"
    r"|\b(?:ahorrar|ahorro)\s+para\s+(?:una\s+|un\s+|mi\s+)?(?:meta|objetivo)\b"
)

_OVERVIEW_ACTIONS = r"(?:(?:puedo|podria|deberia)\s+hacer|hago|hacer)"
_OVERVIEW_WITH_OBJECT = re.compile(
    rf"\bque\s+{_OVERVIEW_ACTIONS}\s+con\s+"
    rf"(?:(?:mi|mis|el|la|los|las|este|esta|estos|estas|ese|esa|esos|esas|un|una)\s+)?"
    rf"(?:dinero|plata|ahorro|ahorros|capital|sobrante|excedente)\b"
)
_OVERVIEW_WITH_PRONOUN = re.compile(
    rf"\bque\s+{_OVERVIEW_ACTIONS}\s+con\s+(?:eso|esto)\b"
)

_OVERVIEW_PATTERNS = (
    _OVERVIEW_WITH_OBJECT,
    re.compile(
        r"\b(?:como|de\s+que\s+manera)\s+(?:puedo\s+)?(?:empezar\s+a\s+|"
        r"arrancar\s+a\s+)?(?:ahorrar|invertir)\b"
    ),
    re.compile(r"\b(?:donde|en\s+que)\s+(?:puedo\s+)?invertir\b"),
    re.compile(
        r"\b(?:quiero|quisiera|me\s+gustaria|necesito)\s+(?:empezar\s+a\s+|"
        r"aprender\s+a\s+)?(?:ahorrar|invertir)\b"
    ),
    re.compile(r"\bopciones\s+(?:de|para)\s+(?:ahorro|inversion|invertir|ahorrar)\b"),
    re.compile(r"\borientacion\s+(?:sobre|de|en|financiera)\b"),
    re.compile(r"\b(?:quiero|necesito|dame|pasame)\s+orientacion\b"),
    re.compile(r"\bno\s+se\s+(?:como|en\s+que|donde)\s+(?:ahorrar|invertir)\b"),
    re.compile(r"\b(?:dame|mostrame|pasame)\s+(?:opciones|orientacion|ideas)\b"),
    re.compile(r"\b(?:ideas|opciones)\s+para\s+(?:ahorrar|invertir)\b"),
)


@dataclass(frozen=True)
class EducationalRequest:
    """Pedido educativo reconocido, sin efectos sobre el estado del usuario."""

    asks_balance: bool
    asks_overview: bool
    topic_id: str | None = None


def _topic_id(normalized: str) -> str | None:
    has_plazo_fijo = _PLAZO_FIJO.search(normalized) is not None
    has_fci = _FCI.search(normalized) is not None
    if _COMPARISON_TERMS.search(normalized) and has_plazo_fijo and has_fci:
        return "comparacion_plazo_fijo_fci"
    if has_plazo_fijo:
        return "plazo_fijo_tradicional"
    if has_fci:
        return "fci_mercado_dinero"
    if _RESERVA.search(normalized) and _IMPREVISTOS.search(normalized):
        return "reserva_imprevistos"
    if _META.search(normalized):
        return "meta_ahorro"
    return None


def classify_educational_request(text: str) -> EducationalRequest | None:
    """Clasificar un pedido educativo explícito, o ``None`` si es ambiguo.

    Es una función pura: solo normaliza y aplica patrones. Las respuestas
    breves (``sí``/``confirmar``), los importes sueltos, los nombres de
    categoría, el texto transaccional, la navegación al dashboard y los
    mensajes ambiguos devuelven ``None`` para conservar el flujo existente.
    """
    if not isinstance(text, str):
        return None
    normalized = _normalize(text)
    if len(normalized.split()) < 2:
        return None
    if _WRITE_VERBS.search(normalized):
        return None
    if is_dashboard_access_candidate(text):
        return None
    if _EDUCATIONAL_SIGNALS.search(normalized) is None:
        return None

    # Un pedido de período abierto/actual o de varios meses no puede calcularse
    # con query_monthly_balance (sólo cubre el último mes cerrado), así que no
    # habilita la ruta de saldo. Puede seguir siendo educación estática.
    open_period = _OPEN_PERIOD_TERMS.search(normalized) is not None
    unsupported_period = _EXPLICIT_MONTH_TERMS.search(normalized) is not None
    category_budget = _CATEGORY_BUDGET_TERMS.search(normalized) is not None
    surplus_signal = bool(
        _SURPLUS_NOUNS.search(normalized) or _SURPLUS_VERBS.search(normalized)
    )
    asks_balance = False if open_period or unsupported_period or category_budget else bool(
        surplus_signal
        or (_BALANCE_TERMS.search(normalized) and _PERIOD_TERMS.search(normalized))
    )
    topic_id = _topic_id(normalized)
    if topic_id is not None and _ACTION_INTENT.search(normalized):
        return None
    asks_overview = any(pattern.search(normalized) for pattern in _OVERVIEW_PATTERNS) or (
        surplus_signal and _OVERVIEW_WITH_PRONOUN.search(normalized) is not None
    )

    if not (asks_balance or asks_overview or topic_id):
        return None
    return EducationalRequest(
        asks_balance=asks_balance,
        asks_overview=asks_overview,
        topic_id=topic_id,
    )


def _format_amount(value: Decimal) -> str:
    quantized = Decimal(value).quantize(Decimal("0.01"))
    if quantized == 0:
        quantized = abs(quantized)
    formatted = f"{quantized:,.2f}"
    integer, _, decimals = formatted.partition(".")
    return f"{integer.replace(',', '.')},{decimals}"


def _format_period(result: MonthlyBalanceResult) -> str:
    inclusive_end = result.period_end - timedelta(days=1)
    return (
        f"{result.period_start.strftime('%d/%m/%Y')} al "
        f"{inclusive_end.strftime('%d/%m/%Y')}"
    )


def _has_positive_difference(result: MonthlyBalanceResult) -> bool:
    return any(balance.difference > 0 for balance in result.currencies)


def _render_balance(result: MonthlyBalanceResult) -> str:
    period = _format_period(result)
    if result.status == "user_not_found":
        return (
            f"No encuentro una cuenta vinculada a este número, así que no puedo "
            f"calcular el sobrante del período {period}."
        )
    if result.status == "no_data":
        return (
            f"No hay movimientos registrados en el período {period}. Sin registros "
            "no puedo calcular un sobrante y no lo reemplazo por cero."
        )
    if result.status == "error":
        return (
            f"No pude calcular el período {period} en este momento. Intentá "
            "nuevamente en unos minutos."
        )

    lines = [f"Movimientos registrados del mes cerrado ({period}):"]
    if _has_positive_difference(result):
        lines.append(
            "Hay un sobrante positivo registrado en al menos una moneda "
            "(sin convertir entre monedas)."
        )
    else:
        lines.append("No hay un sobrante positivo registrado en el período.")
    lines.append("")
    for balance in result.currencies:
        verdict = (
            "sobrante positivo registrado"
            if balance.difference > 0
            else "sin sobrante positivo registrado"
        )
        lines.append(
            f"- {balance.currency}: ingresos {_format_amount(balance.income)}, "
            f"egresos {_format_amount(balance.expense)}, "
            f"diferencia {_format_amount(balance.difference)} ({verdict})."
        )
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def _safe_guidance(topic_id: str | None) -> str:
    try:
        if topic_id is None:
            return render_overview()
        return render_topic(topic_id)
    except InvestmentGuidanceError:
        return GUIDANCE_UNAVAILABLE


def render_educational_reply(phone: str, request: EducationalRequest) -> str:
    """Construir la respuesta educativa usando solo fuentes deterministas."""
    sections: list[str] = []
    balance: MonthlyBalanceResult | None = None

    if request.asks_balance:
        balance = query_monthly_balance(phone)
        sections.append(_render_balance(balance))

    if request.topic_id is not None:
        sections.append(_safe_guidance(request.topic_id))
    elif request.asks_overview:
        sections.append(_safe_guidance(None))

    if (
        balance is not None
        and _has_positive_difference(balance)
        and not request.asks_overview
        and request.topic_id is None
    ):
        sections.append(POSITIVE_OFFER)

    return "\n\n".join(section for section in sections if section)
