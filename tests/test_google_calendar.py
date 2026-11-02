from datetime import date
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from app.api.whatsapp import WhatsAppCTAURL
from app.models.database import (
    GoogleCalendarConnection,
    GoogleCalendarOAuthState,
    Recordatorio,
    Usuario,
)
from app.services import dispatcher, google_calendar as calendar_sync
from app.services.assistant_capabilities import reminder_management
from app.services.assistant_runtime import AssistantReply, run_for_phone
from app.services.assistant_transport import assemble_reply
from app.services.dispatch_contract import DispatchResult
from tests import test_assistant_pilot
from tests.test_assistant_pilot import NOW, PHONE, NativeScript

pytestmark = pytest.mark.unit

accounts = test_assistant_pilot.accounts


def _config():
    return calendar_sync.CalendarConfig(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="https://luka.example/integrations/google-calendar/callback",
        fernet_key=Fernet.generate_key().decode(),
    )


def _user_and_reminder(factory):
    session = factory()
    user = Usuario(
        id=uuid4(),
        nombre="Test",
        email=f"{uuid4()}@example.com",
        whatsapp_id="5491112345678",
    )
    reminder = Recordatorio(
        id=uuid4(),
        usuario_id=user.id,
        titulo="Luz",
        dia_del_mes=31,
        estado="activo",
        dias_anticipacion=3,
        moneda="ARS",
    )
    session.add_all([user, reminder])
    session.commit()
    user_id, reminder_id = user.id, reminder.id
    session.close()
    return user_id, reminder_id


def _mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        calendar_sync.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )


def test_event_matches_month_end_and_native_alert():
    reminder = {
        "id": uuid4(),
        "title": "Luz",
        "day": 31,
        "amount": "1000.00",
        "currency": "ARS",
        "advance_days": 3,
    }
    body = calendar_sync.event_body(reminder, date(2026, 2, 2))
    assert body["start"] == {
        "date": "2026-02-28",
        "timeZone": "America/Argentina/Buenos_Aires",
    }
    assert body["end"] == {
        "date": "2026-03-01",
        "timeZone": "America/Argentina/Buenos_Aires",
    }
    assert body["recurrence"] == ["RRULE:FREQ=MONTHLY;BYMONTHDAY=31,-1;BYSETPOS=1"]
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 4320}]
    reminder["advance_days"] = 30
    assert (
        calendar_sync.event_body(reminder, date(2026, 2, 2))["reminders"]["overrides"][
            0
        ]["minutes"]
        == 40320
    )


@pytest.mark.asyncio
async def test_oauth_is_single_use_and_refresh_token_is_encrypted(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(calendar_sync, "SessionLocal", isolated_db)
    user_id, reminder_id = _user_and_reminder(isolated_db)
    config = _config()
    url = calendar_sync.authorization_url_for_phone("5491112345678", config=config)
    params = parse_qs(urlsplit(url).query)
    assert params["scope"] == [calendar_sync.SCOPE]
    assert params["access_type"] == ["offline"]
    state = params["state"][0]
    session = isolated_db()
    assert session.query(GoogleCalendarOAuthState).one().state_hash != state
    session.close()

    def handler(request):
        assert request.url.path == "/token"
        return httpx.Response(
            200, json={"refresh_token": "refresh-secret", "scope": calendar_sync.SCOPE}
        )

    _mock_http(monkeypatch, handler)
    assert (
        await calendar_sync.complete_authorization("code", state, config=config)
        == user_id
    )
    session = isolated_db()
    connection = session.get(GoogleCalendarConnection, user_id)
    assert connection.refresh_token_ciphertext != "refresh-secret"
    assert (
        config.cipher().decrypt(connection.refresh_token_ciphertext.encode())
        == b"refresh-secret"
    )
    assert session.get(Recordatorio, reminder_id).google_calendar_event_id is None
    session.close()
    with pytest.raises(calendar_sync.CalendarAuthorizationError):
        await calendar_sync.complete_authorization("code", state, config=config)


@pytest.mark.asyncio
async def test_oauth_rejects_missing_scope(isolated_db, monkeypatch):
    monkeypatch.setattr(calendar_sync, "SessionLocal", isolated_db)
    user_id, _ = _user_and_reminder(isolated_db)
    config = _config()
    url = calendar_sync.authorization_url_for_phone("5491112345678", config=config)
    state = parse_qs(urlsplit(url).query)["state"][0]
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "refresh_token": "refresh-secret",
                "scope": "https://www.googleapis.com/auth/calendar.readonly",
            },
        ),
    )
    with pytest.raises(calendar_sync.CalendarAuthorizationError):
        await calendar_sync.complete_authorization("code", state, config=config)
    session = isolated_db()
    assert session.get(GoogleCalendarConnection, user_id) is None
    session.close()


