import uuid
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, MovimientoFinanciero, Recordatorio, Usuario


def _make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _seed_user(
    session,
    whatsapp_id="5491155551234",
    ultimo_mensaje_en=None,
    proactivo_habilitado=True,
    proactivo_ultimo_envio=None,
):
    user = Usuario(
        id=uuid.uuid4(),
        nombre="Test",
        email=f"{whatsapp_id}@test.com",
        whatsapp_id=whatsapp_id,
        ultimo_mensaje_en=ultimo_mensaje_en,
        proactivo_habilitado=proactivo_habilitado,
        proactivo_ultimo_envio=proactivo_ultimo_envio,
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
        monkeypatch.delenv("WHATSAPP_REMINDER_TEMPLATE_NAME", raising=False)

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
        monkeypatch.setenv("WHATSAPP_REMINDER_TEMPLATE_NAME", "recordatorio_pago_vencimiento")

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
        assert kwargs["template_name"] == "recordatorio_pago_vencimiento"
        assert kwargs["template_parameters"] == ["Alquiler", "15/07", "$350000 ARS"]

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

    @pytest.mark.asyncio
    async def test_template_amount_format_with_currency(self, monkeypatch):
        """Template parameter {{3}} should be '$3500 ARS' format."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("WHATSAPP_REMINDER_TEMPLATE_NAME", "recordatorio_pago_vencimiento")

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=48))
        _seed_reminder(session, user, dia_del_mes=15, titulo="Netflix", monto=Decimal("3500"))
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["template_parameters"][2] == "$3500 ARS"

    @pytest.mark.asyncio
    async def test_template_amount_no_especificado(self, monkeypatch):
        """Template parameter {{3}} should be 'no especificado' when monto is None."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("WHATSAPP_REMINDER_TEMPLATE_NAME", "recordatorio_pago_vencimiento")

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=48))
        _seed_reminder(session, user, dia_del_mes=15, titulo="Luz", monto=None)
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["template_parameters"][2] == "no especificado"

    @pytest.mark.asyncio
    async def test_dia_31_in_february_clamps(self, monkeypatch):
        """Reminder for day 31 in February should fire on Feb 27 (28-1)."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 2, 27, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=31, titulo="Internet")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "28/02" in msg or "Internet" in msg

    @pytest.mark.asyncio
    async def test_dia_30_in_february_leap_year(self, monkeypatch):
        """Reminder for day 30 in February leap year (2028) should fire on Feb 28 (29-1)."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2028, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user, dia_del_mes=30, titulo="Netflix")
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_failure_does_not_mark_as_sent(self, monkeypatch):
        """When send_whatsapp_message returns False, ultimo_aviso_enviado stays None."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        rec = _seed_reminder(session, user, dia_del_mes=15, titulo="Luz")
        rec_id = rec.id
        session.close()

        mock_send = AsyncMock(return_value=False)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        mock_send.assert_called_once()
        session = SessionFactory()
        saved = session.query(Recordatorio).filter(Recordatorio.id == rec_id).first()
        assert saved.ultimo_aviso_enviado is None
        session.close()

    @pytest.mark.asyncio
    async def test_send_failure_one_user_continues_others(self, monkeypatch):
        """When send fails for user A, user B's reminder still sends."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user_a = _seed_user(session, whatsapp_id="549111", ultimo_mensaje_en=now - timedelta(hours=2))
        user_b = _seed_user(session, whatsapp_id="549222", ultimo_mensaje_en=now - timedelta(hours=2))
        _seed_reminder(session, user_a, dia_del_mes=15, titulo="Luz")
        _seed_reminder(session, user_b, dia_del_mes=15, titulo="Internet")
        session.close()

        async def fail_first(whatsapp_id, *args, **kwargs):
            if whatsapp_id == "549111":
                return False
            return True

        mock_send = AsyncMock(side_effect=fail_first)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders
        await check_reminders(_now=now)

        assert mock_send.await_count == 2
        # Both users' reminders should have been attempted
        calls = [call.args[0] for call in mock_send.await_args_list]
        assert "549111" in calls
        assert "549222" in calls

    @pytest.mark.asyncio
    async def test_rollover_next_month_after_alert_sent(self, monkeypatch):
        """After sending on July 14, the August run on Aug 14 should send again."""
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)

        session = SessionFactory()
        july = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = _seed_user(session, ultimo_mensaje_en=july - timedelta(hours=2))
        rec = _seed_reminder(session, user, dia_del_mes=15, titulo="Luz")
        rec_id = rec.id
        user_id = user.id
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_reminders

        # July send
        await check_reminders(_now=july)
        assert mock_send.await_count == 1

        # Verify ultimo_aviso_enviado was set
        session = SessionFactory()
        rec = session.query(Recordatorio).filter(Recordatorio.id == rec_id).first()
        assert rec.ultimo_aviso_enviado == date(2026, 7, 14)

        # August run — should send again because ultimo_aviso is in previous month
        mock_send.reset_mock()
        aug = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        user = session.query(Usuario).filter(Usuario.id == user_id).first()
        user.ultimo_mensaje_en = aug - timedelta(hours=2)
        session.commit()
        session.close()

        await check_reminders(_now=aug)
        assert mock_send.await_count == 1


