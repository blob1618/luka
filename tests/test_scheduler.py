import uuid
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Recordatorio, Usuario


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _seed_user(session, whatsapp_id="5491155551234", ultimo_mensaje_en=None):
    user = Usuario(
        id=uuid.uuid4(),
        nombre="Test",
        email=f"{whatsapp_id}@test.com",
        whatsapp_id=whatsapp_id,
        ultimo_mensaje_en=ultimo_mensaje_en,
    )
    session.add(user)
    session.commit()
    return user


def _seed_reminder(session, user, dia_del_mes, titulo="Luz", monto=None, estado="activo", ultimo_aviso=None):
    rec = Recordatorio(
        usuario_id=user.id,
        titulo=titulo,
        dia_del_mes=dia_del_mes,
        monto=monto,
        estado=estado,
        ultimo_aviso_enviado=ultimo_aviso,
    )
    session.add(rec)
    session.commit()
    return rec


class TestCheckReminders:
    @pytest.mark.asyncio
    async def test_sends_reminder_day_before(self, monkeypatch):
        """Reminder with dia_del_mes=15 sends on day 14."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=15, titulo="Luz")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        args = mock_send.call_args
        assert "Luz" in args[0][1] or "Luz" in str(args)
        assert "mañana" in args[0][1].lower()

    @pytest.mark.asyncio
    async def test_does_not_send_wrong_day(self, monkeypatch):
        """Reminder with dia_del_mes=20 should NOT send on day 14."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=20, titulo="Internet")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_resend_same_month(self, monkeypatch):
        """Reminder already sent this month should not resend."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(
            session, user, dia_del_mes=15, titulo="Luz",
            ultimo_aviso=date(2026, 7, 14),
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_with_monto(self, monkeypatch):
        """Reminder with monto includes amount in message."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(
            session, user, dia_del_mes=15, titulo="Alquiler",
            monto=Decimal("350000"),
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "350000" in msg

    @pytest.mark.asyncio
    async def test_skips_paused_reminder(self, monkeypatch):
        """Paused reminders should not be sent."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(
            session, user, dia_del_mes=15, titulo="Luz",
            estado="pausado",
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_outside_window_sends_with_date(self, monkeypatch):
        """When user window is closed and template is missing, the send is skipped."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        # Last message 48h ago — window closed
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=48))
        _seed_reminder(session, user, dia_del_mes=15, titulo="Luz")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_outside_window_uses_template_payload(self, monkeypatch):
        """When the window is closed and template exists, template payload is used."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("WHATSAPP_REMINDER_TEMPLATE_NAME", "reminder_payment")

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=48))
        _seed_reminder(session, user, dia_del_mes=15, titulo="Alquiler", monto=Decimal("350000"))
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["template_name"] == "reminder_payment"
        assert kwargs["template_parameters"] == ["Alquiler", "15/07", "350000"]

    @pytest.mark.asyncio
    async def test_uses_buenos_aires_timezone_for_alert_day(self, monkeypatch):
        """UTC day rollover should still alert using Buenos Aires local date."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 8, 1, 2, 30, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=1, titulo="Internet")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_dia_1_alerts_last_day_prev_month(self, monkeypatch):
        """Reminder for day 1 should alert on last day of previous month."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        # July 31 is last day of July; reminder for day 1 (Aug 1) should fire
        now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=1, titulo="Alquiler")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        assert "Alquiler" in mock_send.call_args[0][1]
