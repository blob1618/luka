"""Own-wallet balance read adapter."""
import asyncio

from pydantic import Field

from app.services import finance
from app.services.assistant_contracts import (
    DataResult,
    EntityReference,
    FrozenModel,
    ToolData,
    amount_text,
)
from app.services.assistant_registry import ToolContract, ToolPolicy
from app.services.currencies import is_supported, supported_text
from app.services.wallet import WalletService


class WalletBalanceQuery(FrozenModel):
    currency: str | None = Field(default=None, title='la moneda', pattern=r'^[A-Z]{3}$')


def _balances(owner):
    with finance.SessionLocal() as session:
        return WalletService.get_balances_own_user(owner, session=session)


async def balances(context, args):
    if args.currency and not is_supported(args.currency):
        message = f'Moneda no soportada. Usá {supported_text()}.'
        return DataResult(data=ToolData(status='ok', items=[]).model_dump(mode='json'),
                          facts=context.presenter.add([message]))
    wallets = await asyncio.to_thread(_balances, context.turn.owner_id)
    if args.currency:
        wallets = [wallet for wallet in wallets if wallet.currency == args.currency]
    rows = []
    for wallet in wallets:
        ref = await context.reference('wallet', EntityReference(id=wallet.wallet_id, label=wallet.name))
        rows.append({'wallet_ref': ref, 'name': wallet.name, 'currency': wallet.currency, 'balance': str(wallet.balance)})
    if wallets:
        facts = context.presenter.add([f'{wallet.name}: {wallet.currency} {amount_text(wallet.balance)}.' for wallet in wallets])
    else:
        message = (f'No tenés una billetera en {args.currency}.' if args.currency
                   else 'No hay billeteras propias registradas.')
        facts = context.presenter.add([message])
    return DataResult(data=ToolData(status='ok', items=rows).model_dump(mode='json'), facts=facts)


def wallet_contracts():
    return (ToolContract('wallet_balances', 'Consulta saldos por billetera propia. Si el usuario pide una moneda, '
                         f'pasá currency (soportadas: {supported_text()}) para no mezclar monedas.',
                         WalletBalanceQuery, ToolData, balances, ToolPolicy('read')),)
