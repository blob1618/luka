import inspect
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias
from urllib.parse import urlsplit

import httpx


DEFAULT_WHATSAPP_GRAPH_API_VERSION = "v26.0"


class WhatsAppDeliveryStatus(str, Enum):
    SUCCESS = "success"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WhatsAppSendResult:
    status: WhatsAppDeliveryStatus
    message_id: str | None = None
    error_code: str | None = None
    status_code: int | None = None
    error_message: str | None = None
    suppress_retry: bool = False

    @property
    def is_success(self) -> bool:
        return self.status == WhatsAppDeliveryStatus.SUCCESS

    @property
    def is_retryable(self) -> bool:
        return self.status == WhatsAppDeliveryStatus.RETRYABLE

    @property
    def is_permanent(self) -> bool:
        return self.status == WhatsAppDeliveryStatus.PERMANENT

    @property
    def is_unknown(self) -> bool:
        return self.status == WhatsAppDeliveryStatus.UNKNOWN


@dataclass(frozen=True)
class WhatsAppText:
    body: str


@dataclass(frozen=True)
class WhatsAppCTAURL:
    body: str
    display_text: str
    url: str


@dataclass(frozen=True)
class WhatsAppImage:
    content: bytes
    filename: str = "luka-grafico.png"
    caption: str | None = None
    mime_type: str = "image/png"


@dataclass(frozen=True)
class WhatsAppReplyButton:
    id: str
    title: str


@dataclass(frozen=True)
class WhatsAppReplyButtons:
    body: str
    buttons: tuple[WhatsAppReplyButton, ...]
    header: str | None = None
    footer: str | None = None


@dataclass(frozen=True)
class WhatsAppListRow:
    id: str
    title: str
    description: str | None = None


@dataclass(frozen=True)
class WhatsAppListSection:
    rows: tuple[WhatsAppListRow, ...]
    title: str | None = None


@dataclass(frozen=True)
class WhatsAppList:
    body: str
    button: str
    sections: tuple[WhatsAppListSection, ...]
    header: str | None = None
    footer: str | None = None


OutboundWhatsAppMessage: TypeAlias = (
    WhatsAppText | WhatsAppImage | WhatsAppReplyButtons | WhatsAppList | WhatsAppCTAURL
)


@dataclass(frozen=True)
class InboundInteractiveReply:
    message_id: str
    sender_phone: str
    reply_type: str
    option_id: str
    title: str | None = None


def normalize_whatsapp_number(to_number: str) -> str:
    if to_number.startswith("549") and len(to_number) == 13:
        return "54" + to_number[3:]
    return to_number


def whatsapp_graph_api_version() -> str | None:
    version = os.getenv(
        "WHATSAPP_GRAPH_API_VERSION",
        DEFAULT_WHATSAPP_GRAPH_API_VERSION,
    ).strip()
    if not re.fullmatch(r"v[1-9][0-9]*\.0", version):
        return None
    return version


def parse_interactive_reply(message: dict) -> InboundInteractiveReply | None:
    if message.get("type") != "interactive":
        return None
    interactive = message.get("interactive")
    if not isinstance(interactive, dict):
        return None
    reply_type = interactive.get("type")
    if reply_type not in {"button_reply", "list_reply"}:
        return None
    reply = interactive.get(reply_type)
    if not isinstance(reply, dict):
        return None

    message_id = str(message.get("id") or "").strip()
    sender_phone = str(message.get("from") or "").strip()
    option_id = str(reply.get("id") or "").strip()
    if not message_id or not sender_phone or not option_id:
        return None

    title = reply.get("title")
    return InboundInteractiveReply(
        message_id=message_id,
        sender_phone=sender_phone,
        reply_type=reply_type,
        option_id=option_id,
        title=str(title) if title is not None else None,
    )


