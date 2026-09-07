import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from app.models.database import DashboardLoginLink, SessionLocal, Usuario
from app.services.onboarding import (
    DEFAULT_REGISTRATION_URL,
    _non_negative_int_from_env,
    _positive_int_from_env,
    _utc_datetime,
)


DEFAULT_LOGIN_URL = "http://localhost:8000/login"
DEFAULT_LINK_TTL_MINUTES = 10
DEFAULT_RESEND_COOLDOWN_SECONDS = 60
DEFAULT_MAX_RESENDS = 3


class DashboardLinkDecision(str, Enum):
    NOT_ELIGIBLE = "not_eligible"
    SEND_LINK = "send_link"
    SUPPRESS_RESPONSE = "suppress_response"
    ERROR = "error"


@dataclass(frozen=True)
class DashboardLinkResult:
    decision: DashboardLinkDecision
    login_url: str | None = None
    link_ttl_minutes: int = DEFAULT_LINK_TTL_MINUTES


@dataclass(frozen=True)
class DashboardLinkConfig:
    login_url: str = DEFAULT_LOGIN_URL
    link_ttl_minutes: int = DEFAULT_LINK_TTL_MINUTES
    resend_cooldown_seconds: int = DEFAULT_RESEND_COOLDOWN_SECONDS
    max_resends: int = DEFAULT_MAX_RESENDS

    @classmethod
    def from_env(cls) -> "DashboardLinkConfig":
        return cls(
            login_url=_login_url_from_frontend_host(
                os.getenv("ONBOARDING_REGISTRATION_URL")
            ),
            link_ttl_minutes=_positive_int_from_env(
                "DASHBOARD_LOGIN_TTL_MINUTES",
                DEFAULT_LINK_TTL_MINUTES,
            ),
            resend_cooldown_seconds=_positive_int_from_env(
                "DASHBOARD_LOGIN_RESEND_COOLDOWN_SECONDS",
                DEFAULT_RESEND_COOLDOWN_SECONDS,
            ),
            max_resends=_non_negative_int_from_env(
                "DASHBOARD_LOGIN_MAX_RESENDS",
                DEFAULT_MAX_RESENDS,
            ),
        )


def _login_url_from_frontend_host(onboarding_url: str | None) -> str:
    """Build the dashboard login URL from the same host ONBOARDING_REGISTRATION_URL
    already points to, instead of a second config value that could drift out
    of sync with it — both /registro and /login are served by the same
    frontend deployment.
    """
    candidate = (onboarding_url or DEFAULT_REGISTRATION_URL).strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_LOGIN_URL
    return urlunsplit((parsed.scheme, parsed.netloc, "/login", "", ""))


