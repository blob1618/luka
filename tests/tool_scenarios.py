"""Test-only explicit plans; no text interpretation, classification or production aliases."""
from contextlib import ExitStack
import importlib
from unittest.mock import patch
from uuid import uuid4

from app.services import assistant_runtime, dispatcher
from tests.test_assistant_pilot import NOW, NativeScript, present_all


async def execute_tool(phone, name, arguments, *, confirm=False, text='Pedido de prueba', now=NOW, factory=None, message_id=None):
    """Use real ingress; explicitly simulate preview text delivery before approval.

    Returned commit reply has not been delivered: delivery/cooldown tests send it
    through the real inbound wrapper with their own synthetic send callback.
    """
    script=NativeScript([arguments if callable(arguments) else [(name,arguments)],present_all])
    original_context=assistant_runtime.make_context
    with ExitStack() as stack:
        stack.enter_context(patch.object(assistant_runtime,'create_provider',return_value=script))
        stack.enter_context(patch.object(assistant_runtime,'make_context',lambda owner,phone,mid,text,clock,state,**kw:
            original_context(owner,phone,mid,text,clock or now,state,**kw)))
        if factory:
            for module in ('finance','onboarding','wallet','reminder','recurring_expense','limit','budget','category_creation','compensation','chart_query','monthly_balance','conversation_flow','assistant_capabilities.movement_effects'):
                stack.enter_context(patch.object(importlib.import_module('app.services.'+module),'SessionLocal',factory))
        result=await dispatcher.process_incoming_message(phone,text,message_id or 'typed-'+uuid4().hex)
        if confirm and result.assistant_preview_delivery:
            await assistant_runtime.record_preview_delivered(phone,result.assistant_preview_delivery,now=now)
            from app.api.whatsapp import WhatsAppReplyButtons
            button=next(button.id for message in [result.reply_message,*result.followup_messages]
                if isinstance(message,WhatsAppReplyButtons) for button in message.buttons if button.id.startswith('assistant:confirm:'))
            result=await dispatcher.process_incoming_interactive_reply(sender_phone=phone,option_id=button,reply_type='button_reply',whatsapp_message_id='typed-confirm-'+uuid4().hex)
        return result