@pytest.mark.asyncio
async def test_reconciles_create_edit_pause_activate_and_delete(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(calendar_sync, "SessionLocal", isolated_db)
    user_id, reminder_id = _user_and_reminder(isolated_db)
    config = _config()
    session = isolated_db()
    session.add(
        GoogleCalendarConnection(
            usuario_id=user_id,
            refresh_token_ciphertext=config.cipher()
            .encrypt(b"refresh-secret")
            .decode(),
        )
    )
    session.commit()
    session.close()

    events = {}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access-secret"})
        assert request.headers["Authorization"] == "Bearer access-secret"
        event_id = calendar_sync._event_id(reminder_id)
        if request.method == "POST":
            events[event_id] = request.content
            return httpx.Response(200, json={"id": event_id})
        if request.method == "PATCH":
            if event_id not in events:
                return httpx.Response(404)
            events[event_id] = request.content
            return httpx.Response(200, json={"id": event_id})
        if request.method == "DELETE":
            events.pop(event_id, None)
            return httpx.Response(204)
        raise AssertionError(request.method)

    _mock_http(monkeypatch, handler)
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    session = isolated_db()
    row = session.get(Recordatorio, reminder_id)
    assert row.google_calendar_event_id == calendar_sync._event_id(reminder_id)
    assert row.google_calendar_synced_fingerprint
    assert len(events) == 1
    session.close()

    before = len(calls)
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    assert len(calls) == before  # no token refresh or event write for unchanged data

    session = isolated_db()
    row = session.get(Recordatorio, reminder_id)
    row.titulo = "Nueva luz"
    session.commit()
    session.close()
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    assert b"Nueva luz" in events[calendar_sync._event_id(reminder_id)]

    for state, expected_count in [("pausado", 0), ("activo", 1), ("eliminado", 0)]:
        session = isolated_db()
        session.get(Recordatorio, reminder_id).estado = state
        session.commit()
        session.close()
        await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
        assert len(events) == expected_count
    session = isolated_db()
    assert session.get(Recordatorio, reminder_id).estado == "eliminado"
    assert session.get(Recordatorio, reminder_id).google_calendar_event_id is None
    session.close()


@pytest.mark.asyncio
async def test_google_failure_keeps_reminder_and_retries(isolated_db, monkeypatch):
    monkeypatch.setattr(calendar_sync, "SessionLocal", isolated_db)
    user_id, reminder_id = _user_and_reminder(isolated_db)
    config = _config()
    session = isolated_db()
    session.add(
        GoogleCalendarConnection(
            usuario_id=user_id,
            refresh_token_ciphertext=config.cipher()
            .encrypt(b"refresh-secret")
            .decode(),
        )
    )
    session.commit()
    session.close()
    failing = True

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(
            503 if failing else 200, json={"id": calendar_sync._event_id(reminder_id)}
        )

    _mock_http(monkeypatch, handler)
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    session = isolated_db()
    row = session.get(Recordatorio, reminder_id)
    assert row.estado == "activo"
    assert row.google_calendar_event_id is None
    session.close()
    failing = False
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    session = isolated_db()
    assert session.get(
        Recordatorio, reminder_id
    ).google_calendar_event_id == calendar_sync._event_id(reminder_id)
    session.close()


@pytest.mark.asyncio
async def test_existing_event_id_is_recovered_after_interrupted_insert(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(calendar_sync, "SessionLocal", isolated_db)
    user_id, reminder_id = _user_and_reminder(isolated_db)
    config = _config()
    session = isolated_db()
    session.add(
        GoogleCalendarConnection(
            usuario_id=user_id,
            refresh_token_ciphertext=config.cipher()
            .encrypt(b"refresh-secret")
            .decode(),
        )
    )
    session.commit()
    session.close()
    methods = []

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "token"})
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(200, json={"id": calendar_sync._event_id(reminder_id)})

    _mock_http(monkeypatch, handler)
    await calendar_sync.sync_user(user_id, config=config, today=date(2026, 2, 2))
    assert methods == ["POST", "PATCH"]
    session = isolated_db()
    assert session.get(
        Recordatorio, reminder_id
    ).google_calendar_event_id == calendar_sync._event_id(reminder_id)
    session.close()


