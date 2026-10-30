"""Explicit synthetic delivery through the production dispatcher and inbound wrappers."""
import asyncio
import os
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.api.whatsapp import (
    InboundInteractiveReply, WhatsAppCTAURL, WhatsAppImage, WhatsAppList, WhatsAppReplyButtons,
)
from app.services import assistant_state
from app.services.assistant_runtime import _owner
from app.services.conversation import ConversationHistoryService, ConversationService, _memory_key
from app.services.dispatcher import process_incoming_interactive_reply, process_incoming_message
from app.services.llm import LLMService
from app.services.telemetry import start_message_telemetry, finish_message_telemetry
from app.services.webhook_idempotency import (
    _visible_reply_text, process_interactive_message_once, process_text_message_once,
)
from testing.config.settings import set_model_env


async def purge_user_state(phone: str) -> list[str]:
    """Delete the simulated user's workspace, claim and visible history from Redis."""
    owner = await asyncio.to_thread(_owner, phone)
    client = await ConversationService._get_client()
    keys = []
    if owner is not None:
        keys.extend([assistant_state.key(owner), assistant_state.key(owner) + ':claim'])
    keys.append(_memory_key(phone))
    await client.delete(*keys)
    return keys


@dataclass
class WebhookModeResult:
    reply_text: str
    latency_ms: float
    provider: str
    prompt_path: str
    redis_state: dict | None = None
    service_invoked: str | None = None
    model: str = ''
    message_id: str = ''
    status: str = 'completed'
    tool_trace: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    memory: list[dict[str, str]] | None = None
    memory_ttl_seconds: int | None = None
    image_pngs: list[bytes] = field(default_factory=list)
    cta_urls: list[tuple[str, str]] = field(default_factory=list)
    interactions: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    delivery: str = 'synthetic: successful local capture; no WhatsApp/Meta request'


def interaction_options(messages):
    options = []
    for message in messages:
        if isinstance(message, WhatsAppReplyButtons):
            options.extend({'id': item.id, 'title': item.title, 'reply_type': 'button_reply'} for item in message.buttons)
        elif isinstance(message, WhatsAppList):
            options.extend({'id': item.id, 'title': item.title, 'reply_type': 'list_reply'}
                           for section in message.sections for item in section.rows)
    return options


class WebhookModeService:
    async def send_message(self, text, phone, provider, prompt_path, model='', *, option_id=None,
                           reply_type='button_reply', title=None):
        os.environ['LLM_PROVIDER'] = provider
        set_model_env(provider, model)
        LLMService.reset_provider()
        LLMService.set_prompt_path(prompt_path)
        message_id = 'simulated-' + uuid4().hex
        start = time.perf_counter()
        start_message_telemetry(message_id)
        captured, sent = None, []
        status, error = 'failed', None
        memory, memory_ttl, state = None, None, None
        try:
            client = await ConversationService._get_client()
            async def process_text(**kwargs):
                nonlocal captured
                captured = await process_incoming_message(**kwargs)
                return captured
            async def process_reply(**kwargs):
                nonlocal captured
                captured = await process_incoming_interactive_reply(**kwargs)
                return captured
            async def deliver_synthetically(sender_phone, message):
                sent.append(message)
                return True
            if option_id:
                status = await process_interactive_message_once(redis_client=client,
                    interactive_reply=InboundInteractiveReply(message_id, phone, reply_type, option_id, title),
                    process_reply=process_reply, send_message=deliver_synthetically)
            else:
                status = await process_text_message_once(redis_client=client, sender_phone=phone, text_body=text,
                    whatsapp_message_id=message_id, process_message=process_text, send_message=deliver_synthetically)
            memory = [item.to_dict() for item in await ConversationHistoryService.get_recent(client, phone)]
            ttl = int(await client.ttl(_memory_key(phone)))
            memory_ttl = ttl if ttl >= 0 else None
            try:
                owner = await asyncio.to_thread(_owner, phone)
                state = await assistant_state.read(owner) if owner else None
            except Exception as exc:
                state = {'error': type(exc).__name__}
        except Exception as exc:
            error = type(exc).__name__
        metrics = finish_message_telemetry(status) or {}
        return WebhookModeResult(
            reply_text=(_visible_reply_text(captured) or '') if captured and status == 'completed'
                       else 'No se pudo completar la entrega simulada. Volvé a intentar.',
            latency_ms=(time.perf_counter() - start) * 1000, provider=provider, prompt_path=prompt_path,
            redis_state=state, service_invoked=getattr(captured, 'service_invoked', None), model=model,
            message_id=message_id, status=status, tool_trace=getattr(captured, 'tool_trace', []),
            metrics=metrics, memory=memory, memory_ttl_seconds=memory_ttl, error=error,
            image_pngs=[item.content for item in sent if isinstance(item, WhatsAppImage)],
            cta_urls=[(item.display_text, item.url) for item in sent if isinstance(item, WhatsAppCTAURL)],
            interactions=interaction_options(sent))
