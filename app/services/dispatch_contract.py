"""Public transport result shared by the unified entry points and simulator."""
from dataclasses import dataclass, field
from typing import Any
from app.api.whatsapp import OutboundWhatsAppMessage
from app.services.budget import BudgetStatus


@dataclass
class DispatchResult:
    """Result of processing an incoming message."""
    reply_text: str
    raw_llm_response: dict | None = None
    service_invoked: str | None = None
    intent: str | None = None
    debug_info: dict = field(default_factory=dict)
    event_key: str | None = None
    event_variables: dict[str, Any] = field(default_factory=dict)
    reply_message: OutboundWhatsAppMessage | None = None
    clear_memory: bool = False
    followup_messages: list[OutboundWhatsAppMessage] = field(default_factory=list)
    budget_threshold_alerts: list[BudgetStatus] = field(default_factory=list)
    proposal_candidate_id: str | None = None
    proposal_delivery_mode: str | None = None  # "primary" or "followup"
    proposal_followup_message: OutboundWhatsAppMessage | None = None
    protected_presentation: bool = False
    assistant_preview_delivery: Any | None = None
    tool_trace: list[dict] = field(default_factory=list)
