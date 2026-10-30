"""Query HTTP ports retain backend filters, financial immutability and safe links."""
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models.database import AssistantWriteReceipt,MovimientoFinanciero
from app.api.whatsapp import WhatsAppCTAURL
from app.services import assistant_runtime
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_pilot import PHONE,NativeScript,present_all
from tests.test_webhook import make_text_message,make_webhook_payload
accounts=scenarios.accounts
ingress=scenarios.ingress
client=TestClient(app)


@pytest.mark.parametrize('period,currency,has_link',[(True,'ARS',True),(False,'ARS',False),(True,'USD',False)])
def test_webhook_query_movements_offers_dashboard_link_only_for_period_and_more_rows(accounts,ingress,monkeypatch,period,currency,has_link):
    sent=AsyncMock(return_value=True)
    monkeypatch.setattr('app.main.send_whatsapp_message',sent)
    monkeypatch.setattr('app.main._dispatch_whatsapp_typing_indicator',lambda *args:None)
    selector={'kind':'month','month':'2026-10'} if period else {'kind':'all'}
    script=NativeScript([[('find_movements',{'selector':selector,'currency':currency,'sort_by':'date_desc','limit':5})],present_all])
    monkeypatch.setattr(assistant_runtime,'create_provider',lambda:script)
    with accounts[0]() as session:
        before=[(r.id,r.cantidad,r.descripcion,r.fecha_movimiento,r.anulado_en) for r in session.query(MovimientoFinanciero).order_by(MovimientoFinanciero.id)]
    response=client.post('/webhook',json=make_webhook_payload([make_text_message('Gastos','query-http',PHONE)]))
    assert response.status_code==200
    links=[call.args[1] for call in sent.await_args_list if isinstance(call.args[1],WhatsAppCTAURL)]
    assert bool(links)==has_link
    with accounts[0]() as session:
        assert [(r.id,r.cantidad,r.descripcion,r.fecha_movimiento,r.anulado_en) for r in session.query(MovimientoFinanciero).order_by(MovimientoFinanciero.id)]==before
        assert session.query(AssistantWriteReceipt).count()==0
    assert 'privado' not in str(sent.await_args_list)
