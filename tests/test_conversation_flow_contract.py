import pytest

from app.services.conversation_flow_contract import (
    ConversationFlowDefinitionInvalid,
    available_contract,
    validate_flow_definition,
)


def text_definition(body="Registré {description}."):
    return {
        "start_node": "done",
        "nodes": [
            {"id": "done", "type": "text", "body": body, "terminal": True}
        ],
    }


def button_definition(**option_overrides):
    option = {
        "id": "confirm",
        "title": "Confirmar",
        "action": "confirm_category",
    }
    option.update(option_overrides)
    return {
        "start_node": "question",
        "nodes": [
            {
                "id": "question",
                "type": "reply_button",
                "body": "¿Usamos {category}?",
                "options": [option],
            }
        ],
    }


def issue_messages(exc_info):
    return [issue.message for issue in exc_info.value.issues]


def test_valid_text_is_normalized():
    definition = validate_flow_definition(
        "movement.registered",
        text_definition(),
    )

    assert definition["nodes"][0]["terminal"] is True


def test_valid_allowed_action_is_accepted():
    definition = validate_flow_definition(
        "category.confirmation_required",
        button_definition(),
    )

    assert definition["nodes"][0]["options"][0]["action"] == "confirm_category"


def test_unknown_event_is_rejected():
    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition("unknown.event", text_definition())

    assert "no esta habilitado" in issue_messages(exc_info)[0]


def test_variable_not_owned_by_event_is_rejected():
    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition(
            "movement.registered",
            text_definition("{login_url}"),
        )

    assert "no esta permitida" in issue_messages(exc_info)[0]


def test_arbitrary_action_is_rejected():
    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition(
            "category.confirmation_required",
            button_definition(action="run_sql"),
        )

    assert "accion no esta permitida" in issue_messages(exc_info)[0]


def test_literal_url_is_rejected():
    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition(
            "dashboard.link.sent",
            text_definition("Entrá en https://internal.example/admin"),
        )

    assert "URLs deben llegar" in issue_messages(exc_info)[0]


def test_backend_url_variable_is_accepted():
    definition = validate_flow_definition(
        "dashboard.link.sent",
        text_definition("Entrá en {login_url}"),
    )

    assert definition["nodes"][0]["body"] == "Entrá en {login_url}"


def test_missing_start_node_is_rejected():
    invalid = text_definition()
    invalid["start_node"] = "missing"

    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition("movement.registered", invalid)

    assert "el nodo inicial no existe" in issue_messages(exc_info)


def test_unreachable_node_is_rejected():
    invalid = text_definition()
    invalid["nodes"].append(
        {"id": "orphan", "type": "text", "body": "Nunca", "terminal": True}
    )

    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition("movement.registered", invalid)

    assert "no es alcanzable" in issue_messages(exc_info)[0]


def test_cycle_without_exit_is_rejected():
    invalid = button_definition(action=None, next_node="question")

    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition("category.confirmation_required", invalid)

    assert "todas las ramas deben finalizar" in issue_messages(exc_info)[0]


def test_duplicate_option_id_is_rejected():
    invalid = button_definition()
    invalid["nodes"][0]["options"].append(
        {
            "id": "confirm",
            "title": "Cancelar",
            "action": "cancel_pending_operation",
        }
    )

    with pytest.raises(ConversationFlowDefinitionInvalid) as exc_info:
        validate_flow_definition("category.confirmation_required", invalid)

    assert "opcion esta repetido" in issue_messages(exc_info)[0]


def test_whatsapp_button_limit_is_enforced():
    invalid = button_definition()
    invalid["nodes"][0]["options"] = [
        {
            "id": f"option-{index}",
            "title": f"Opción {index}",
            "action": "cancel_pending_operation",
        }
        for index in range(4)
    ]

    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition("category.confirmation_required", invalid)


def test_contract_exposes_only_closed_registry():
    contract = available_contract()
    event_keys = {event["event_key"] for event in contract["events"]}

    assert "movement.registered" in event_keys
    assert contract["node_types"] == ["text", "reply_button", "list"]
