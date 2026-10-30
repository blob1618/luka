"""Simulator ports: shared ingress/wrappers and explicit synthetic delivery, no Meta."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api.whatsapp import WhatsAppCTAURL, WhatsAppImage, WhatsAppReplyButtons, WhatsAppReplyButton, WhatsAppList, WhatsAppListSection, WhatsAppListRow
from app.services.conversation import ConversationHistoryService, ConversationService
from app.services.dispatch_contract import DispatchResult
from testing.services import webhook_mode
from testing.services.webhook_mode import WebhookModeResult, WebhookModeService
from tests.conftest import FakeRedis


@pytest.fixture
def redis_mocks(monkeypatch):
    client=FakeRedis()
    monkeypatch.setattr(ConversationService,'_get_client',AsyncMock(return_value=client))
    monkeypatch.setattr(webhook_mode,'_owner',Mock(return_value=uuid4()))
    monkeypatch.setattr(webhook_mode.assistant_state,'read',AsyncMock(return_value={'workspace':{'active_operation_id':None}}))
    dispatch=AsyncMock(return_value=DispatchResult('Registré5000ARS',service_invoked='assistant',tool_trace=[{'tool':'register_movement','outcome':'prepared'}]))
    monkeypatch.setattr(webhook_mode,'process_incoming_message',dispatch)
    return SimpleNamespace(client=client,dispatch=dispatch)


async def send():
    return await WebhookModeService().send_message('Gasté5000','12345','gemini','prompt.md')


class TestWebhookModeResult:
    def test_result_has_required_fields(self):
        result=WebhookModeResult(reply_text='ok',latency_ms=1,provider='gemini',prompt_path='prompt.md')
        assert result.tool_trace==[] and result.interactions==[] and result.image_pngs==[] and result.cta_urls==[]
        assert 'synthetic' in result.delivery and result.memory is None


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_purge_user_state_clears_workspace_claim_and_history(self, redis_mocks, monkeypatch):
        owner = uuid4()
        monkeypatch.setattr(webhook_mode, '_owner', Mock(return_value=owner))
        client = redis_mocks.client
        await client.set(f'assistant:v3:{owner}', 'state')
        await client.set(f'assistant:v3:{owner}:claim', 'token')
        await client.rpush('conversation_memory:whatsapp:12345', 'turn')

        deleted = await webhook_mode.purge_user_state('12345')

        assert deleted == [f'assistant:v3:{owner}', f'assistant:v3:{owner}:claim',
                           'conversation_memory:whatsapp:12345']
        assert await client.get(f'assistant:v3:{owner}') is None
    @pytest.mark.asyncio
    async def test_captures_chart_png_from_dispatch_result(self,redis_mocks):
        redis_mocks.dispatch.return_value=DispatchResult('Gráfico',reply_message=WhatsAppImage(b'PNG1',caption='Parte1'),followup_messages=[WhatsAppImage(b'PNG2',caption='Parte2')])
        result=await send()
        assert result.image_pngs==[b'PNG1',b'PNG2']
        assert 'Parte1' in result.reply_text and 'Parte2' in result.reply_text

    @pytest.mark.asyncio
    async def test_captures_cta_urls_from_dispatch_result(self,redis_mocks):
        redis_mocks.dispatch.return_value=DispatchResult('Acceso',reply_message=WhatsAppCTAURL('Acceso','Abrir','http://localhost/access'),followup_messages=[WhatsAppCTAURL('Detalle','Ver','http://localhost/detail')])
        assert (await send()).cta_urls==[('Abrir','http://localhost/access'),('Ver','http://localhost/detail')]

    @pytest.mark.asyncio
    async def test_plain_reply_has_no_media(self,redis_mocks):
        result=await send()
        assert not result.image_pngs and not result.cta_urls

    @pytest.mark.asyncio
    async def test_routes_through_dispatcher(self,redis_mocks):
        result=await send()
        kwargs=redis_mocks.dispatch.await_args.kwargs
        assert kwargs['sender_phone']=='12345' and kwargs['text_body']=='Gasté5000'
        assert kwargs['whatsapp_message_id'].startswith('simulated-') and kwargs['conversation_history']==[]
        assert result.service_invoked=='assistant' and result.tool_trace[0]['tool']=='register_movement'
        assert (await send()).message_id!=result.message_id

    @pytest.mark.asyncio
    async def test_measures_latency(self,redis_mocks):
        result=await send()
        assert result.latency_ms>=0 and result.metrics['status']=='completed'

    @pytest.mark.asyncio
    async def test_captures_redis_state(self,redis_mocks):
        assert (await send()).redis_state=={'workspace':{'active_operation_id':None}}

    @pytest.mark.asyncio
    async def test_handles_dispatcher_error(self,redis_mocks):
        redis_mocks.dispatch.side_effect=RuntimeError('synthetic private error')
        result=await send()
        assert result.error=='RuntimeError' and result.status=='failed'
        assert 'synthetic private error' not in result.reply_text and result.memory is None

    @pytest.mark.asyncio
    async def test_redis_state_read_failure_returns_error_dict(self,redis_mocks,monkeypatch):
        monkeypatch.setattr(webhook_mode.assistant_state,'read',AsyncMock(side_effect=RuntimeError('synthetic')))
        result=await send()
        assert result.status=='completed' and result.redis_state=={'error':'RuntimeError'}

    @pytest.mark.asyncio
    async def test_sets_model_via_env(self,redis_mocks,monkeypatch):
        monkeypatch.delenv('GEMINI_MODEL',raising=False)
        result=await WebhookModeService().send_message('test','12345','gemini','prompt.md',model='gemini-3.5-flash')
        import os
        assert os.environ['GEMINI_MODEL']=='gemini-3.5-flash' and result.model=='gemini-3.5-flash'


class TestConversationMemory:
    @pytest.mark.asyncio
    async def test_passes_recent_history_to_dispatcher(self,redis_mocks):
        await ConversationHistoryService.append_exchange(redis_mocks.client,'12345','hola','buenas',message_id='previous')
        result=await send()
        assert redis_mocks.dispatch.await_args.kwargs['conversation_history']==[{'role':'user','content':'hola'},{'role':'assistant','content':'buenas'}]
        assert [x['content'] for x in result.memory]==['hola','buenas','Gasté5000','Registré5000ARS']

    @pytest.mark.asyncio
    @pytest.mark.parametrize('ttl,expected',[(-2,None),(-1,None),(0,0)])
    async def test_memory_ttl_negative_is_none(self,redis_mocks,monkeypatch,ttl,expected):
        monkeypatch.setattr(redis_mocks.client,'ttl',AsyncMock(return_value=ttl))
        assert (await send()).memory_ttl_seconds==expected

    @pytest.mark.asyncio
    async def test_appends_exchange_with_visible_reply(self,redis_mocks):
        result=await send()
        assert result.memory==[{'role':'user','content':'Gasté5000'},{'role':'assistant','content':'Registré5000ARS'}]
        assert result.memory_ttl_seconds>0

    @pytest.mark.asyncio
    async def test_clear_memory_clears_without_appending(self,redis_mocks):
        await ConversationHistoryService.append_exchange(redis_mocks.client,'12345','anterior','respuesta')
        redis_mocks.dispatch.return_value.clear_memory=True
        assert (await send()).memory==[]

    @pytest.mark.asyncio
    async def test_redis_connection_failure_keeps_reply(self,redis_mocks,monkeypatch):
        # Unified ingress fails closed before execution when inbound idempotency is unavailable.
        monkeypatch.setattr(ConversationService,'_get_client',AsyncMock(side_effect=ConnectionError('synthetic')))
        result=await send()
        assert result.status=='failed' and result.error=='ConnectionError'
        redis_mocks.dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ping_failure_keeps_reply_and_closes_client(self,redis_mocks,monkeypatch):
        monkeypatch.setattr(redis_mocks.client,'set',AsyncMock(side_effect=ConnectionError('synthetic')))
        result=await send()
        assert result.status=='failed' and result.memory is None
        redis_mocks.dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_service_errors_keep_reply(self,redis_mocks,monkeypatch):
        monkeypatch.setattr(redis_mocks.client,'lrange',AsyncMock(side_effect=ConnectionError('synthetic')))
        result=await send()
        assert result.status=='completed' and result.reply_text=='Registré5000ARS' and result.memory==[]


class TestSimulatorInteractions:
    @pytest.mark.asyncio
    async def test_buttons_and_lists_use_same_interactive_entry(self,redis_mocks,monkeypatch):
        redis_mocks.dispatch.return_value=DispatchResult('Opciones',reply_message=WhatsAppReplyButtons('Elegí',(WhatsAppReplyButton('bound:yes','Confirmar'),)),
            followup_messages=[WhatsAppList('Otros','Abrir',(WhatsAppListSection((WhatsAppListRow('bound:row','Elemento'),)),))])
        result=await send()
        assert [o['reply_type'] for o in result.interactions]==['button_reply','list_reply']
        reply=AsyncMock(return_value=DispatchResult('Elegiste Elemento'))
        monkeypatch.setattr(webhook_mode,'process_incoming_interactive_reply',reply)
        option=result.interactions[1]
        selected=await WebhookModeService().send_message('','12345','gemini','prompt.md',option_id=option['id'],reply_type=option['reply_type'],title=option['title'])
        assert reply.await_args.kwargs['option_id']=='bound:row' and reply.await_args.kwargs['reply_type']=='list_reply'
        assert selected.memory[-2]['content']=='Elemento' and selected.status=='completed'

    @pytest.mark.asyncio
    async def test_actual_runtime_prompt_preview_delivery_and_confirmation(self,in_memory_db,test_user,monkeypatch,tmp_path):
        from app.models.database import AssistantWriteReceipt,Billetera
        from app.services import assistant_runtime
        from tests.test_assistant_pilot import NativeScript
        factory=in_memory_db['SessionLocal']
        user=test_user['user']
        session=test_user['session']
        user.auth_user_id=uuid4()
        session.add(Billetera(usuario_id=user.id,nombre='Caja',moneda='ARS'))
        session.commit()
        memory=FakeRedis()
        monkeypatch.setattr(ConversationService,'_get_client',AsyncMock(return_value=memory))
        observed=[]
        def plan(kwargs):
            observed.append(kwargs['messages'][0].content)
            return [('register_movement',{'economic_effect':'cash_flow','movement_type':'egreso','amount':'5000','currency':'ARS','description':'super','date_spec':{'kind':'absent'}})]
        monkeypatch.setattr(assistant_runtime,'create_provider',lambda:NativeScript([plan]))
        prompt=tmp_path/'agent.md'
        prompt.write_text('PROMPT_DE_ESCENARIO: usá las herramientas declaradas.',encoding='utf-8')
        preview=await WebhookModeService().send_message('Gasté5000',user.whatsapp_id,'gemini',str(prompt))
        assert observed==[prompt.read_text(encoding='utf-8')]
        assert factory().query(AssistantWriteReceipt).count()==0
        option=next(o for o in preview.interactions if o['title']=='Confirmar')
        confirmed=await WebhookModeService().send_message('',user.whatsapp_id,'gemini',str(prompt),option_id=option['id'],reply_type=option['reply_type'],title=option['title'])
        assert confirmed.status=='completed' and len(confirmed.memory)==4
        with factory() as check:
            assert check.query(AssistantWriteReceipt).count()==1