@pytest.mark.asyncio
async def test_calendar_callback_starts_sync(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    user_id = uuid4()
    authorize = AsyncMock(return_value=user_id)
    sync = AsyncMock()
    monkeypatch.setattr("app.main.complete_authorization", authorize)
    monkeypatch.setattr("app.main.sync_user", sync)
    with TestClient(app) as client:
        denied = client.get(
            "/integrations/google-calendar/callback?error=access_denied"
        )
        assert denied.status_code == 400
        assert authorize.await_count == 0
        accepted = client.get(
            "/integrations/google-calendar/callback?code=code&state=state"
        )
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
    authorize.assert_awaited_once_with("code", "state")
    sync.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_calendar_connect_tool_sends_single_cta_through_agent(accounts, monkeypatch):
    _, *_ = accounts
    url = "https://accounts.google.com/example"
    monkeypatch.setattr(
        reminder_management,
        "authorization_url_for_phone",
        lambda phone: url,
    )
    script = NativeScript([[('connect_google_calendar', {})]])
    reply = await run_for_phone(
        PHONE,
        "calendar-connect",
        "Conectá mi calendario de Google",
        provider=script,
        now=NOW,
    )
    assert script.calls == 1
    assert reply.event_key == "calendar.link.sent"
    assert reply.event_variables == {"auth_url": url}
    body = (
        "📅 Tocá el botón para autorizar a Luka a gestionar eventos de pagos en "
        "tu calendario de Google. El enlace vence en 10 minutos y se usa una sola vez."
    )
    assert reply.text == body
    assert len(reply.outbound) == 1
    message = reply.outbound[0]
    assert isinstance(message, WhatsAppCTAURL)
    assert message.url == url
    assert message.display_text == "Conectar calendario"


@pytest.mark.asyncio
async def test_calendar_connect_tool_reports_configuration_error(accounts, monkeypatch):
    _, *_ = accounts

    def unavailable(phone):
        raise calendar_sync.CalendarConfigurationError("missing")

    monkeypatch.setattr(reminder_management, "authorization_url_for_phone", unavailable)
    script = NativeScript([[('connect_google_calendar', {})]])
    reply = await run_for_phone(
        PHONE, "calendar-config-error", "Conectá mi calendario", provider=script, now=NOW
    )
    assert reply.text == reminder_management.CALENDAR_CONNECT_ERROR
    assert reply.event_key is None
    assert reply.outbound == []


@pytest.mark.asyncio
async def test_calendar_link_is_the_primary_transport_message(monkeypatch):
    from app.services import assistant_transport

    monkeypatch.setattr(
        assistant_transport.ConversationFlowRuntime,
        "render_event",
        AsyncMock(return_value=None),
    )
    link = WhatsAppCTAURL(
        body="Tocá el botón para conectar tu calendario.",
        display_text="Conectar calendario",
        url="https://accounts.google.com/example",
    )
    answer = AssistantReply(
        link.body,
        outbound=[link],
        event_key="calendar.link.sent",
        event_variables={"auth_url": link.url},
    )
    result = await assemble_reply(PHONE, answer)
    assert result.reply_message is link
    assert result.reply_text == link.body
    assert result.followup_messages == []


@pytest.mark.asyncio
async def test_calendar_phrase_delegates_to_assistant_without_regex(monkeypatch):
    monkeypatch.setattr(dispatcher, "_enter", AsyncMock(return_value=None))
    assistant = AsyncMock(return_value=AssistantReply("respuesta del agente"))
    monkeypatch.setattr(dispatcher, "run_for_phone", assistant)
    assembled = DispatchResult("respuesta del agente")
    monkeypatch.setattr(dispatcher, "assemble_reply", AsyncMock(return_value=assembled))

    result = await dispatcher.process_incoming_message(
        PHONE, "Conectá mi calendario de Google"
    )

    assert result is assembled
    assistant.assert_awaited_once()
    assert not hasattr(dispatcher, "_is_calendar_connect")
    assert not hasattr(dispatcher, "_handle_calendar_connect")
