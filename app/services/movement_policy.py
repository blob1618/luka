"""Registration boundary on explicit semantic data, independent of language."""
from typing import Literal

EconomicEffect = Literal['cash_flow', 'investment_principal', 'placement', 'existing_investment']

UNSUPPORTED_PRINCIPAL_REPLY = (
    'No registré movimientos. La colocación, renovación o existencia de capital propio '
    'en una inversión no se registra como ingreso ni egreso. '
    'Los intereses cobrados y gastos o comisiones pagados se registran por separado del capital.'
)


def registration_policy_reply(effect: EconomicEffect | None) -> str | None:
    if effect == 'cash_flow':
        return None
    if effect is None:
        return 'Necesito aclarar si el importe es un flujo de dinero cobrado/pagado o capital de una inversión.'
    return UNSUPPORTED_PRINCIPAL_REPLY
