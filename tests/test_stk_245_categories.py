"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import Categoria, MovimientoFinanciero
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import visible_ref
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
async def test_explicit_create_and_rename_category_intents_use_owned_finance_service(ingress,accounts):
    preview=await ingress.tool('create_category',{'name':'Jardinería'})
    await ingress.confirm(preview)
    def rename(kwargs):return [('rename_category',{'category_ref':next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Jardinería'),'name':'Plantas'})]
    preview=await ingress.send('Renombrar',steps=[rename])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(Categoria).filter_by(usuario_id=accounts[1],nombre='Plantas').count()==1
        assert session.query(Categoria).filter_by(usuario_id=accounts[2],nombre='Plantas').count()==0


@pytest.mark.asyncio
async def test_reclassification_by_por_keeps_last_registered_movement_route(ingress,accounts):
    await ingress.tool('find_movements',{'selector':{'kind':'all'},'sort_by':'date_desc','limit':1})
    with accounts[0]() as session:
        other=Categoria(usuario_id=accounts[1],nombre='Transporte')
        session.add(other)
        session.commit()
        count=session.query(MovimientoFinanciero).count()
    def reclassify(kwargs):return [('update_movement',{'movement_ref':visible_ref(kwargs,'movement'),'changes':{'category_ref':next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Transporte')}})]
    preview=await ingress.send('Cambiar categoría por Transporte',steps=[reclassify])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==count
        assert session.query(Categoria).filter_by(usuario_id=accounts[1]).count()==2
        changed=session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[1],descripcion='gasto 7').one()
        assert session.get(Categoria,changed.categoria_id).nombre=='Transporte' and changed.cantidad==107


@pytest.mark.asyncio
async def test_category_rename_confirmation_never_applies_rejection_or_new_target(ingress,accounts):
    await ingress.send('Renombrar', steps=[lambda kw: [('rename_category', {'category_ref': initial(kw)['categories'][0]['ref'], 'name': 'No aplicar'})]])
    await ingress.send('cancelar')
    with accounts[0]() as session:
        assert session.get(Categoria,accounts[4]).nombre=='Comida'
        assert session.query(Categoria).filter_by(nombre='No aplicar').count()==0
