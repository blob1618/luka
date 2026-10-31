"""Opt-in Google Calendar sync for payment reminders.

The internal reminder is authoritative. Google writes are reconciled after DB
commits, so an external outage never rolls back a user's reminder.
"""

import calendar
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.models.database import (
    GoogleCalendarConnection,
    GoogleCalendarOAuthState,
    Recordatorio,
    SessionLocal,
    Usuario,
)

logger = logging.getLogger(__name__)
SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


class CalendarConfigurationError(Exception):
    pass


class CalendarAuthorizationError(Exception):
    pass


@dataclass(frozen=True)
class CalendarConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    fernet_key: str

    @classmethod
    def from_env(cls) -> "CalendarConfig":
        values = [
            os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "").strip(),
            os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "").strip(),
            os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "").strip(),
            os.getenv("GOOGLE_CALENDAR_FERNET_KEY", "").strip(),
        ]
        if not all(values):
            raise CalendarConfigurationError("Google Calendar is not configured")
        if not (
            values[2].startswith("https://")
            or values[2].startswith("http://localhost:")
        ):
            raise CalendarConfigurationError("Google Calendar redirect must use HTTPS")
        try:
            Fernet(values[3].encode())
        except (ValueError, TypeError) as exc:
            raise CalendarConfigurationError(
                "Invalid Google Calendar encryption key"
            ) from exc
        return cls(*values)

    def cipher(self) -> Fernet:
        return Fernet(self.fernet_key.encode())


def _aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _event_id(reminder_id: UUID) -> str:
    # Google event IDs permit base32hex characters; UUID hex is a subset.
    return "luka" + reminder_id.hex


def _first_due(day: int, today: date) -> date:
    due = date(
        today.year,
        today.month,
        min(day, calendar.monthrange(today.year, today.month)[1]),
    )
    if due < today:
        year = today.year + (today.month == 12)
        month = 1 if today.month == 12 else today.month + 1
        due = date(year, month, min(day, calendar.monthrange(year, month)[1]))
    return due


def event_body(reminder: dict, today: date) -> dict:
    due = _first_due(reminder["day"], today)
    next_day = due + timedelta(days=1)
    amount = reminder["amount"]
    description = "Recordatorio de pago de Luka"
    if amount is not None:
        description += f" · {amount} {reminder['currency'] or 'ARS'}"
    return {
        "id": _event_id(reminder["id"]),
        "summary": f"Pago: {reminder['title']}",
        "description": description,
        "start": {
            "date": due.isoformat(),
            "timeZone": "America/Argentina/Buenos_Aires",
        },
        "end": {
            "date": next_day.isoformat(),
            "timeZone": "America/Argentina/Buenos_Aires",
        },
        # For days 29-31, use the last day of a shorter month, as Luka does.
        "recurrence": [
            f"RRULE:FREQ=MONTHLY;BYMONTHDAY={reminder['day']},-1;BYSETPOS=1"
        ],
        "reminders": {
            "useDefault": False,
            # Google caps native reminders at 28 days; Luka permits up to 30.
            "overrides": [
                {
                    "method": "popup",
                    "minutes": min(reminder["advance_days"] * 1440, 40320),
                }
            ],
        },
        "visibility": "private",
        "transparency": "transparent",
        "extendedProperties": {"private": {"lukaReminderId": str(reminder["id"])}},
    }


