"""Published journey validation plus independent domain category service."""
import pytest
from sqlalchemy import select
from app.models.database import LimiteCategoria
from app.services.conversation_flow import ConversationFlowService
from app.services.conversation_flow_contract import ConversationFlowDefinitionInvalid,validate_flow_definition
from app.services.limit_flow_journey import LIMIT_JOURNEY,limit_journey_definition
from tests.test_assistant_stage5 import accounts as _accounts_fixture,ingress as _ingress_fixture
accounts=_accounts_fixture
ingress=_ingress_fixture
PHONE='5491100260001'

@pytest.fixture
def environment(accounts,ingress):
    flow=ConversationFlowService.create(slug='limites',name='Límites',event_key=LIMIT_JOURNEY,definition=limit_journey_definition())
    ConversationFlowService.publish(flow.id)
    return accounts[0],None

def definition(body, actions=()):
    node = {"id": "message", "body": body, "type": "text"}
    if actions:
        node.update(type="reply_button", options=[
            {"id": action, "title": label, "action": action}
            for action, label in actions
        ])
    return {"start_node": "message", "nodes": [node]}

def test_journey_keeps_event_permissions_and_rejects_skipping_confirmation():
    journey = limit_journey_definition()
    node = next(n for n in journey['nodes'] if n['id'] == 'started')
    node['options'][0]['action'] = 'confirm_limit_category'
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)
    node['options'][0].pop('action')
    node['options'][0]['next_node'] = 'created'
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)

def test_journey_rejects_missing_stage_and_wrong_variables():
    journey = limit_journey_definition()
    journey['event_nodes'].pop('limit.created')
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)
    journey = limit_journey_definition()
    journey['nodes'][1]['body'] = '{amount}'  # Amount is unavailable at the start.
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)

def test_journey_publishes_all_messages_and_examples_together(environment):
    from app.services.conversation_flow import ConversationFlowConflict
    flow = next(f for f in ConversationFlowService.list() if f.event_key == LIMIT_JOURNEY)
    journey = flow.published.definition
    journey['response_examples']['datos'] = 'Transporte, 50000, noviembre'
    journey['nodes'][1]['body'] = 'Indicá categoría, monto y mes juntos.'
    ConversationFlowService.save_draft(flow.id, definition=journey)
    assert ConversationFlowService.get(flow.id).published.definition['nodes'][1]['body'] != journey['nodes'][1]['body']
    published = ConversationFlowService.publish(flow.id)
    assert published.published.definition['response_examples']['datos'] == 'Transporte, 50000, noviembre'
    for event in journey['event_nodes']:
        assert ConversationFlowService.find_published_by_event(event).id == flow.id
    competing = ConversationFlowService.create(
        slug='competidor', name='Competidor', event_key='limit.created',
        definition=definition('Guardado'),
    )
    with pytest.raises(ConversationFlowConflict):
        ConversationFlowService.publish(competing.id)

def test_category_service_can_be_used_without_a_parent_operation(environment):
    from app.models.database import MovimientoFinanciero
    from app.services.category_creation import CategoryCreationService
    sessions, _ = environment
    created = CategoryCreationService.confirm(PHONE, 'Mascotas')
    existing = CategoryCreationService.confirm(PHONE, 'Mascotas')
    assert (created.status, existing.status) == ('created', 'already_exists')
    assert created.category_id == existing.category_id
    with sessions() as session:
        assert len(session.scalars(select(MovimientoFinanciero)).all()) == 10
        assert session.scalar(select(LimiteCategoria)) is None
