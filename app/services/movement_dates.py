"""Validation of explicitly supplied movement dates, independent of the LLM."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.llm_contract import resolve_relative_date

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
_NUMERIC_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)(?!\d)")
_MONTHS = {name: index for index, name in enumerate((
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
), 1)}
_MONTHS["setiembre"] = 9
_NAMED_DATE = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(_MONTHS)
    + r")(?:\s+(?:de(?:l)?\s+)?(\d+))?\b", re.IGNORECASE,
)
_PERIOD_UNIT = r"(?:mes(?:es)?|semanas?|a[ñn]os?|quincenas?|trimestres?|semestres?|bimestres?|d[eé]cadas?)"
_PERIOD_REFERENCE = re.compile(
    r"\b(?:" + _PERIOD_UNIT + r"\s+(?:pasad[oa]s?|anteriores?|actual(?:es)?|pr[oó]xim[oa]s?|siguientes?|que\s+viene)"
    r"|(?:este|esta|estos|estas|pasad[oa]s?|anterior|pr[oó]xim[oa]s?|siguiente|[uú]ltim[oa]s?)\s+" + _PERIOD_UNIT
    + r"|hace\s+\w+\s+" + _PERIOD_UNIT + r")\b", re.IGNORECASE,
)
_MONTH_NAME = re.compile(r"\b(?:" + "|".join(_MONTHS) + r")\b", re.IGNORECASE)
_WEEKDAY_PATTERN = r"\b(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b"
_SUBJECT_PERIOD_PREFIX = re.compile(
    r"\b(?:el|la|los|las|un|una|unos|unas)\s+"
    r"(?!(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b)"
    r"\w+(?:\s+\w+){0,2}\s+(?:de|del|de\s+la|de\s+los|de\s+las)\s*$",
    re.IGNORECASE,
)
_PARTICIPLE_BEFORE_DATE = re.compile(
    r"\b(?!pasad[oa]s?\b)\w+(?:ad[oa]s?|id[oa]s?)\s+(?:el\s+)?"
    r"(?=(?:anteayer|ayer|hoy|ma[ñn]ana|antes\s+de\s+ayer|pasado\s+ma[ñn]ana|"
    r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|\d{1,2}[/-])\b)",
    re.IGNORECASE,
)


def argentina_today() -> date:
    return datetime.now(ARGENTINA_TZ).date()


def numeric_dates(text: str, today: date) -> list[tuple[str, date | None, bool]]:
    """Return recognized date tokens, parsed date and whether the year was omitted."""
    dates = []
    for match in _NUMERIC_DATE.finditer(text):
        token = match.group()
        parts = re.split(r"[/-]", token)
        no_year = len(parts) == 2
        try:
            if len(parts[0]) == 4:
                year, month, day = map(int, parts)
            else:
                day, month = map(int, parts[:2])
                year = today.year if no_year else int(parts[2])
                if not no_year and len(parts[2]) != 4:
                    raise ValueError("year must be unambiguous")
            value = date(year, month, day)
        except ValueError:
            value = None
        dates.append((match.start(), token, value, no_year))
    for match in _NAMED_DATE.finditer(text):
        day, month, year = match.groups()
        try:
            if year is not None and len(year) != 4:
                raise ValueError("year must be unambiguous")
            value = date(int(year) if year else today.year, _MONTHS[month.casefold()], int(day))
        except ValueError:
            value = None
        dates.append((match.start(), match.group(), value, year is None))
    return [(token, value, no_year) for _, token, value, no_year in sorted(dates)]


def relative_dates(text: str, today: date) -> list[date]:
    """Extract recognizable references, distinguishing a morning from tomorrow."""
    normalized = text.casefold()
    matched = []
    compounds = [
        (r"\bantes\s+de\s+ayer\b", today - timedelta(days=2)),
        (r"\bpasado\s+ma[ñn]ana\b", today + timedelta(days=2)),
    ]
    for pattern, value in compounds:
        matches = list(re.finditer(pattern, normalized))
        matched.extend(value for _ in matches)
        normalized = re.sub(pattern, lambda match: " " * len(match.group()), normalized)
    references = [
        (r"\banteayer\b", today - timedelta(days=2)),
        (r"\bayer\b", today - timedelta(days=1)),
        (r"\bhoy\b", today),
        (r"\besta\s+ma[ñn]ana\b", today),
        (r"(?<!la )(?<!esta )\bma[ñn]ana\b", today + timedelta(days=1)),
    ]
    matched.extend(value for pattern, value in references if re.search(pattern, normalized))
    for match in re.finditer(_WEEKDAY_PATTERN, normalized):
        previous = resolve_relative_date(match.group(), today)
        before, after = normalized[:match.start()], normalized[match.end():]
        upcoming = (
            re.search(r"\b(?:pr[oó]xim[oa]|siguiente)\s*$", before)
            or re.match(r"\s+(?:que\s+viene|pr[oó]xim[oa]|de\s+la\s+pr[oó]xima\s+semana)\b", after)
        )
        if upcoming:
            # The resolver chooses the previous occurrence, so adding one week
            # always yields the next occurrence, including when today matches.
            next_occurrence = previous + timedelta(days=7)
            if next_occurrence <= today:
                next_occurrence += timedelta(days=7)
            matched.append(next_occurrence)
        elif re.search(r"\b(?:este|esta)\s*$", before):
            matched.append(today - timedelta(days=today.weekday()) + timedelta(days=previous.weekday()))
        else:
            matched.append(previous)
    return matched


def incomplete_period(text: str, precision: str | None, today: date) -> bool:
    """Require an actual day when the extracted temporal precision is a period."""
    if isinstance(precision, str) and precision in {"periodo", "ambigua"}:
        return True
    if numeric_dates(text, today):
        return False
    periods = [*_PERIOD_REFERENCE.finditer(text), *_MONTH_NAME.finditer(text)]
    if not periods:
        return False
    if re.search(_WEEKDAY_PATTERN, text, re.IGNORECASE):
        return True
    relatives = relative_dates(text, today)
    if len(relatives) != 1:
        return True
    for period in periods:
        prefix = text[:period.start()]
        if relative_dates(prefix, today) != relatives or not _SUBJECT_PERIOD_PREFIX.search(prefix):
            return True
    return False


def movement_temporal_text(text: str, today: date) -> str | None:
    """Require clarification when a relative clause may date another action."""
    if _PARTICIPLE_BEFORE_DATE.search(text):
        return None
    for marker in re.finditer(r"\bque\b", text, re.IGNORECASE):
        after = text[marker.end():]
        boundary = re.search(r"[.!?;]", after)
        end = marker.end() + (boundary.start() if boundary else len(after))
        clause = text[marker.end():end]
        if not (numeric_dates(clause, today) or relative_dates(clause, today)
                or incomplete_period(clause, None, today)):
            continue
        return None
    return text


def interpret_date(
    text: str, raw_date: str | None, today: date, precision: str | None = None,
) -> tuple[date | None, bool]:
    """Prefer one explicit numeric date over a provider's inference."""
    tokens = numeric_dates(text, today)
    relatives = relative_dates(text, today)
    if incomplete_period(text, precision, today):
        return None, False
    if not tokens and not relatives and (raw_date is not None or precision == "dia"):
        return None, False
    if len(tokens) > 1 or (tokens and relatives):
        return None, False
    if tokens:
        return tokens[0][1], tokens[0][2]
    normalized = text.casefold()
    if len(relatives) > 1:
        return None, False
    if relatives:
        if raw_date is not None and resolve_relative_date(raw_date, today) != relatives[0]:
            return None, False
        return relatives[0], False
    if raw_date is None:
        if re.search(r"\b(?:fecha|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b", normalized):
            return None, False
    resolved = resolve_relative_date(raw_date, today)
    months = r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre"
    no_year = bool(re.search(rf"\b(?:{months})\b", normalized)) and not re.search(r"\b\d{4}\b", normalized)
    if resolved and no_year:
        try:
            resolved = resolved.replace(year=today.year)
        except ValueError:
            resolved = None
    return resolved, bool(no_year)
