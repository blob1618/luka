from unittest.mock import AsyncMock, Mock

import pytest

from app.api.whatsapp import (
    InboundInteractiveReply,
    WhatsAppList,
    WhatsAppListRow,
    WhatsAppListSection,
    WhatsAppReplyButton,
    WhatsAppReplyButtons,
    build_whatsapp_payload,
    parse_interactive_reply,
    send_whatsapp_message,
    whatsapp_graph_api_version,
)


def test_text_payload_normalizes_argentine_mobile_number():
    payload = build_whatsapp_payload("5491123456789", "Hola")

    assert payload == {
        "messaging_product": "whatsapp",
        "to": "541123456789",
        "type": "text",
        "text": {"body": "Hola"},
    }


def test_template_payload_is_preserved():
    payload = build_whatsapp_payload(
        "541123456789",
        template_name="recordatorio",
        template_parameters=["Luz", "17/09"],
    )

    assert payload["type"] == "template"
    assert payload["template"]["name"] == "recordatorio"
    assert payload["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Luz"},
        {"type": "text", "text": "17/09"},
    ]


def test_reply_button_payload_uses_opaque_ids():
    message = WhatsAppReplyButtons(
        body="¿Confirmás?",
        header="Categoría",
        footer="Elegí una opción",
        buttons=(
            WhatsAppReplyButton("flow.v1.question.confirm", "Confirmar"),
            WhatsAppReplyButton("flow.v1.question.cancel", "Cancelar"),
        ),
    )

    payload = build_whatsapp_payload("541123456789", message)

    assert payload["type"] == "interactive"
    assert payload["interactive"] == {
        "type": "button",
        "header": {"type": "text", "text": "Categoría"},
        "body": {"text": "¿Confirmás?"},
        "footer": {"text": "Elegí una opción"},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {
                        "id": "flow.v1.question.confirm",
                        "title": "Confirmar",
                    },
                },
                {
                    "type": "reply",
                    "reply": {
                        "id": "flow.v1.question.cancel",
                        "title": "Cancelar",
                    },
                },
            ]
        },
    }


def test_list_payload_builds_sections_and_rows():
    message = WhatsAppList(
        body="Elegí un mes",
        button="Ver meses",
        sections=(
            WhatsAppListSection(
                title="2026",
                rows=(
                    WhatsAppListRow(
                        "flow.v2.month.september",
                        "Septiembre",
                        "Límite de septiembre",
                    ),
                    WhatsAppListRow("flow.v2.month.october", "Octubre"),
                ),
            ),
        ),
    )

    payload = build_whatsapp_payload("541123456789", message)

    assert payload["interactive"]["type"] == "list"
    assert payload["interactive"]["action"] == {
        "button": "Ver meses",
        "sections": [
            {
                "title": "2026",
                "rows": [
                    {
                        "id": "flow.v2.month.september",
                        "title": "Septiembre",
                        "description": "Límite de septiembre",
                    },
                    {"id": "flow.v2.month.october", "title": "Octubre"},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "message",
    [
        WhatsAppReplyButtons(
            body="Elegí",
            buttons=tuple(
                WhatsAppReplyButton(f"option.{index}", str(index))
                for index in range(4)
            ),
        ),
        WhatsAppReplyButtons(
            body="Elegí",
            buttons=(
                WhatsAppReplyButton("same", "Uno"),
                WhatsAppReplyButton("same", "Dos"),
            ),
        ),
        WhatsAppList(
            body="Elegí",
            button="Ver",
            sections=(
                WhatsAppListSection(
                    rows=tuple(
                        WhatsAppListRow(f"row.{index}", str(index))
                        for index in range(11)
                    )
                ),
            ),
        ),
    ],
)
def test_invalid_interactive_payload_is_rejected(message):
    with pytest.raises(ValueError):
        build_whatsapp_payload("541123456789", message)


@pytest.mark.parametrize(
    ("reply_type", "payload_key"),
    [("button_reply", "button_reply"), ("list_reply", "list_reply")],
)
def test_parse_interactive_reply(reply_type, payload_key):
    message = {
        "from": "541123456789",
        "id": "wamid.interactive",
        "type": "interactive",
        "interactive": {
            "type": reply_type,
            payload_key: {"id": "flow.v1.node.option", "title": "Visible"},
        },
    }

    parsed = parse_interactive_reply(message)

    assert parsed == InboundInteractiveReply(
        message_id="wamid.interactive",
        sender_phone="541123456789",
        reply_type=reply_type,
        option_id="flow.v1.node.option",
        title="Visible",
    )


@pytest.mark.parametrize(
    "message",
    [
        {"type": "text"},
        {"type": "interactive", "interactive": {"type": "unknown"}},
        {
            "from": "5411",
            "id": "wamid.1",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": ""}},
        },
    ],
)
def test_malformed_interactive_reply_is_ignored(message):
    assert parse_interactive_reply(message) is None


def test_graph_api_version_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    assert whatsapp_graph_api_version() == "v26.0"

    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "latest")
    assert whatsapp_graph_api_version() is None


@pytest.mark.asyncio
async def test_send_uses_configured_graph_version(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    response = Mock(status_code=200)
    response.text = "ok"
    post = AsyncMock(return_value=response)
    client = Mock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", Mock(return_value=client))

    sent = await send_whatsapp_message("541123456789", "Hola")

    assert sent is True
    post.assert_awaited_once()
    assert post.await_args.args[0] == (
        "https://graph.facebook.com/v26.0/phone-id/messages"
    )


@pytest.mark.asyncio
async def test_send_rejects_invalid_graph_version_before_network(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "26")
    client_factory = Mock()
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", client_factory)

    sent = await send_whatsapp_message("541123456789", "Hola")

    assert sent is False
    client_factory.assert_not_called()
