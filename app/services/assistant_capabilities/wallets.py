"""Own-wallet balance read adapter."""
import asyncio

from app.services import finance
from app.services.assistant_contracts import DataResult, EmptyInput, EntityReference, ToolData, amount_text
from app.services.assistant_registry import ToolContract, ToolPolicy
from app.services.wallet import WalletService


def _balances(owner):
    with finance.SessionLocal() as session:
        return WalletService.get_balances_own_user(owner, session=session)


async def balances(context, args):
    wallets = await asyncio.to_thread(_balances, context.turn.owner_id)
    rows = []
    for wallet in wallets:
        ref = await context.reference('wallet', EntityReference(id=wallet.wallet_id, label=wallet.name))
        rows.append({'wallet_ref': ref, 'name': wallet.name, 'currency': wallet.currency, 'balance': str(wallet.balance)})
    facts = context.presenter.add([f'{wallet.name}: {wallet.currency} {amount_text(wallet.balance)}.' for wallet in wallets]
                                  or ['No hay billeteras propias registradas.'])
    return DataResult(data=ToolData(status='ok', items=rows).model_dump(mode='json'), facts=facts)


def wallet_contracts():
    return (ToolContract('wallet_balances', 'Consulta saldos por billetera propia y moneda, sin conversión.',
                         EmptyInput, ToolData, balances, ToolPolicy('read')),)
