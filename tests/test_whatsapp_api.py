from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from app.api.whatsapp import (
    InboundInteractiveReply,
    WhatsAppList,
    WhatsAppListRow,
    WhatsAppListSection,
    WhatsAppReplyButton,
    WhatsAppReplyButtons,
    build_whatsapp_payload,
    close_whatsapp_client,
    get_whatsapp_client,
    parse_interactive_reply,
    send_whatsapp_message,
    send_whatsapp_reaction,
    whatsapp_graph_api_version,
)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_whatsapp_client():
    await close_whatsapp_client()
    yield
    await close_whatsapp_client()


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
    client.is_closed = False
    client.post = post
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


@pytest.mark.asyncio
async def test_whatsapp_client_pool_configuration():
    client = get_whatsapp_client()
    try:
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 5.0
        assert client.timeout.write == 5.0
        assert client.timeout.pool == 5.0
        assert client._transport._pool._max_keepalive_connections == 10
        assert client._transport._pool._max_connections == 20
        assert client._transport._pool._keepalive_expiry == 30.0
    finally:
        await client.aclose()


def test_whatsapp_client_pool_configuration_args(monkeypatch):
    mock_client = Mock()
    mock_client.is_closed = False
    mock_factory = Mock(return_value=mock_client)
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", mock_factory)

    client = get_whatsapp_client()
    assert client is mock_client
    mock_factory.assert_called_once()
    _, kwargs = mock_factory.call_args
    assert kwargs["timeout"] == 5.0
    limits = kwargs["limits"]
    assert limits.max_keepalive_connections == 10
    assert limits.max_connections == 20
    assert limits.keepalive_expiry == 30.0


@pytest.mark.asyncio
async def test_whatsapp_client_reuse():
    client1 = get_whatsapp_client()
    client2 = get_whatsapp_client()
    try:
        assert client1 is client2
    finally:
        await client1.aclose()


@pytest.mark.asyncio
async def test_whatsapp_client_recreation_if_closed():
    client1 = get_whatsapp_client()
    assert not client1.is_closed
    await client1.aclose()
    assert client1.is_closed

    client2 = get_whatsapp_client()
    try:
        assert client2 is not client1
        assert not client2.is_closed
    finally:
        await client2.aclose()


@pytest.mark.asyncio
async def test_whatsapp_client_close_idempotent():
    client = get_whatsapp_client()
    assert not client.is_closed

    await close_whatsapp_client()
    assert client.is_closed

    # Second call must be safe and idempotent
    await close_whatsapp_client()


@pytest.mark.asyncio
async def test_shared_client_used_by_message_and_reaction(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")

    mock_client = Mock()
    mock_client.is_closed = False
    mock_client.post = AsyncMock(return_value=Mock(status_code=200, text="ok"))
    client_factory = Mock(return_value=mock_client)
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", client_factory)

    sent_msg = await send_whatsapp_message("541123456789", "Hola")
    assert sent_msg is True

    sent_react = await send_whatsapp_reaction("541123456789", "wamid.123")
    assert sent_react is True

    # Both reuse the exact same client instance
    client_factory.assert_called_once()
    assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_reaction_contract_and_payload(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id-123")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")

    response = Mock(status_code=200)
    response.text = '{"success": true}'
    post = AsyncMock(return_value=response)
    client = Mock()
    client.is_closed = False
    client.post = post

    client_factory = Mock(return_value=client)
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", client_factory)

    sent = await send_whatsapp_reaction("5491123456789", "wamid.12345")

    assert sent is True
    post.assert_awaited_once()
    assert post.await_args.args[0] == "https://graph.facebook.com/v26.0/phone-id-123/messages"
    assert post.await_args.kwargs["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "541123456789",
        "type": "reaction",
        "reaction": {
            "message_id": "wamid.12345",
            "emoji": "⏳",
        },
    }
    assert post.await_args.kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_send_reaction_custom_emoji(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id-123")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")

    response = Mock(status_code=200)
    response.text = '{"success": true}'
    post = AsyncMock(return_value=response)
    client = Mock()
    client.is_closed = False
    client.post = post
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", Mock(return_value=client))

    sent = await send_whatsapp_reaction("541123456789", "wamid.12345", emoji="✅")

    assert sent is True
    post.assert_awaited_once()
    assert post.await_args.kwargs["json"]["reaction"]["emoji"] == "✅"


@pytest.mark.asyncio
async def test_send_reaction_missing_credentials_or_invalid_version(monkeypatch):
    client_factory = Mock()
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", client_factory)

    # Missing credentials
    monkeypatch.delenv("WHATSAPP_API_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_ID", raising=False)
    assert await send_whatsapp_reaction("541123456789", "wamid.1") is False

    # Invalid version
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "invalid_ver")
    assert await send_whatsapp_reaction("541123456789", "wamid.1") is False

    # Empty inputs
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    assert await send_whatsapp_reaction("", "wamid.1") is False
    assert await send_whatsapp_reaction("541123456789", "") is False

    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_send_reaction_error_timeout_and_network_tolerance(monkeypatch):
    import httpx

    monkeypatch.setenv("WHATSAPP_API_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")

    # Non-200 HTTP response
    response_400 = Mock(status_code=400, text="Bad Request")
    client_400 = Mock()
    client_400.is_closed = False
    client_400.post = AsyncMock(return_value=response_400)
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", Mock(return_value=client_400))

    assert await send_whatsapp_reaction("541123456789", "wamid.1") is False

    # HTTP Timeout
    await close_whatsapp_client()
    client_timeout = Mock()
    client_timeout.is_closed = False
    client_timeout.post = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", Mock(return_value=client_timeout))

    assert await send_whatsapp_reaction("541123456789", "wamid.1") is False

    # Network error
    await close_whatsapp_client()
    client_network = Mock()
    client_network.is_closed = False
    client_network.post = AsyncMock(side_effect=httpx.NetworkError("Connection refused"))
    monkeypatch.setattr("app.api.whatsapp.httpx.AsyncClient", Mock(return_value=client_network))

    assert await send_whatsapp_reaction("541123456789", "wamid.1") is False