def _fingerprint(reminder: dict) -> str:
    values = [
        reminder["title"],
        reminder["day"],
        reminder["amount"],
        reminder["currency"],
        reminder["advance_days"],
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _needs_sync(reminder: dict) -> bool:
    desired = reminder["state"] == "activo" and reminder["day"] is not None
    if not desired:
        return bool(reminder["event_id"])
    return reminder["event_id"] != _event_id(reminder["id"]) or reminder[
        "fingerprint"
    ] != _fingerprint(reminder)


def authorization_url_for_phone(
    whatsapp_id: str, *, config: CalendarConfig | None = None
) -> str | None:
    config = config or CalendarConfig.from_env()
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == whatsapp_id).first()
        if user is None:
            return None
        session.add(
            GoogleCalendarOAuthState(
                state_hash=state_hash,
                usuario_id=user.id,
                expira_en=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        session.commit()
    finally:
        session.close()
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def complete_authorization(
    code: str, state: str, *, config: CalendarConfig | None = None
) -> UUID:
    config = config or CalendarConfig.from_env()
    if not code or len(code) > 4096 or not state or len(state) > 256:
        raise CalendarAuthorizationError("Invalid authorization response")
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    session = SessionLocal()
    try:
        pending = (
            session.query(GoogleCalendarOAuthState)
            .filter(
                GoogleCalendarOAuthState.state_hash == state_hash,
            )
            .with_for_update()
            .first()
        )
        if (
            pending is None
            or pending.consumido_en is not None
            or _aware_utc(pending.expira_en) <= datetime.now(timezone.utc)
        ):
            raise CalendarAuthorizationError(
                "Authorization link expired or already used"
            )
        user_id = pending.usuario_id
        pending.consumido_en = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "redirect_uri": config.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CalendarAuthorizationError("Google authorization failed") from exc
    if not isinstance(payload, dict):
        raise CalendarAuthorizationError("Invalid Google token response")
    refresh_token = payload.get("refresh_token")
    granted_scopes = payload.get("scope")
    granted = granted_scopes.split() if isinstance(granted_scopes, str) else []
    if not isinstance(refresh_token, str) or not refresh_token or SCOPE not in granted:
        raise CalendarAuthorizationError("Google did not grant offline Calendar access")

    ciphertext = config.cipher().encrypt(refresh_token.encode()).decode()
    session = SessionLocal()
    try:
        connection = session.get(GoogleCalendarConnection, user_id)
        if connection is None:
            session.add(
                GoogleCalendarConnection(
                    usuario_id=user_id, refresh_token_ciphertext=ciphertext
                )
            )
        else:
            connection.refresh_token_ciphertext = ciphertext
            connection.conectado_en = datetime.now(timezone.utc)
        # Reconcile even if the user authorized a different Google account.
        session.query(Recordatorio).filter(Recordatorio.usuario_id == user_id).update(
            {
                Recordatorio.google_calendar_event_id: None,
                Recordatorio.google_calendar_synced_fingerprint: None,
            }
        )
        session.commit()
    finally:
        session.close()
    return user_id


async def _access_token(
    client: httpx.AsyncClient, config: CalendarConfig, ciphertext: str
) -> str:
    try:
        refresh_token = config.cipher().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise CalendarConfigurationError(
            "Stored Google Calendar credential cannot be decrypted"
        ) from exc
    response = await client.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise CalendarAuthorizationError("Invalid Google token response")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise CalendarAuthorizationError("Google did not return an access token")
    return token


def _snapshot_reminders(session, user_id: UUID) -> list[dict]:
    rows = session.query(Recordatorio).filter(Recordatorio.usuario_id == user_id).all()
    return [
        {
            "id": row.id,
            "title": row.titulo,
            "day": row.dia_del_mes,
            "amount": str(row.monto) if row.monto is not None else None,
            "currency": row.moneda,
            "advance_days": row.dias_anticipacion,
            "state": row.estado,
            "event_id": row.google_calendar_event_id,
            "fingerprint": row.google_calendar_synced_fingerprint,
        }
        for row in rows
    ]


async def _write_event(
    client: httpx.AsyncClient, headers: dict, reminder: dict, today: date
) -> None:
    event_id = _event_id(reminder["id"])
    url = f"{EVENTS_URL}/{quote(event_id)}"
    body = event_body(reminder, today)
    if reminder["event_id"]:
        response = await client.patch(url, headers=headers, json=body)
        if response.status_code == 404:
            response = await client.post(EVENTS_URL, headers=headers, json=body)
    else:
        response = await client.post(EVENTS_URL, headers=headers, json=body)
    if response.status_code == 409:
        response = await client.patch(url, headers=headers, json=body)
    response.raise_for_status()


async def sync_user(
    user_id: UUID, *, config: CalendarConfig | None = None, today: date | None = None
) -> None:
    config = config or CalendarConfig.from_env()
    session = SessionLocal()
    try:
        connection = session.get(GoogleCalendarConnection, user_id)
        if connection is None:
            return
        ciphertext = connection.refresh_token_ciphertext
        reminders = [
            row for row in _snapshot_reminders(session, user_id) if _needs_sync(row)
        ]
    finally:
        session.close()

    if not reminders:
        return

    today = today or datetime.now(LOCAL_TZ).date()
    async with httpx.AsyncClient(timeout=8.0) as client:
        token_started = time.perf_counter()
        try:
            token = await _access_token(client, config, ciphertext)
        except (
            httpx.HTTPError,
            ValueError,
            CalendarAuthorizationError,
            CalendarConfigurationError,
        ) as exc:
            logger.warning(
                "[CALENDAR_SYNC] user=%s token_error=%s duration_ms=%.1f",
                user_id,
                type(exc).__name__,
                (time.perf_counter() - token_started) * 1000,
            )
            return
        logger.info(
            "[CALENDAR_SYNC] user=%s token_refresh_ms=%.1f",
            user_id,
            (time.perf_counter() - token_started) * 1000,
        )
        headers = {"Authorization": f"Bearer {token}"}
        for reminder in reminders:
            desired = reminder["state"] == "activo" and reminder["day"] is not None
            fingerprint = _fingerprint(reminder) if desired else None
            event_started = time.perf_counter()
            try:
                if desired:
                    await _write_event(client, headers, reminder, today)
                    new_event_id = _event_id(reminder["id"])
                else:
                    response = await client.delete(
                        f"{EVENTS_URL}/{quote(reminder['event_id'])}", headers=headers
                    )
                    if response.status_code != 404:
                        response.raise_for_status()
                    new_event_id = None
                session = SessionLocal()
                try:
                    row = session.get(Recordatorio, reminder["id"])
                    if row is not None:
                        # A concurrent edit is picked up by the next reconciliation.
                        row.google_calendar_event_id = new_event_id
                        row.google_calendar_synced_fingerprint = (
                            fingerprint if new_event_id else None
                        )
                        session.commit()
                finally:
                    session.close()
                logger.info(
                    "[CALENDAR_SYNC] reminder=%s action=%s duration_ms=%.1f",
                    reminder["id"],
                    "upsert" if desired else "delete",
                    (time.perf_counter() - event_started) * 1000,
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "[CALENDAR_SYNC] reminder=%s error=%s duration_ms=%.1f",
                    reminder["id"],
                    type(exc).__name__,
                    (time.perf_counter() - event_started) * 1000,
                )


async def reconcile_google_calendar() -> None:
    try:
        config = CalendarConfig.from_env()
    except CalendarConfigurationError:
        return
    session = SessionLocal()
    try:
        user_ids = [
            row[0] for row in session.query(GoogleCalendarConnection.usuario_id).all()
        ]
    finally:
        session.close()
    for user_id in user_ids:
        try:
            await sync_user(user_id, config=config)
        except Exception as exc:
            logger.warning(
                "[CALENDAR_SYNC] user=%s error=%s", user_id, type(exc).__name__
            )
