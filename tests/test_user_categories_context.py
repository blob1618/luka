"""Explicit tool scenarios preserve financial effects after the global cut."""
import json
import pytest
from app.models.database import Categoria
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
async def test_build_user_context_lists_active_categories(ingress,accounts):
    observed=[]
    with accounts[0]() as session:
        session.add_all([Categoria(usuario_id=accounts[1],nombre='Activa'),Categoria(usuario_id=accounts[1],nombre='Borrada',esta_eliminado=True),Categoria(usuario_id=accounts[2],nombre='Privada')])
        session.commit()
    def plan(kwargs):
        observed.append(initial(kwargs))
        return [('conversation_reply',{'kind':'help'})]
    await ingress.send('Ayuda',steps=[plan])
    names={c['name'] for c in observed[0]['categories']}
    assert names=={'Comida','Activa'}
    assert str(accounts[1]) not in json.dumps(observed) and str(accounts[2]) not in json.dumps(observed)


@pytest.mark.asyncio
async def test_build_user_context_no_categories_only_date(ingress,accounts):
    with accounts[0]() as session:
        session.query(Categoria).filter_by(usuario_id=accounts[1]).update({'esta_eliminado':True})
        session.commit()
    def plan(kwargs):
        body=json.loads(next(m.content for m in kwargs['messages'] if m.role=='user'))
        assert body['date'] and body['context']['categories']==[]
        return [('conversation_reply',{'kind':'help'})]
    await ingress.send('Ayuda',steps=[plan])
