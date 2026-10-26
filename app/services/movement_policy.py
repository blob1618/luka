"""Bounded registration policy for own principal placed in a fixed deposit.

This does not classify investments or infer an accounting entry. It rejects
explicit placements and requests that fail to distinguish principal from income;
the original message, rather than the model's description/type, is authoritative.
"""

import re

from app.services.reference_resolution import normalize_text


FIXED_DEPOSIT_REGISTRATION_REPLY = (
    "No registré movimientos. La colocación o renovación de capital propio en un "
    "plazo fijo no está soportada como ingreso ni egreso. "
    "Si cobraste intereses o pagaste un gasto, indicá ese importe por separado "
    "del capital."
)

_FIXED_DEPOSIT = re.compile(r"\bplazos?\s+fijos?\b")
_PLACED = re.compile(
    r"\b(?:meti|puse|coloque|constitui|renove|reinverti|inverti|deposite)\b"
    r"|\b(?:colocad|constituid|renovad|reinvertid|invertid|depositad)[oa]s?\b"
)
_PLACEMENT = re.compile(
    r"\b(?:colocacion|constitucion|renovacion|reinversion)\b"
    r"|\bdeposito\s+a\s+plazo\s+fijo\b"
)
_REGISTER_VERB = r"(?:anota(?:me|melo|lo)?|registra(?:me|melo|lo)?)"
_REGISTER = re.compile(rf"\b{_REGISTER_VERB}\b")
_RECEIVED = re.compile(r"\b(?:cobr[eé]|recib[ií]|acreditaron|pagaron)\b", re.IGNORECASE)
_PAID = re.compile(r"\b(?:pagu[eé]|gast[eé]|abon[eé]|me cobraron)\b", re.IGNORECASE)
_PRINCIPAL = re.compile(r"\b(?:capital|principal|sobrante|ahorros)\b")
_EARNINGS = re.compile(r"\b(?:interes(?:es)?|rendimientos?)\b")
_COST = re.compile(r"\b(?:comision(?:es)?|impuestos?|cargos?|costos?|gastos?|honorarios?)\b")
_EXPENSE_OBJECT = re.compile(r"\ben\s+(?P<object>.+?)\s+(?:para|por)\b")
_FUNDING_OBJECT = re.compile(
    r"\b(?:aporte|deposito|inversion|transferencia|colocar|constituir|renovar|"
    r"reinvertir|invertir|depositar|aportar|comprar|abrir)\b"
)
_PRINCIPAL_REQUEST = re.compile(
    rf"\b{_REGISTER_VERB}\s+(?:(?:el|la|un|una|mi|mis|ese|esa)\s+)?"
    r"(?:plazos?\s+fijos?|deposito\s+a\s+plazo\s+fijo|"
    r"colocacion|constitucion|renovacion|reinversion|capital|principal|sobrante|ahorros)\b"
)
_DEPENDENT_AMOUNT = re.compile(
    r"(?:\d+(?:\s+\d+)*(?:\s+(?:ars|usd|pesos|dolares|lucas|mil))*|"
    r"todo(?: eso)?|eso|el resto|lo restante)"
)


def _describes_existing_deposit(text: str, placement: re.Match) -> bool:
    """Describe the instrument of an already identified current cash flow."""
    deposits = list(_FIXED_DEPOSIT.finditer(text, 0, placement.start()))
    if not deposits:
        return False
    link = text[deposits[-1].end():placement.start()].split()
    participle = placement.group().endswith(("ado", "ada", "ados", "adas", "ido", "ida", "idos", "idas"))
    return participle or "que" in link


def _current_cash_flow(raw_clause: str) -> bool:
    """Bind a realized verb to its object, before instrument/history references.

    Preserve accents: "cobré" is a receipt, "que cobre al vencimiento" is not.
    An ambiguous unaccented verb cannot establish the exception by itself.
    """
    for verbs, concept in ((_RECEIVED, _EARNINGS), (_PAID, _COST)):
        for verb in verbs.finditer(raw_clause):
            if verb.group().casefold() in {"cobre", "recibi", "pague", "gaste", "abone"}:
                continue
            prefix = normalize_text(raw_clause[:verb.start()])
            if re.search(r"\bno$", prefix):
                continue
            if _REGISTER.search(prefix) and (
                _FIXED_DEPOSIT.search(prefix) or _PLACEMENT.search(prefix) or _PRINCIPAL.search(prefix)
            ):
                continue  # Registering principal cannot become a receipt in its history.
            if prefix.endswith(" que"):
                antecedent = prefix[:-4]
                if concept.search(antecedent) and not (
                    _FIXED_DEPOSIT.search(antecedent) or _PLACEMENT.search(antecedent)
                    or _PRINCIPAL.search(antecedent)
                ):
                    return True  # "Anotá 500 de intereses que cobré del plazo fijo".
            head = normalize_text(raw_clause[verb.end():])
            head = _REGISTER.split(head, maxsplit=1)[0].split(" que ", 1)[0]
            head = _FIXED_DEPOSIT.split(head, maxsplit=1)[0]
            if concept.search(head) and not _PRINCIPAL.search(head):
                return True
            if verbs is _PAID:
                expense = _EXPENSE_OBJECT.search(head)
                if expense and not any(
                    pattern.search(expense.group("object"))
                    for pattern in (
                        _FIXED_DEPOSIT, _PRINCIPAL, _PLACEMENT, _FUNDING_OBJECT, _EARNINGS,
                    )
                ):
                    # "en taxi para/por ..." names a distinct expense;
                    # paying an amount "por el plazo fijo" does not.
                    return True
    return False


