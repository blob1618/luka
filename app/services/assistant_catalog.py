"""The only composition root for currently supported capabilities."""
from app.services.assistant_capabilities.conversation import conversation_contracts
from app.services.assistant_capabilities.insights import insight_contracts
from app.services.assistant_capabilities.movements import movement_contracts
from app.services.assistant_capabilities.reminder_management import reminder_management_contracts
from app.services.assistant_capabilities.entities import entity_contracts
from app.services.assistant_capabilities.budgets import budget_contracts
from app.services.assistant_capabilities.movement_management import movement_management_contracts
from app.services.assistant_capabilities.financial_reads import financial_read_contracts
from app.services.assistant_capabilities.charts import chart_contracts
from app.services.assistant_capabilities.wallets import wallet_contracts
from app.services.assistant_registry import ToolRegistry
from app.services import finance
from app.services.movement_commands import receipt_result


def _movement_receipt(owner_id, **selector):
    return receipt_result(finance.SessionLocal, owner_id, **selector)


def build_tool_registry():
    registry = ToolRegistry(receipt_lookup=_movement_receipt)
    for contract in (*movement_contracts(), *insight_contracts(), *wallet_contracts(),
                     *conversation_contracts(), *reminder_management_contracts(),
                     *entity_contracts(), *budget_contracts(), *movement_management_contracts(),
                     *financial_read_contracts(), *chart_contracts()):
        registry.register(contract)
    return registry
