import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from app.models.database import OnboardingInvitacion, SessionLocal, Usuario


DEFAULT_REGISTRATION_URL = "http://localhost:8000/registro"
DEFAULT_INVITATION_TTL_MINUTES = 30
DEFAULT_RESEND_COOLDOWN_SECONDS = 60
DEFAULT_MAX_RESENDS = 3


class OnboardingDecision(str, Enum):
    KNOWN_USER = "known_user"
    SEND_INVITATION = "send_invitation"
    SUPPRESS_RESPONSE = "suppress_response"
    ERROR = "error"


@dataclass(frozen=True)
class OnboardingResult:
    decision: OnboardingDecision
    registration_url: str | None = None
    invitation_ttl_minutes: int = DEFAULT_INVITATION_TTL_MINUTES


@dataclass(frozen=True)
class OnboardingConfig:
    registration_url: str = DEFAULT_REGISTRATION_URL
    invitation_ttl_minutes: int = DEFAULT_INVITATION_TTL_MINUTES
    resend_cooldown_seconds: int = DEFAULT_RESEND_COOLDOWN_SECONDS
    max_resends: int = DEFAULT_MAX_RESENDS

    @classmethod
    def from_env(cls) -> "OnboardingConfig":
        return cls(
            registration_url=_valid_registration_url(
                os.getenv("ONBOARDING_REGISTRATION_URL")
            ),
            invitation_ttl_minutes=_positive_int_from_env(
                "ONBOARDING_INVITATION_TTL_MINUTES",
                DEFAULT_INVITATION_TTL_MINUTES,
            ),
            resend_cooldown_seconds=_positive_int_from_env(
                "ONBOARDING_RESEND_COOLDOWN_SECONDS",
                DEFAULT_RESEND_COOLDOWN_SECONDS,
            ),
            max_resends=_non_negative_int_from_env(
                "ONBOARDING_MAX_RESENDS",
                DEFAULT_MAX_RESENDS,
            ),
        )


def _positive_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _non_negative_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _valid_registration_url(value: str | None) -> str:
    candidate = (value or DEFAULT_REGISTRATION_URL).strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_REGISTRATION_URL
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class OnboardingService:
    @classmethod
    def prepare_whatsapp_message(
        cls,
        sender_phone: str,
        *,
        session_factory=None,
        config: OnboardingConfig | None = None,
        now: datetime | None = None,
    ) -> OnboardingResult:
        if not isinstance(sender_phone, str) or not sender_phone.strip():
            return OnboardingResult(OnboardingDecision.ERROR)

        config = config or OnboardingConfig.from_env()
        current_time = _utc_datetime(now or datetime.now(timezone.utc))
        session = None

        try:
            session = (session_factory or SessionLocal)()
            known_user = (
                session.query(Usuario.id)
                .filter(Usuario.whatsapp_id == sender_phone)
                .first()
            )
            if known_user is not None:
                return OnboardingResult(OnboardingDecision.KNOWN_USER)

            pending_invitation = cls._pending_invitation(session, sender_phone)
            if pending_invitation is not None:
                return cls._handle_pending(
                    session,
                    pending_invitation,
                    current_time,
                    config,
                )

            return cls._create_pending(
                session,
                sender_phone,
                current_time,
                config,
            )
        except IntegrityError:
            if session is None:
                return OnboardingResult(OnboardingDecision.ERROR)
            session.rollback()
            return cls._recover_from_concurrent_insert(
                session,
                sender_phone,
                current_time,
                config,
            )
        except Exception as exc:
            if session is not None:
                session.rollback()
            print(f"[ONBOARDING] Controlled error: {type(exc).__name__}")
            return OnboardingResult(OnboardingDecision.ERROR)
        finally:
            if session is not None:
                session.close()

    @staticmethod
    def _pending_invitation(session, sender_phone: str):
        return (
            session.query(OnboardingInvitacion)
            .filter(
                OnboardingInvitacion.whatsapp_id == sender_phone,
                OnboardingInvitacion.estado == "pendiente",
            )
            .order_by(OnboardingInvitacion.creado_en.desc())
            .with_for_update()
            .first()
        )

    @classmethod
    def _handle_pending(cls, session, invitation, now, config):
        if _utc_datetime(invitation.expira_en) <= now:
            invitation.estado = "vencida"
            session.flush()
            return cls._create_pending(
                session,
                invitation.whatsapp_id,
                now,
                config,
            )

        if invitation.reenvios >= config.max_resends:
            return OnboardingResult(OnboardingDecision.SUPPRESS_RESPONSE)

        if invitation.ultimo_envio_en is not None:
            cooldown_ends_at = _utc_datetime(
                invitation.ultimo_envio_en
            ) + timedelta(seconds=config.resend_cooldown_seconds)
            if now < cooldown_ends_at:
                return OnboardingResult(OnboardingDecision.SUPPRESS_RESPONSE)

        token = secrets.token_urlsafe(32)
        invitation.token_hash = cls._token_hash(token)
        invitation.expira_en = now + timedelta(
            minutes=config.invitation_ttl_minutes
        )
        invitation.reenvios += 1
        invitation.ultimo_envio_en = now
        session.commit()
        return cls._send_result(token, config)

    @classmethod
    def _create_pending(cls, session, sender_phone, now, config):
        token = secrets.token_urlsafe(32)
        invitation = OnboardingInvitacion(
            whatsapp_id=sender_phone,
            token_hash=cls._token_hash(token),
            estado="pendiente",
            expira_en=now + timedelta(minutes=config.invitation_ttl_minutes),
            intentos=0,
            reenvios=0,
            ultimo_envio_en=now,
            creado_en=now,
            actualizado_en=now,
        )
        session.add(invitation)
        session.commit()
        return cls._send_result(token, config)

    @classmethod
    def _recover_from_concurrent_insert(
        cls,
        session,
        sender_phone,
        now,
        config,
    ):
        try:
            pending_invitation = cls._pending_invitation(session, sender_phone)
            if pending_invitation is None:
                return OnboardingResult(OnboardingDecision.ERROR)
            return cls._handle_pending(
                session,
                pending_invitation,
                now,
                config,
            )
        except Exception as exc:
            session.rollback()
            print(f"[ONBOARDING] Controlled recovery error: {type(exc).__name__}")
            return OnboardingResult(OnboardingDecision.ERROR)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _send_result(token: str, config: OnboardingConfig) -> OnboardingResult:
        parsed = urlsplit(config.registration_url)
        registration_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode({"token": token}),
                "",
            )
        )
        return OnboardingResult(
            decision=OnboardingDecision.SEND_INVITATION,
            registration_url=registration_url,
            invitation_ttl_minutes=config.invitation_ttl_minutes,
        )