class TestProactivePrompts:
    # 2026-07-14 23:00 UTC == 20:00 in Buenos Aires (UTC-3)
    NOW_20_LOCAL = datetime(2026, 7, 14, 23, 0, 0, tzinfo=timezone.utc)
    TODAY = date(2026, 7, 14)

    @pytest.mark.asyncio
    async def test_sends_within_window_when_enabled(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        user = _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        user_id = user.id
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import PROACTIVE_PROMPT_TEXT, check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_awaited_once_with("5491155551234", PROACTIVE_PROMPT_TEXT)
        session = SessionFactory()
        saved = session.query(Usuario).filter(Usuario.id == user_id).first()
        assert saved.proactivo_ultimo_envio == self.TODAY
        session.close()

    @pytest.mark.asyncio
    async def test_does_not_resend_same_day(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
            proactivo_ultimo_envio=self.TODAY,
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("horas_desde_ultimo_mensaje", [None, 25])
    async def test_skips_when_window_closed(self, monkeypatch, horas_desde_ultimo_mensaje):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        last_message_at = (
            None
            if horas_desde_ultimo_mensaje is None
            else self.NOW_20_LOCAL - timedelta(hours=horas_desde_ultimo_mensaje)
        )
        session = SessionFactory()
        _seed_user(session, ultimo_mensaje_en=last_message_at)
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
            proactivo_habilitado=False,
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_other_hour(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        # 12:00 UTC == 09:00 local
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        _seed_user(session, ultimo_mensaje_en=now - timedelta(hours=2))
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=now)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_hour_unset(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.delenv("PROACTIVE_PROMPT_HOUR", raising=False)

        session = SessionFactory()
        _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_hour", ["24", "-1", "abc", "20.5"])
    async def test_invalid_hour_disables_and_warns(self, monkeypatch, caplog, raw_hour):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", raw_hour)

        session = SessionFactory()
        _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        with caplog.at_level("WARNING", logger="app.scheduler"):
            await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_not_called()
        assert "PROACTIVE_CONFIG_ERROR" in caplog.text

    def _seed_movement(self, session, user, fecha_movimiento, anulado_en=None):
        session.add(
            MovimientoFinanciero(
                usuario_id=user.id,
                tipo="egreso",
                cantidad=Decimal("100"),
                fecha_movimiento=fecha_movimiento,
                anulado_en=anulado_en,
            )
        )
        session.commit()

    @pytest.mark.asyncio
    async def test_skips_only_users_who_registered_movement_today(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        active_window = self.NOW_20_LOCAL - timedelta(hours=2)
        registered = _seed_user(session, whatsapp_id="549111", ultimo_mensaje_en=active_window)
        _seed_user(session, whatsapp_id="549222", ultimo_mensaje_en=active_window)
        self._seed_movement(session, registered, fecha_movimiento=self.TODAY)
        registered_id = registered.id
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import PROACTIVE_PROMPT_TEXT, check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        mock_send.assert_awaited_once_with("549222", PROACTIVE_PROMPT_TEXT)
        session = SessionFactory()
        saved = session.query(Usuario).filter(Usuario.id == registered_id).first()
        assert saved.proactivo_ultimo_envio is None
        session.close()

    @pytest.mark.asyncio
    async def test_sends_when_only_movements_are_old_or_annulled(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        user = _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        self._seed_movement(session, user, fecha_movimiento=self.TODAY - timedelta(days=1))
        self._seed_movement(
            session, user, fecha_movimiento=self.TODAY, anulado_en=self.NOW_20_LOCAL
        )
        session.close()

        mock_send = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_send_failure_releases_claim_and_retries(self, monkeypatch):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        user = _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        user_id = user.id
        session.close()

        mock_send = AsyncMock(return_value=False)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        assert mock_send.await_count == 1
        session = SessionFactory()
        saved = session.query(Usuario).filter(Usuario.id == user_id).first()
        assert saved.proactivo_ultimo_envio is None
        session.close()

        mock_send.return_value = True
        await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        assert mock_send.await_count == 2
        session = SessionFactory()
        saved = session.query(Usuario).filter(Usuario.id == user_id).first()
        assert saved.proactivo_ultimo_envio == self.TODAY
        session.close()

    @pytest.mark.asyncio
    async def test_failure_logs_warning(self, monkeypatch, caplog):
        SessionFactory, engine = _make_session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionFactory)
        monkeypatch.setenv("PROACTIVE_PROMPT_HOUR", "20")

        session = SessionFactory()
        _seed_user(
            session,
            ultimo_mensaje_en=self.NOW_20_LOCAL - timedelta(hours=2),
        )
        session.close()

        mock_send = AsyncMock(return_value=False)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message", mock_send)

        from app.scheduler import check_proactive_prompts
        with caplog.at_level("WARNING", logger="app.scheduler"):
            await check_proactive_prompts(_now=self.NOW_20_LOCAL)

        assert "PROACTIVE_ERROR" in caplog.text