class DashboardLinkService:
    """Genera enlaces mágicos de acceso al dashboard para usuarios ya vinculados.

    Independiente de OnboardingService (STK-143/144), que sólo cubre altas
    nuevas: acá el usuario ya tiene `usuario.auth_user_id` asignado y sólo
    necesita una forma de volver a entrar desde WhatsApp.
    """

    @classmethod
    def generate_or_reuse(
        cls,
        whatsapp_id: str,
        *,
        session_factory=None,
        config: DashboardLinkConfig | None = None,
        now: datetime | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> DashboardLinkResult:
        if not isinstance(whatsapp_id, str) or not whatsapp_id.strip():
            return DashboardLinkResult(DashboardLinkDecision.ERROR)

        if date_from is not None and not isinstance(date_from, date):
            return DashboardLinkResult(DashboardLinkDecision.ERROR)
        if date_to is not None and not isinstance(date_to, date):
            return DashboardLinkResult(DashboardLinkDecision.ERROR)

        config = config or DashboardLinkConfig.from_env()
        current_time = _utc_datetime(now or datetime.now(timezone.utc))
        session = None

        try:
            session = (session_factory or SessionLocal)()
            usuario_id = cls._linked_usuario_id(session, whatsapp_id)
            if usuario_id is None:
                return DashboardLinkResult(DashboardLinkDecision.NOT_ELIGIBLE)

            pending_link = cls._pending_link(session, usuario_id)
            if pending_link is not None:
                return cls._handle_pending(
                    session,
                    pending_link,
                    current_time,
                    config,
                    date_from=date_from,
                    date_to=date_to,
                )

            return cls._create_pending(
                session,
                usuario_id,
                current_time,
                config,
                date_from=date_from,
                date_to=date_to,
            )
        except IntegrityError:
            if session is None:
                return DashboardLinkResult(DashboardLinkDecision.ERROR)
            session.rollback()
            return cls._recover_from_concurrent_insert(
                session,
                whatsapp_id,
                current_time,
                config,
                date_from=date_from,
                date_to=date_to,
            )
        except Exception as exc:
            if session is not None:
                session.rollback()
            print(f"[DASHBOARD_LINK] Controlled error: {type(exc).__name__}")
            return DashboardLinkResult(DashboardLinkDecision.ERROR)
        finally:
            if session is not None:
                session.close()

    @staticmethod
    def _linked_usuario_id(session, whatsapp_id: str):
        """Devuelve el id del usuario sólo si ya está vinculado (auth_user_id set).

        No distingue "no existe" de "existe pero no vinculado" en el resultado:
        en ambos casos no hay nada que ofrecer por WhatsApp, y no queremos que
        la respuesta del bot revele si un número está registrado o no.
        """
        row = (
            session.query(Usuario.id, Usuario.auth_user_id)
            .filter(Usuario.whatsapp_id == whatsapp_id)
            .first()
        )
        if row is None or row.auth_user_id is None:
            return None
        return row.id

    @staticmethod
    def _pending_link(session, usuario_id):
        return (
            session.query(DashboardLoginLink)
            .filter(
                DashboardLoginLink.usuario_id == usuario_id,
                DashboardLoginLink.estado == "pendiente",
            )
            .order_by(DashboardLoginLink.creado_en.desc())
            .with_for_update()
            .first()
        )

    @classmethod
    def _handle_pending(
        cls,
        session,
        link,
        now,
        config,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ):
        if _utc_datetime(link.expira_en) <= now:
            link.estado = "vencido"
            session.flush()
            return cls._create_pending(
                session,
                link.usuario_id,
                now,
                config,
                date_from=date_from,
                date_to=date_to,
            )

        if link.reenvios >= config.max_resends:
            return DashboardLinkResult(DashboardLinkDecision.SUPPRESS_RESPONSE)

        if link.ultimo_envio_en is not None:
            cooldown_ends_at = _utc_datetime(link.ultimo_envio_en) + timedelta(
                seconds=config.resend_cooldown_seconds
            )
            if now < cooldown_ends_at:
                return DashboardLinkResult(DashboardLinkDecision.SUPPRESS_RESPONSE)

        token = secrets.token_urlsafe(32)
        link.token_hash = cls._token_hash(token)
        link.expira_en = now + timedelta(minutes=config.link_ttl_minutes)
        link.reenvios += 1
        link.ultimo_envio_en = now
        session.commit()
        return cls._send_result(token, config, date_from=date_from, date_to=date_to)

    @classmethod
    def _create_pending(
        cls,
        session,
        usuario_id,
        now,
        config,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ):
        token = secrets.token_urlsafe(32)
        link = DashboardLoginLink(
            usuario_id=usuario_id,
            token_hash=cls._token_hash(token),
            estado="pendiente",
            expira_en=now + timedelta(minutes=config.link_ttl_minutes),
            reenvios=0,
            ultimo_envio_en=now,
            creado_en=now,
            actualizado_en=now,
        )
        session.add(link)
        session.commit()
        return cls._send_result(token, config, date_from=date_from, date_to=date_to)

    @classmethod
    def _recover_from_concurrent_insert(
        cls,
        session,
        whatsapp_id,
        now,
        config,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ):
        try:
            usuario_id = cls._linked_usuario_id(session, whatsapp_id)
            if usuario_id is None:
                return DashboardLinkResult(DashboardLinkDecision.ERROR)
            pending_link = cls._pending_link(session, usuario_id)
            if pending_link is None:
                return DashboardLinkResult(DashboardLinkDecision.ERROR)
            return cls._handle_pending(
                session,
                pending_link,
                now,
                config,
                date_from=date_from,
                date_to=date_to,
            )
        except Exception as exc:
            session.rollback()
            print(f"[DASHBOARD_LINK] Controlled recovery error: {type(exc).__name__}")
            return DashboardLinkResult(DashboardLinkDecision.ERROR)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _send_result(
        token: str,
        config: DashboardLinkConfig,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> DashboardLinkResult:
        query_params: dict[str, str] = {"token": token}
        if date_from is not None:
            query_params["date_from"] = date_from.isoformat()
        if date_to is not None:
            query_params["date_to"] = date_to.isoformat()

        parsed = urlsplit(config.login_url)
        login_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query_params),
                "",
            )
        )
        return DashboardLinkResult(
            decision=DashboardLinkDecision.SEND_LINK,
            login_url=login_url,
            link_ttl_minutes=config.link_ttl_minutes,
        )