def build_whatsapp_payload(
    to_number: str,
    message: str | OutboundWhatsAppMessage | None = None,
    *,
    template_name: str | None = None,
    template_parameters: list[str] | None = None,
) -> dict:
    recipient = normalize_whatsapp_number(to_number)
    base = {
        "messaging_product": "whatsapp",
        "to": recipient,
    }
    if template_name:
        parameters = template_parameters or []
        return {
            **base,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "es_AR"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(parameter)}
                            for parameter in parameters
                        ],
                    }
                ],
            },
        }

    if isinstance(message, str) or message is None:
        message = WhatsAppText(message or "")
    if isinstance(message, WhatsAppText):
        _bounded_text(message.body, field="body", minimum=1, maximum=4096)
        return {**base, "type": "text", "text": {"body": message.body}}
    if isinstance(message, WhatsAppCTAURL):
        _bounded_text(message.body, field="body", minimum=1, maximum=1024)
        _bounded_text(message.display_text, field="display_text", minimum=1, maximum=20)
        parsed = urlsplit(message.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(char.isspace() for char in message.url)
        ):
            raise ValueError("CTA URL must be an absolute HTTP or HTTPS URL")
        return {
            **base,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": message.body},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": message.display_text,
                        "url": message.url,
                    },
                },
            },
        }
    if isinstance(message, WhatsAppReplyButtons):
        return {**base, "type": "interactive", "interactive": _buttons_payload(message)}
    if isinstance(message, WhatsAppList):
        return {**base, "type": "interactive", "interactive": _list_payload(message)}
    raise TypeError("Unsupported WhatsApp message type")


def _buttons_payload(message: WhatsAppReplyButtons) -> dict:
    _bounded_text(message.body, field="body", minimum=1, maximum=1024)
    _optional_bounded_text(message.header, field="header", maximum=60)
    _optional_bounded_text(message.footer, field="footer", maximum=60)
    if not 1 <= len(message.buttons) <= 3:
        raise ValueError("reply buttons must contain between 1 and 3 options")

    interactive = _interactive_shell(
        interactive_type="button",
        body=message.body,
        header=message.header,
        footer=message.footer,
    )
    seen_ids: set[str] = set()
    buttons = []
    for button in message.buttons:
        _stable_id(button.id, seen_ids)
        _bounded_text(button.title, field="button title", minimum=1, maximum=20)
        buttons.append(
            {
                "type": "reply",
                "reply": {"id": button.id, "title": button.title},
            }
        )
    interactive["action"] = {"buttons": buttons}
    return interactive


def _list_payload(message: WhatsAppList) -> dict:
    _bounded_text(message.body, field="body", minimum=1, maximum=1024)
    _bounded_text(message.button, field="list button", minimum=1, maximum=20)
    _optional_bounded_text(message.header, field="header", maximum=60)
    _optional_bounded_text(message.footer, field="footer", maximum=60)
    if not 1 <= len(message.sections) <= 10:
        raise ValueError("lists must contain between 1 and 10 sections")
    if len(message.sections) > 1 and any(not section.title for section in message.sections):
        raise ValueError("every section needs a title when a list has multiple sections")

    interactive = _interactive_shell(
        interactive_type="list",
        body=message.body,
        header=message.header,
        footer=message.footer,
    )
    seen_ids: set[str] = set()
    row_count = 0
    sections = []
    for section in message.sections:
        if not section.rows:
            raise ValueError("list sections cannot be empty")
        _optional_bounded_text(section.title, field="section title", maximum=24)
        rows = []
        for row in section.rows:
            row_count += 1
            _stable_id(row.id, seen_ids)
            _bounded_text(row.title, field="row title", minimum=1, maximum=24)
            _optional_bounded_text(
                row.description,
                field="row description",
                maximum=72,
            )
            row_payload = {"id": row.id, "title": row.title}
            if row.description:
                row_payload["description"] = row.description
            rows.append(row_payload)
        section_payload = {"rows": rows}
        if section.title:
            section_payload["title"] = section.title
        sections.append(section_payload)
    if row_count > 10:
        raise ValueError("lists can contain at most 10 rows")
    interactive["action"] = {"button": message.button, "sections": sections}
    return interactive


