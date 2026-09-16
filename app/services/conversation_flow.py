from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.models.database import (
    ConversationFlow,
    ConversationFlowVersion,
    SessionLocal,
)


class ConversationFlowError(Exception):
    """Base para errores controlados del ciclo administrativo."""


class ConversationFlowNotFound(ConversationFlowError):
    pass


class ConversationFlowConflict(ConversationFlowError):
    pass


@dataclass(frozen=True)
class FlowVersionSnapshot:
    id: UUID
    flow_id: UUID
    version_number: int
    status: str
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


@dataclass(frozen=True)
class FlowSnapshot:
    id: UUID
    slug: str
    name: str
    event_key: str
    status: str
    created_at: datetime
    updated_at: datetime
    draft: FlowVersionSnapshot | None = None
    published: FlowVersionSnapshot | None = None


class ConversationFlowService:
    @classmethod
    def create(
        cls,
        *,
        slug: str,
        name: str,
        event_key: str,
        definition: dict[str, Any],
        session_factory=None,
    ) -> FlowSnapshot:
        session = (session_factory or SessionLocal)()
        try:
            flow = ConversationFlow(
                slug=slug.strip(),
                name=name.strip(),
                event_key=event_key.strip(),
            )
            session.add(flow)
            session.flush()
            session.add(
                ConversationFlowVersion(
                    flow_id=flow.id,
                    version_number=1,
                    definition=deepcopy(definition),
                )
            )
            session.commit()
            return cls._snapshot(session, flow.id)
        except IntegrityError as exc:
            session.rollback()
            raise ConversationFlowConflict(
                "El slug o el evento ya pertenecen a otro recorrido."
            ) from exc
        finally:
            session.close()

    @classmethod
    def list(cls, *, session_factory=None) -> list[FlowSnapshot]:
        session = (session_factory or SessionLocal)()
        try:
            flow_ids = [
                row[0]
                for row in session.query(ConversationFlow.id)
                .order_by(ConversationFlow.name, ConversationFlow.slug)
                .all()
            ]
            return [cls._snapshot(session, flow_id) for flow_id in flow_ids]
        finally:
            session.close()

    @classmethod
    def get(cls, flow_id: UUID, *, session_factory=None) -> FlowSnapshot:
        session = (session_factory or SessionLocal)()
        try:
            return cls._snapshot(session, flow_id)
        finally:
            session.close()

    @classmethod
    def save_draft(
        cls,
        flow_id: UUID,
        *,
        definition: dict[str, Any],
        name: str | None = None,
        session_factory=None,
    ) -> FlowSnapshot:
        session = (session_factory or SessionLocal)()
        try:
            flow = cls._flow_or_raise(session, flow_id)
            if flow.status == "archived":
                raise ConversationFlowConflict(
                    "Un recorrido retirado no admite nuevos borradores."
                )
            if name is not None:
                normalized_name = name.strip()
                if not normalized_name:
                    raise ConversationFlowConflict("El nombre no puede estar vacio.")
                flow.name = normalized_name

            draft = cls._version_for_status(session, flow_id, "draft")
            if draft is None:
                last_number = (
                    session.query(ConversationFlowVersion.version_number)
                    .filter(ConversationFlowVersion.flow_id == flow_id)
                    .order_by(ConversationFlowVersion.version_number.desc())
                    .limit(1)
                    .scalar()
                    or 0
                )
                draft = ConversationFlowVersion(
                    flow_id=flow_id,
                    version_number=last_number + 1,
                    definition=deepcopy(definition),
                )
                session.add(draft)
            else:
                draft.definition = deepcopy(definition)

            session.commit()
            return cls._snapshot(session, flow_id)
        except IntegrityError as exc:
            session.rollback()
            raise ConversationFlowConflict(
                "No se pudo guardar el borrador por un cambio concurrente."
            ) from exc
        finally:
            session.close()

    @classmethod
    def publish(
        cls,
        flow_id: UUID,
        *,
        session_factory=None,
        now: datetime | None = None,
    ) -> FlowSnapshot:
        session = (session_factory or SessionLocal)()
        try:
            flow = cls._flow_or_raise(session, flow_id)
            if flow.status == "archived":
                raise ConversationFlowConflict(
                    "Un recorrido retirado no se puede publicar."
                )
            draft = cls._version_for_status(session, flow_id, "draft")
            if draft is None:
                raise ConversationFlowConflict("No hay un borrador para publicar.")

            published = cls._version_for_status(session, flow_id, "published")
            if published is not None:
                published.status = "retired"
                session.flush()

            draft.status = "published"
            draft.published_at = now or datetime.now(timezone.utc)
            session.commit()
            return cls._snapshot(session, flow_id)
        except IntegrityError as exc:
            session.rollback()
            raise ConversationFlowConflict(
                "No se pudo publicar por un cambio concurrente."
            ) from exc
        finally:
            session.close()

    @classmethod
    def archive(
        cls,
        flow_id: UUID,
        *,
        session_factory=None,
    ) -> FlowSnapshot:
        session = (session_factory or SessionLocal)()
        try:
            flow = cls._flow_or_raise(session, flow_id)
            flow.status = "archived"
            session.commit()
            return cls._snapshot(session, flow_id)
        finally:
            session.close()

    @staticmethod
    def _flow_or_raise(session, flow_id: UUID) -> ConversationFlow:
        flow = (
            session.query(ConversationFlow)
            .filter(ConversationFlow.id == flow_id)
            .first()
        )
        if flow is None:
            raise ConversationFlowNotFound("Recorrido no encontrado.")
        return flow

    @staticmethod
    def _version_for_status(session, flow_id: UUID, status: str):
        return (
            session.query(ConversationFlowVersion)
            .filter(
                ConversationFlowVersion.flow_id == flow_id,
                ConversationFlowVersion.status == status,
            )
            .first()
        )

    @classmethod
    def _snapshot(cls, session, flow_id: UUID) -> FlowSnapshot:
        flow = cls._flow_or_raise(session, flow_id)
        draft = cls._version_for_status(session, flow_id, "draft")
        published = cls._version_for_status(session, flow_id, "published")
        return FlowSnapshot(
            id=flow.id,
            slug=flow.slug,
            name=flow.name,
            event_key=flow.event_key,
            status=flow.status,
            created_at=cls._as_utc(flow.created_at),
            updated_at=cls._as_utc(flow.updated_at),
            draft=cls._version_snapshot(draft),
            published=cls._version_snapshot(published),
        )

    @classmethod
    def _version_snapshot(cls, version) -> FlowVersionSnapshot | None:
        if version is None:
            return None
        return FlowVersionSnapshot(
            id=version.id,
            flow_id=version.flow_id,
            version_number=version.version_number,
            status=version.status,
            definition=deepcopy(version.definition),
            created_at=cls._as_utc(version.created_at),
            updated_at=cls._as_utc(version.updated_at),
            published_at=(
                cls._as_utc(version.published_at) if version.published_at else None
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