def _unsupported_clause(raw_clause: str) -> bool:
    clause = normalize_text(raw_clause)
    placement_name = _PLACEMENT.search(clause)
    if not (_FIXED_DEPOSIT.search(clause) or placement_name or _PRINCIPAL.search(clause)):
        return False  # A verb about another object, e.g. "Renové mi seguro".
    if _PRINCIPAL_REQUEST.search(clause):
        return True
    cash_flow = _current_cash_flow(raw_clause)
    for placement in _PLACED.finditer(clause):
        if not (cash_flow and _describes_existing_deposit(clause, placement)):
            return True
    if cash_flow:
        return False
    return bool(
        _REGISTER.search(clause) or _RECEIVED.search(raw_clause) or _PAID.search(raw_clause)
        or placement_name or _PRINCIPAL.search(clause)
    )


def _dependent_placement(raw_clause: str) -> bool:
    """A coordinated placement can use the deposit elsewhere in its sentence.

    An amount/reference is eligible; an explicit other object such as "mi seguro"
    is not. This only rejects a request for clarification; it never supplies IDs.
    """
    # A comma retains the temporal prefix, unlike the conjunction splitter.
    # It still coordinates an action with the deposit in this sentence.
    clause = re.sub(r"^(?:despues|luego)\s+", "", normalize_text(raw_clause))
    placement = _PLACED.search(clause)
    if placement is None or clause[:placement.start()].strip() not in {"", "yo", "lo", "la", "los", "las"}:
        return False
    tail = clause[placement.end():].strip()
    if not tail:
        return True
    amount, separator, qualification = tail.partition(" con ")
    if separator and not _EARNINGS.search(qualification):
        return False
    return _DEPENDENT_AMOUNT.fullmatch(amount) is not None


def _registration_sentences(text: str) -> list[list[str]]:
    sentences = []
    for sentence in re.split(r"[.!?;]+(?=\s|$)", text):
        clauses = []
        for fragment in re.split(r"(?<!\d),|,(?!\d)", sentence):
            start = 0
            # A temporal connector still starts a new action; it must not inherit
            # the relative clause describing a previously constituted deposit.
            for conjunction in re.finditer(
                r"\b(?:y|e|pero|además|ademas)\b(?:\s+(?:después|despues|luego)\b)?",
                fragment, re.IGNORECASE,
            ):
                suffix = fragment[conjunction.end():].lstrip()
                subjectless = re.sub(r"^(?:yo|me)\s+", "", suffix, flags=re.IGNORECASE)
                normalized = normalize_text(subjectless)
                if (
                    _REGISTER.match(normalized) or _PLACED.match(normalized)
                    or _RECEIVED.match(subjectless) or _PAID.match(suffix)
                    or _PAID.match(subjectless)
                ):
                    clauses.append(fragment[start:conjunction.start()])
                    start = conjunction.end()
            clauses.append(fragment[start:])
        sentences.append(clauses)
    return sentences


def fixed_deposit_registration_reply(original_text: str) -> str | None:
    """Return a safe rejection only for the fixed-deposit registration boundary.

    An instrument mention alone is insufficient. Ordinary paid expenses and earnings
    remain registrable; an explicit principal placement takes precedence in a mixed
    request so no operation in that request can be persisted partially.
    """
    if not isinstance(original_text, str):
        return None
    if not _FIXED_DEPOSIT.search(normalize_text(original_text)):
        return None
    # Preserve raw clauses and accents. A salary receipt before a comma cannot
    # exempt principal after it; decimal/thousands commas remain inside amounts.
    # Split independent actions, not noun coordination such as "capital e intereses".
    for clauses in _registration_sentences(original_text):
        # An omitted placement object may precede or follow the named instrument.
        # Resolve that context for this sentence, without crossing its boundary.
        deposit_context = any(_FIXED_DEPOSIT.search(normalize_text(clause)) for clause in clauses)
        for clause in clauses:
            if deposit_context and _dependent_placement(clause):
                return FIXED_DEPOSIT_REGISTRATION_REPLY
            if _unsupported_clause(clause):
                return FIXED_DEPOSIT_REGISTRATION_REPLY
    return None
