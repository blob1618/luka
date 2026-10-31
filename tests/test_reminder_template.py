"""Recordatorios sin template: no se reintentan en cada ciclo ni se marcan como enviados."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, Recordatorio, Usuario
from app import scheduler


def _factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


@pytest.mark.asyncio
async def test_missing_template_logs_once_and_does_not_mark_sent(monkeypatch, caplog):
    session_factory, engine = _factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.delenv("WHATSAPP_REMINDER_TEMPLATE_NAME", raising=False)

    now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        user = Usuario(
            id=uuid.uuid4(),
            nombre="Test",
            email="template@test.invalid",
            whatsapp_id="5491155551234",
            ultimo_mensaje_en=None,
        )
        session.add(user)
        session.flush()
        reminder = Recordatorio(
            usuario_id=user.id,
            titulo="Luz",
            dia_del_mes=15,
            estado="activo",
        )
        session.add(reminder)
        session.commit()
        reminder_id = reminder.id

    with caplog.at_level("WARNING", logger="app.scheduler"):
        await scheduler.check_reminders(_now=now)
        await scheduler.check_reminders(_now=now)

    messages = [record.message for record in caplog.records
                if "REMINDER_TEMPLATE_MISSING" in record.message]
    assert len(messages) == 1

    with session_factory() as session:
        stored = session.get(Recordatorio, reminder_id)
        assert stored is not None and stored.ultimo_aviso_enviado is None

    engine.dispose()