def _interactive_shell(
    *,
    interactive_type: str,
    body: str,
    header: str | None,
    footer: str | None,
) -> dict:
    payload = {"type": interactive_type, "body": {"text": body}}
    if header:
        payload["header"] = {"type": "text", "text": header}
    if footer:
        payload["footer"] = {"text": footer}
    return payload


def _stable_id(value: str, seen_ids: set[str]) -> None:
    if not value or len(value) > 200 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]*", value
    ):
        raise ValueError("interactive option IDs must be stable opaque identifiers")
    if value in seen_ids:
        raise ValueError("interactive option IDs must be unique")
    seen_ids.add(value)


def _bounded_text(value: str, *, field: str, minimum: int, maximum: int) -> None:
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must contain between {minimum} and {maximum} characters")


def _optional_bounded_text(
    value: str | None,
    *,
    field: str,
    maximum: int,
) -> None:
    if value is not None:
        _bounded_text(value, field=field, minimum=1, maximum=maximum)


_whatsapp_client: httpx.AsyncClient | None = None


def _is_client_closed(client: httpx.AsyncClient) -> bool:
    is_closed = getattr(client, "is_closed", False)
    if isinstance(is_closed, bool):
        return is_closed
    return False


def get_whatsapp_client() -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient configured for WhatsApp API calls.

    Reuses the existing client if open; recreates it if None or closed.
    """
    global _whatsapp_client
    if _whatsapp_client is None or _is_client_closed(_whatsapp_client):
        _whatsapp_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            ),
            timeout=5.0,
        )
    return _whatsapp_client


async def close_whatsapp_client() -> None:
    """Safely and idempotently close the shared WhatsApp httpx.AsyncClient."""
    global _whatsapp_client
    if _whatsapp_client is not None:
        client = _whatsapp_client
        _whatsapp_client = None
        if not _is_client_closed(client):
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                res = aclose()
                if inspect.isawaitable(res):
                    await res


async def send_whatsapp_message_detailed(
    to_number: str,
    message_text: str | OutboundWhatsAppMessage | None = None,
    *,
    template_name: str | None = None,
    template_parameters: list[str] | None = None,
) -> WhatsAppSendResult:
    """Send a supported message through Meta returning a detailed result."""
    api_token = os.getenv("WHATSAPP_API_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    api_version = whatsapp_graph_api_version()
    if not api_token or not phone_id:
        print("Falta WHATSAPP_API_TOKEN o WHATSAPP_PHONE_ID. No se puede enviar el mensaje.")
        return WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.PERMANENT,
            error_code="missing_credentials",
            error_message="Falta WHATSAPP_API_TOKEN o WHATSAPP_PHONE_ID.",
        )
    if api_version is None:
        print("WHATSAPP_GRAPH_API_VERSION tiene un formato invalido.")
        return WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.PERMANENT,
            error_code="invalid_graph_version",
            error_message="WHATSAPP_GRAPH_API_VERSION tiene un formato invalido.",
        )

    try:
        if isinstance(message_text, WhatsAppImage):
            _validate_image(message_text)
            upload_result, media_id = await _upload_whatsapp_image(
                message_text,
                api_token=api_token,
                phone_id=phone_id,
                api_version=api_version,
            )
            if media_id is None:
                return await _image_failure_with_fallback(
                    to_number,
                    upload_result,
                    ambiguous=upload_result.is_unknown,
                )
            payload = _image_payload(to_number, media_id, message_text.caption)
        else:
            payload = build_whatsapp_payload(
                to_number,
                message_text,
                template_name=template_name,
                template_parameters=template_parameters,
            )
    except (TypeError, ValueError) as exc:
        print(f"Mensaje saliente de WhatsApp invalido: {exc}")
        invalid_result = WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.PERMANENT,
            error_code="invalid_payload",
            error_message=str(exc),
        )
        if isinstance(message_text, WhatsAppImage):
            return await _image_failure_with_fallback(
                to_number,
                invalid_result,
                ambiguous=False,
            )
        return invalid_result

    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    client = get_whatsapp_client()
    try:
        response = await client.post(url, headers=headers, json=payload)
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        print(f"Error de conexión al enviar mensaje de WhatsApp: {type(exc).__name__}: {exc}")
        result = WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.RETRYABLE,
            error_code="connect_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        if isinstance(message_text, WhatsAppImage):
            return await _image_failure_with_fallback(to_number, result, ambiguous=False)
        return result
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
        print(f"Timeout ambiguo al enviar mensaje de WhatsApp: {type(exc).__name__}: {exc}")
        result = WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.UNKNOWN,
            error_code="ambiguous_timeout",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        if isinstance(message_text, WhatsAppImage):
            return await _image_failure_with_fallback(to_number, result, ambiguous=True)
        return result
    except Exception as exc:
        print(f"Excepción inesperada de red al enviar mensaje de WhatsApp: {type(exc).__name__}: {exc}")
        result = WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.UNKNOWN,
            error_code="unexpected_network_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        if isinstance(message_text, WhatsAppImage):
            return await _image_failure_with_fallback(to_number, result, ambiguous=True)
        return result

    if response.status_code == 200:
        wamid = None
        try:
            data = response.json()
            messages = data.get("messages", [])
            if messages and isinstance(messages[0], dict):
                wamid = messages[0].get("id")
        except Exception:
            wamid = None
        print(f"Mensaje enviado a {payload['to']}")
        return WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.SUCCESS,
            message_id=wamid,
            status_code=200,
        )

    if response.status_code in {429, 500, 502, 503, 504}:
        print(f"Error reintentable al enviar el mensaje: {response.status_code} {response.text}")
        result = WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.RETRYABLE,
            status_code=response.status_code,
            error_code=f"http_{response.status_code}",
            error_message=response.text[:500],
        )
        if isinstance(message_text, WhatsAppImage):
            return await _image_failure_with_fallback(to_number, result, ambiguous=False)
        return result

    print(f"Error permanente al enviar el mensaje: {response.status_code} {response.text}")
    result = WhatsAppSendResult(
        status=WhatsAppDeliveryStatus.PERMANENT,
        status_code=response.status_code,
        error_code=f"http_{response.status_code}",
        error_message=response.text[:500],
    )
    if isinstance(message_text, WhatsAppImage):
        return await _image_failure_with_fallback(to_number, result, ambiguous=False)
    return result


async def send_whatsapp_message(
    to_number: str,
    message_text: str | OutboundWhatsAppMessage | None = None,
    *,
    template_name: str | None = None,
    template_parameters: list[str] | None = None,
) -> bool:
    """Send a text, interactive message or approved template through Meta."""
    result = await send_whatsapp_message_detailed(
        to_number,
        message_text,
        template_name=template_name,
        template_parameters=template_parameters,
    )
    return result.is_success or result.suppress_retry


def _validate_image(message: WhatsAppImage) -> None:
    if message.mime_type != "image/png":
        raise ValueError("only PNG images are supported")
    if not message.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("image content is not a valid PNG")
    if not 1 <= len(message.content) <= 5 * 1024 * 1024:
        raise ValueError("image must be between 1 byte and 5 MB")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", message.filename):
        raise ValueError("image filename is invalid")
    _optional_bounded_text(message.caption, field="image caption", maximum=1024)


def _image_payload(to_number: str, media_id: str, caption: str | None) -> dict:
    image = {"id": media_id}
    if caption:
        image["caption"] = caption
    return {
        "messaging_product": "whatsapp",
        "to": normalize_whatsapp_number(to_number),
        "type": "image",
        "image": image,
    }


async def _upload_whatsapp_image(
    message: WhatsAppImage,
    *,
    api_token: str,
    phone_id: str,
    api_version: str,
) -> tuple[WhatsAppSendResult, str | None]:
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/media"
    client = get_whatsapp_client()
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_token}"},
            data={"messaging_product": "whatsapp", "type": message.mime_type},
            files={"file": (message.filename, message.content, message.mime_type)},
        )
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        return WhatsAppSendResult(
            WhatsAppDeliveryStatus.RETRYABLE,
            error_code="media_connect_error",
            error_message=f"{type(exc).__name__}: {exc}",
        ), None
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
        return WhatsAppSendResult(
            WhatsAppDeliveryStatus.UNKNOWN,
            error_code="media_ambiguous_timeout",
            error_message=f"{type(exc).__name__}: {exc}",
        ), None
    except Exception as exc:
        return WhatsAppSendResult(
            WhatsAppDeliveryStatus.UNKNOWN,
            error_code="media_unexpected_network_error",
            error_message=f"{type(exc).__name__}: {exc}",
        ), None

    if response.status_code == 200:
        try:
            media_id = str(response.json().get("id") or "").strip()
        except Exception:
            media_id = ""
        if media_id:
            return WhatsAppSendResult(
                WhatsAppDeliveryStatus.SUCCESS,
                message_id=media_id,
                status_code=200,
            ), media_id
        return WhatsAppSendResult(
            WhatsAppDeliveryStatus.PERMANENT,
            status_code=200,
            error_code="missing_media_id",
            error_message="Meta did not return a media id",
        ), None

    retryable = response.status_code in {429, 500, 502, 503, 504}
    return WhatsAppSendResult(
        WhatsAppDeliveryStatus.RETRYABLE if retryable else WhatsAppDeliveryStatus.PERMANENT,
        status_code=response.status_code,
        error_code=f"media_http_{response.status_code}",
        error_message=response.text[:500],
    ), None


async def _image_failure_with_fallback(
    to_number: str,
    failure: WhatsAppSendResult,
    *,
    ambiguous: bool,
) -> WhatsAppSendResult:
    fallback = await send_whatsapp_message_detailed(
        to_number,
        WhatsAppText(
            "No pude enviar el gráfico en este momento. "
            "Podés volver a pedírmelo en unos minutos."
        ),
    )
    if fallback.is_success:
        return fallback
    if ambiguous:
        return WhatsAppSendResult(
            status=failure.status,
            error_code=failure.error_code,
            status_code=failure.status_code,
            error_message=failure.error_message,
            suppress_retry=True,
        )
    return failure


async def send_whatsapp_reaction(
    to_number: str,
    message_id: str,
    emoji: str = "⏳",
) -> bool:
    """Send an immediate reaction emoji for an inbound WhatsApp message."""
    api_token = os.getenv("WHATSAPP_API_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    api_version = whatsapp_graph_api_version()
    if not api_token or not phone_id:
        print("Falta WHATSAPP_API_TOKEN o WHATSAPP_PHONE_ID. No se puede enviar la reacción.")
        return False
    if api_version is None:
        print("WHATSAPP_GRAPH_API_VERSION tiene un formato invalido.")
        return False

    normalized_to = normalize_whatsapp_number(to_number)
    if not normalized_to or not message_id or not emoji:
        print("Datos insuficientes para enviar la reacción de WhatsApp.")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalized_to,
        "type": "reaction",
        "reaction": {
            "message_id": message_id,
            "emoji": emoji,
        },
    }

    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        client = get_whatsapp_client()
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Error al enviar la reacción: {response.status_code}")
            return False
        return True
    except Exception as exc:
        print(f"Excepción al enviar la reacción de WhatsApp: {type(exc).__name__}")
        return False


async def send_whatsapp_typing_indicator(message_id: str) -> bool:
    """Show the typing indicator for an inbound WhatsApp message (marks it read)."""
    api_token = os.getenv("WHATSAPP_API_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    api_version = whatsapp_graph_api_version()
    if not api_token or not phone_id:
        print(
            "Falta WHATSAPP_API_TOKEN o WHATSAPP_PHONE_ID. "
            "No se puede enviar el typing indicator."
        )
        return False
    if api_version is None:
        print("WHATSAPP_GRAPH_API_VERSION tiene un formato invalido.")
        return False
    if not message_id:
        print("Datos insuficientes para enviar el typing indicator de WhatsApp.")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }

    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        client = get_whatsapp_client()
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Error al enviar el typing indicator: {response.status_code}")
            return False
        return True
    except Exception as exc:
        print(f"Excepción al enviar el typing indicator de WhatsApp: {type(exc).__name__}")
        return False
