"""Consolidación del patrimonio en ARS y USD (STK-45 / STK-217).

Solo agrega los saldos all-time de las billeteras del usuario y aplica las
cotizaciones que el caller ya obtuvo: no hace I/O de red ni escribe en la DB.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.services.currency import QuoteType, UsdQuote
from app.services.wallet import WalletService


class NetWorthStatus(str, Enum):
    OK = "ok"
    NO_WALLETS = "no_wallets"
    QUOTE_UNAVAILABLE = "quote_unavailable"


@dataclass(frozen=True)
class NetWorthResult:
    status: NetWorthStatus
    ars_total: Decimal
    usd_total: Decimal
    totals: dict[QuoteType, Decimal | None]
    quotes: dict[QuoteType, UsdQuote]
    missing: tuple[QuoteType, ...]
    stale: tuple[QuoteType, ...]
    other_totals: dict[str, Decimal] = field(default_factory=dict)


def calculate_net_worth(
    user_id: UUID | str,
    session=None,
    quotes: dict[QuoteType, UsdQuote] | None = None,
) -> NetWorthResult:
    balances = WalletService.get_balances_own_user(user_id, session=session)
    available = {QuoteType(tipo): q for tipo, q in (quotes or {}).items()}

    if not balances:
        return NetWorthResult(
            status=NetWorthStatus.NO_WALLETS,
            ars_total=Decimal("0"),
            usd_total=Decimal("0"),
            totals=dict.fromkeys(QuoteType, Decimal("0")),
            quotes={},
            missing=(),
            stale=(),
        )

    by_currency: dict[str, Decimal] = {}
    for balance in balances:
        currency = balance.currency.upper()
        by_currency[currency] = by_currency.get(currency, Decimal("0")) + balance.balance

    ars_total = by_currency.get("ARS", Decimal("0"))
    usd_total = by_currency.get("USD", Decimal("0"))
    other_totals = {
        code: total for code, total in by_currency.items()
        if code not in {"ARS", "USD"}
    }

    if usd_total == 0:
        return NetWorthResult(
            status=NetWorthStatus.OK,
            ars_total=ars_total,
            usd_total=usd_total,
            totals=dict.fromkeys(QuoteType, ars_total),
            quotes=available,
            missing=(),
            stale=(),
            other_totals=other_totals,
        )

    totals: dict[QuoteType, Decimal | None] = {}
    missing: list[QuoteType] = []
    stale: list[QuoteType] = []
    for tipo in QuoteType:
        quote = available.get(tipo)
        if quote is None or quote.compra is None:
            totals[tipo] = None
            missing.append(tipo)
            continue
        totals[tipo] = ars_total + usd_total * quote.compra
        if quote.stale:
            stale.append(tipo)

    status = (
        NetWorthStatus.OK
        if any(total is not None for total in totals.values())
        else NetWorthStatus.QUOTE_UNAVAILABLE
    )
    return NetWorthResult(
        status=status,
        ars_total=ars_total,
        usd_total=usd_total,
        totals=totals,
        quotes=available,
        missing=tuple(missing),
        stale=tuple(stale),
        other_totals=other_totals,
    )
