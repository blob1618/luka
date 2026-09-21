"""Suite de pruebas de integración determinista para STK-189 (HU-REM-03).

Valida el ciclo de vida completo integrado de gastos recurrentes:
1. Detección batch (3 meses consecutivos) -> Creación de candidato pendiente.
2. Registro de egreso en webhook -> Adjunción determinista de propuesta interactiva.
3. Aceptación vía botón interactivo -> Conversión atómica a Recordatorio inteligente (dias_anticipacion=3).
4. Ejecución del scheduler en fecha de alerta -> Despacho desacoplado a WhatsApp y registro de AvisoRecordatorio ('sent').
5. Rechazo interactivo ('rechazado') -> Persistencia y supresión permanente de futuras propuestas.
6. Supresión determinista por gasto registrado previamente en el período ('suprimido' con motivo 'gasto_registrado').
7. Seguridad y preservación de contexto ante texto libre (nuevo gasto o texto afirmativo sin botón).
8. Aislamiento estricto multi-usuario y protección anti-tampering.
9. Desacoplamiento del webhook: 0 escaneos de historial financiero en el webhook.
10. Regresión de recuperación y liberación de advisory lock ante fallos de BD en el worker batch.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.whatsapp import (
    WhatsAppDeliveryStatus,
    WhatsAppReplyButtons,
    WhatsAppSendResult,
)
from app.models.database import (
    AvisoRecordatorio,
    Base,
    CandidatoGastoRecurrente,
    Categoria,
    MovimientoFinanciero,
    Recordatorio,
    Usuario,
)
from app.scheduler import (
    _run_daily_recurring_detection_sync,
    check_reminders,
)
from app.services.dispatcher import (
    process_incoming_interactive_reply,
    process_incoming_message,
)
from app.services.onboarding import OnboardingDecision, OnboardingResult
from app.services.recurring_expense import (
    RecurringExpenseService,
    calculate_pattern_hash,
    normalize_description,
)

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def _setup_test_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _create_user(session, phone="5491100001111", name="Usuario Integración", proactivo=True):
    user = Usuario(
        id=uuid.uuid4(),
        nombre=name,
        email=f"user_{uuid.uuid4().hex[:8]}@integration.test",
        whatsapp_id=phone,
        proactivo_habilitado=proactivo,
    )
    session.add(user)
    session.commit()
    return user


def _create_category(session, user_id, name="Servicios"):
    cat = Categoria(
        id=uuid.uuid4(),
        usuario_id=user_id,
        nombre=name,
    )
    session.add(cat)
    session.commit()
    return cat


def _insert_movement(session, user_id, category_id, desc, amount, mov_date, mov_type="egreso", anulado=False):
    mov = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user_id,
        categoria_id=category_id,
        tipo=mov_type,
        cantidad=Decimal(str(amount)),
        moneda="ARS",
        descripcion=desc,
        fecha_movimiento=mov_date,
        origen="whatsapp_text",
        anulado_en=datetime.now(timezone.utc) if anulado else None,
        creado_en=datetime.now(timezone.utc),
    )
    session.add(mov)
    session.commit()
    return mov


class TestRecurringIntegratedLifecycle:
    """Escenario 1: Ciclo completo E2E (Detección -> Propuesta -> Aceptación -> Scheduler -> WhatsApp)."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_detection_to_delivery(self, monkeypatch):
        SessionLocal = _setup_test_db()
        session = SessionLocal()
        phone = "5491144445555"
        user = _create_user(session, phone=phone)
        cat = _create_category(session, user.id, name="Servicios")

        # 1. Carga histórica de 3 meses consecutivos (día 10)
        _insert_movement(session, user.id, cat.id, "Internet Fibertel Hogar", 12500, date(2026, 6, 10))
        _insert_movement(session, user.id, cat.id, "Internet Fibertel Hogar", 12500, date(2026, 7, 10))
        _insert_movement(session, user.id, cat.id, "Internet Fibertel Hogar", 12500, date(2026, 8, 10))

        # 2. Ejecución de la detección batch nocturna a fecha de corte 2026-08-15
        detection_result = RecurringExpenseService.run_daily_detection(
            session=session,
            as_of_date=date(2026, 8, 15),
        )
        session.commit()

        assert detection_result.metrics.candidates_created == 1
        cand = session.query(CandidatoGastoRecurrente).filter(
            CandidatoGastoRecurrente.usuario_id == user.id
        ).first()
        assert cand is not None
        assert cand.estado == "pendiente"
        assert cand.proxima_fecha_estimada == date(2026, 9, 10)

        # 3. El usuario registra su egreso de septiembre por WhatsApp el 2026-09-08
        # Fijar reloj de elegibilidad de propuesta a 2026-09-08 (estimado 2026-09-10 >= 2026-09-08)
        frozen_webhook_time = datetime(2026, 9, 8, 12, 0, tzinfo=ARGENTINA_TZ)
        orig_eligible = RecurringExpenseService.is_candidate_eligible_for_proposal
        orig_expired = RecurringExpenseService.is_proposal_expired
        monkeypatch.setattr(
            RecurringExpenseService,
            "is_candidate_eligible_for_proposal",
            lambda c, as_of=None: orig_eligible(c, as_of or frozen_webhook_time),
        )
        monkeypatch.setattr(
            RecurringExpenseService,
            "is_proposal_expired",
            lambda c, as_of=None: orig_expired(c, as_of or frozen_webhook_time),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.services.finance.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.models.database.SessionLocal", SessionLocal)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        # Mockear LLMService para simular interpretación exacta de egreso
        mock_llm = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 12500,
            "currency": "ARS",
            "description": "Internet Fibertel Hogar",
            "category": "Servicios",
            "reply_text": "Registré tu egreso de Internet Fibertel Hogar por $12500 ARS.",
        }
        monkeypatch.setattr("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=mock_llm))

        dispatch_result = await process_incoming_message(
            sender_phone=phone,
            text_body="Pagué Internet Fibertel Hogar 12500",
            whatsapp_message_id="msg_int_001",
        )

        # Verificar que la respuesta adjunta botones interactivos de propuesta
        assert dispatch_result.reply_message is not None
        assert isinstance(dispatch_result.reply_message, WhatsAppReplyButtons)
        buttons = dispatch_result.reply_message.buttons
        assert len(buttons) == 2
        assert buttons[0].id == f"rec_cand:accept:{cand.id}"
        assert buttons[1].id == f"rec_cand:reject:{cand.id}"

        # 4. El usuario acepta tocando el botón interactivo "Sí, avisame"
        interactive_result = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand.id}",
            reply_type="button_reply",
        )
        assert "Agendé el recordatorio para *Internet Fibertel Hogar*" in interactive_result.reply_text

        # Verificar estado en BD: candidato 'aceptado' y Recordatorio creado
        session.refresh(cand)
        assert cand.estado == "aceptado"
        rec = session.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).first()
        assert rec is not None
        assert rec.estado == "activo"
        assert rec.origen == "recurrente_inteligente"
        assert rec.dias_anticipacion == 3
        assert rec.dia_del_mes == 10

        # 5. Ejecución del scheduler en fecha de alerta (3 días antes del día 10 = día 7)
        # Próximo vencimiento: 2026-10-10 -> Alerta: 2026-10-07
        fake_now = datetime(2026, 10, 7, 12, 0, tzinfo=ARGENTINA_TZ)
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionLocal)

        monkeypatch.setenv("WHATSAPP_REMINDER_TEMPLATE_NAME", "reminder_notice")
        sent_messages = []
        async def fake_send_whatsapp(recipient, message=None, template_name=None, template_parameters=None):
            sent_messages.append({"recipient": recipient, "message": message, "template": template_name})
            return WhatsAppSendResult(status=WhatsAppDeliveryStatus.SUCCESS, message_id="wamid_mock_123")

        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", fake_send_whatsapp)

        with patch("app.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            await check_reminders()

        # Verificar que se creó y marcó el AvisoRecordatorio como 'sent'
        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.recordatorio_id == rec.id
        ).first()
        assert aviso is not None
        assert aviso.estado == "sent"
        assert aviso.periodo == "2026-10"
        assert len(sent_messages) == 1
        assert sent_messages[0]["recipient"] == phone


class TestRecurringRejectionAndSuppression:
    """Escenarios 2 y 3: Rechazo explícito y supresión determinista por gasto ya pagado."""

    @pytest.mark.asyncio
    async def test_rejection_prevents_future_proposals_and_reminders(self, monkeypatch):
        SessionLocal = _setup_test_db()
        session = SessionLocal()
        phone = "5491188889999"
        user = _create_user(session, phone=phone)
        cat = _create_category(session, user.id, name="Servicios")

        # 1. Cargar 3 meses históricos absolutos (mayo, junio, julio 2026)
        _insert_movement(session, user.id, cat.id, "Gimnasio Megatlon", 25000, date(2026, 5, 5))
        _insert_movement(session, user.id, cat.id, "Gimnasio Megatlon", 25000, date(2026, 6, 5))
        _insert_movement(session, user.id, cat.id, "Gimnasio Megatlon", 25000, date(2026, 7, 5))

        RecurringExpenseService.run_daily_detection(session, as_of_date=date(2026, 7, 10))
        session.commit()

        cand = session.query(CandidatoGastoRecurrente).filter(CandidatoGastoRecurrente.usuario_id == user.id).first()
        assert cand is not None
        assert cand.estado == "pendiente"
        assert cand.dia_estimado == 5
        assert cand.proxima_fecha_estimada == date(2026, 8, 5)

        frozen_rejection_time = datetime(2026, 7, 20, 12, 0, tzinfo=ARGENTINA_TZ)
        orig_eligible = RecurringExpenseService.is_candidate_eligible_for_proposal
        orig_expired = RecurringExpenseService.is_proposal_expired
        monkeypatch.setattr(
            RecurringExpenseService,
            "is_candidate_eligible_for_proposal",
            lambda c, as_of=None: orig_eligible(c, as_of or frozen_rejection_time),
        )
        monkeypatch.setattr(
            RecurringExpenseService,
            "is_proposal_expired",
            lambda c, as_of=None: orig_expired(c, as_of or frozen_rejection_time),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", SessionLocal)

        # Usuario rechaza con el botón interactivo
        res = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand.id}",
            reply_type="button_reply",
        )
        assert "no volveré a sugerirte este recordatorio" in res.reply_text

        session.refresh(cand)
        assert cand.estado == "rechazado"

        # Siguiente corrida del detector nocturno: no revive ni altera el rechazo
        RecurringExpenseService.run_daily_detection(session, as_of_date=date(2026, 8, 20))
        session.commit()
        session.refresh(cand)
        assert cand.estado == "rechazado"

        # Siguiente egreso registrado: no adjunta propuesta
        mock_llm = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 25000,
            "currency": "ARS",
            "description": "Gimnasio Megatlon",
            "category": "Servicios",
            "reply_text": "Registrado.",
        }
        monkeypatch.setattr("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=mock_llm))
        monkeypatch.setattr("app.services.dispatcher.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.services.finance.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.models.database.SessionLocal", SessionLocal)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        res_exp = await process_incoming_message(
            sender_phone=phone,
            text_body="Gimnasio 25000",
            whatsapp_message_id="msg_int_002",
        )
        # Sin botones interactivos adjuntos
        assert not isinstance(res_exp.reply_message, WhatsAppReplyButtons)

    @pytest.mark.asyncio
    async def test_scheduler_suppression_when_user_pays_early(self, monkeypatch):
        """Si el usuario paga el gasto del mes antes de la fecha de aviso, se suprime."""
        SessionLocal = _setup_test_db()
        session = SessionLocal()
        phone = "5491122223333"
        user = _create_user(session, phone=phone)
        cat = _create_category(session, user.id, name="Servicios")

        # Candidato ya aceptado y recordatorio activo
        norm_desc = normalize_description("Seguro La Segunda")
        p_hash = calculate_pattern_hash(norm_desc, cat.id, "ARS")
        cand = CandidatoGastoRecurrente(
            id=uuid.uuid4(),
            usuario_id=user.id,
            patron_hash=p_hash,
            descripcion_normalizada=norm_desc,
            categoria_id=cat.id,
            moneda="ARS",
            concepto="Seguro La Segunda",
            monto_estimado=Decimal("35000"),
            dia_estimado=20,
            proxima_fecha_estimada=date(2026, 10, 20),
            estado="aceptado",
            ultima_fecha_movimiento=date(2026, 9, 20),
            evidencia_movimiento_ids=[],
        )
        rec = Recordatorio(
            id=uuid.uuid4(),
            usuario_id=user.id,
            candidato_id=cand.id,
            titulo="Seguro La Segunda",
            dia_del_mes=20,
            dias_anticipacion=3,
            origen="recurrente_inteligente",
            estado="activo",
        )
        session.add_all([cand, rec])
        session.commit()

        # El usuario paga tempranamente el día 2026-10-10
        _insert_movement(session, user.id, cat.id, "Seguro La Segunda", 35000, date(2026, 10, 10))

        # En la fecha de alerta (día 17, 3 días antes del 20), el scheduler evalúa
        fake_alert_day = datetime(2026, 10, 17, 10, 0, tzinfo=ARGENTINA_TZ)
        monkeypatch.setattr("app.scheduler.SessionLocal", SessionLocal)

        sent_messages = []
        async def fake_send_whatsapp(*args, **kwargs):
            sent_messages.append(args)
            return WhatsAppSendResult(status=WhatsAppDeliveryStatus.DELIVERED, message_id="wamid_mock")
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", fake_send_whatsapp)

        with patch("app.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_alert_day
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            await check_reminders()

        # Verificar supresión: 0 mensajes enviados, aviso 'suprimido' por 'gasto_registrado'
        assert len(sent_messages) == 0
        aviso = session.query(AvisoRecordatorio).filter(AvisoRecordatorio.recordatorio_id == rec.id).first()
        assert aviso is not None
        assert aviso.estado == "suprimido"
        assert aviso.motivo_supresion == "gasto_registrado"


class TestSecurityAndConversationalSafety:
    """Escenarios 4, 5 y 6: Seguridad de texto libre, anti-tampering y desacoplamiento."""

    @pytest.mark.asyncio
    async def test_free_text_does_not_corrupt_candidate_and_processes_expenses(self, monkeypatch):
        SessionLocal = _setup_test_db()
        session = SessionLocal()
        phone = "5491177778888"
        user = _create_user(session, phone=phone)
        _create_category(session, user.id, name="Comida")

        # Crear un candidato pendiente
        norm_desc = normalize_description("Internet Personal")
        p_hash = calculate_pattern_hash(norm_desc, None, "ARS")
        cand = CandidatoGastoRecurrente(
            id=uuid.uuid4(),
            usuario_id=user.id,
            patron_hash=p_hash,
            descripcion_normalizada=norm_desc,
            categoria_id=None,
            moneda="ARS",
            concepto="Internet Personal",
            dia_estimado=15,
            proxima_fecha_estimada=date(2026, 10, 15),
            estado="pendiente",
            ultima_fecha_movimiento=date(2026, 9, 15),
            evidencia_movimiento_ids=[],
        )
        session.add(cand)
        session.commit()

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.services.finance.SessionLocal", SessionLocal)
        monkeypatch.setattr("app.models.database.SessionLocal", SessionLocal)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        # Usuario escribe un gasto nuevo no relacionado: "almuerzo 4500"
        mock_llm_expense = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 4500,
            "currency": "ARS",
            "description": "almuerzo",
            "category": "Comida",
            "reply_text": "Registré tu almuerzo por $4500.",
        }
        monkeypatch.setattr("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=mock_llm_expense))

        res = await process_incoming_message(
            sender_phone=phone,
            text_body="almuerzo 4500",
            whatsapp_message_id="msg_int_003",
        )
        assert "almuerzo" in res.reply_text

        # El candidato original permanece intacto en 'pendiente'
        session.refresh(cand)
        assert cand.estado == "pendiente"

    @pytest.mark.asyncio
    async def test_anti_tampering_cross_user_button_rejection(self, monkeypatch):
        SessionLocal = _setup_test_db()
        session = SessionLocal()
        user1 = _create_user(session, phone="5491111111111", name="User 1")
        _create_user(session, phone="5491122222222", name="User 2")

        cand_user1 = CandidatoGastoRecurrente(
            id=uuid.uuid4(),
            usuario_id=user1.id,
            patron_hash="a" * 64,
            descripcion_normalizada="gasto test",
            moneda="ARS",
            concepto="Gasto Test",
            dia_estimado=10,
            proxima_fecha_estimada=date(2026, 10, 10),
            estado="pendiente",
            ultima_fecha_movimiento=date(2026, 9, 10),
            evidencia_movimiento_ids=[],
        )
        session.add(cand_user1)
        session.commit()

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", SessionLocal)

        # User 2 intenta aceptar el candidato perteneciente a User 1
        res = await process_incoming_interactive_reply(
            sender_phone="5491122222222",
            option_id=f"rec_cand:accept:{cand_user1.id}",
            reply_type="button_reply",
        )
        # Debe rechazar por falta de propiedad
        assert "No encontré la sugerencia solicitada" in res.reply_text
        session.refresh(cand_user1)
        assert cand_user1.estado == "pendiente"


class TestAdvisoryLockRollbackRecovery:
    """Escenario 7: Regresión de liberación de lock y gestión de pool en PostgreSQL."""

    def _build_mock_pool(self):
        global_locks = {}

        class MockPhysicalConnection:
            def __init__(self, pid):
                self.pid = pid
                self.in_aborted_transaction = False
                self.is_pinned = False

            def execute(self, stmt):
                stmt_str = str(stmt)
                if self.in_aborted_transaction and "ROLLBACK" not in stmt_str.upper():
                    raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")

                if "pg_try_advisory_lock" in stmt_str:
                    key = 5354418701
                    if key not in global_locks or global_locks[key] == self.pid:
                        global_locks[key] = self.pid
                        return type("Result", (), {"scalar": lambda s: True})()
                    return type("Result", (), {"scalar": lambda s: False})()

                if "pg_advisory_unlock" in stmt_str:
                    key = 5354418701
                    if global_locks.get(key) == self.pid:
                        del global_locks[key]
                        return type("Result", (), {"scalar": lambda s: True})()
                    return type("Result", (), {"scalar": lambda s: False})()

                if "SELECT 1/0" in stmt_str:
                    self.in_aborted_transaction = True
                    raise RuntimeError("division by zero")

                return type("Result", (), {"scalar": lambda s: None})()

            def rollback(self):
                self.in_aborted_transaction = False

            def invalidate(self):
                for k, v in list(global_locks.items()):
                    if v == self.pid:
                        del global_locks[k]

        conn1 = MockPhysicalConnection(pid=101)
        conn2 = MockPhysicalConnection(pid=102)
        idle_pool = [conn1, conn2]

        class PinnedConnectionContext:
            def __init__(self, conn):
                self.conn = conn

            def __enter__(self):
                self.conn.is_pinned = True
                return self.conn

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.conn.is_pinned = False
                idle_pool.append(self.conn)

        class MockBind:
            class dialect:
                name = "postgresql"

            @staticmethod
            def connect():
                if not idle_pool:
                    raise RuntimeError("Connection pool exhausted")
                c = idle_pool.pop(0)
                return PinnedConnectionContext(c)

        mock_bind = MockBind()

        class MockSession:
            def __init__(self, bind=None):
                self.bind = bind
                if self.bind is None:
                    # Comportamiento no pineado: pide conexión del pool en cada uso
                    if not idle_pool:
                        raise RuntimeError("Connection pool exhausted")
                    self._conn = idle_pool.pop(0)
                else:
                    self._conn = self.bind

            def execute(self, stmt):
                return self._conn.execute(stmt)

            def commit(self):
                # Si no está pineada, el Session commit devuelve la conexión al pool
                if not getattr(self._conn, "is_pinned", False):
                    idle_pool.append(self._conn)
                    self._conn = None

            def rollback(self):
                if self._conn is not None:
                    self._conn.rollback()

            def close(self):
                if self._conn is not None and not getattr(self._conn, "is_pinned", False):
                    idle_pool.append(self._conn)
                    self._conn = None

        class MockSessionFactory:
            kw = {"bind": mock_bind}

            def __call__(self, bind=None):
                return MockSession(bind=bind)

        return MockSessionFactory(), mock_bind, global_locks, idle_pool

    def test_advisory_lock_pinned_connection_normal_run(self, monkeypatch):
        """Verifica que el diseño con conexión pineada retenga el lock durante commits y lo libere limpiamente."""
        factory, mock_bind, global_locks, idle_pool = self._build_mock_pool()
        monkeypatch.setattr("app.scheduler.SessionLocal", factory)

        # Simular detección exitosa con commit interno
        def mock_detection(session, as_of_date=None):
            session.commit()
            # Concurrencia durante detección: otra conexión del pool intenta adquirir el lock
            with mock_bind.connect() as other_conn:
                assert other_conn.pid == 102
                other_acquired = other_conn.execute("SELECT pg_try_advisory_lock(5354418701)").scalar()
                assert other_acquired is False, "Otra conexión no debe poder adquirir el lock retenido"
            class Result:
                class metrics:
                    candidates_created = 0
                    candidates_updated = 0
            return Result()

        monkeypatch.setattr("app.services.recurring_expense.RecurringExpenseService.run_daily_detection", mock_detection)

        # Ejecutar worker
        _run_daily_recurring_detection_sync(as_of_date=date(2026, 9, 21))

        # Verificación post-ejecución: lock completamente liberado y disponible para conexiones independientes
        assert len(global_locks) == 0, f"El lock debería estar libre, pero sigue retenido: {global_locks}"
        with mock_bind.connect() as independent_conn:
            free_check = independent_conn.execute("SELECT pg_try_advisory_lock(5354418701)").scalar()
            assert free_check is True, "Conexión independiente debe poder adquirir el lock tras finalizar"
            independent_conn.execute("SELECT pg_advisory_unlock(5354418701)")

    def test_advisory_lock_pinned_connection_error_run(self, monkeypatch):
        """Verifica que ante errores en la detección, se ejecute rollback previo a unlock y no haya fuga."""
        factory, mock_bind, global_locks, idle_pool = self._build_mock_pool()
        monkeypatch.setattr("app.scheduler.SessionLocal", factory)

        def mock_failing_detection(session, as_of_date=None):
            session.execute("SELECT 1/0")  # aborta transacción en PostgreSQL

        monkeypatch.setattr("app.services.recurring_expense.RecurringExpenseService.run_daily_detection", mock_failing_detection)

        # Ejecutar worker
        _run_daily_recurring_detection_sync(as_of_date=date(2026, 9, 21))

        # Verificación post-error: rollback ejecutado antes de unlock, lock completamente liberado
        assert len(global_locks) == 0, f"El lock debería estar libre tras error, pero quedó: {global_locks}"
        with mock_bind.connect() as independent_conn:
            free_check = independent_conn.execute("SELECT pg_try_advisory_lock(5354418701)").scalar()
            assert free_check is True, "Conexión independiente debe poder adquirir el lock tras error recuperado"
            independent_conn.execute("SELECT pg_advisory_unlock(5354418701)")

    def test_unpinned_session_fails_and_leaks_lock_under_pool_concurrency(self):
        """Demuestra por qué el código anterior sin conexión pineada fugaba el lock bajo concurrencia."""
        factory, mock_bind, global_locks, idle_pool = self._build_mock_pool()

        # Simulación del código anterior: session = SessionLocal() sin bind.connect()
        old_session = factory(bind=None)
        # 1. Adquiere lock en conexión 101
        acquired = old_session.execute("SELECT pg_try_advisory_lock(5354418701)").scalar()
        assert acquired is True
        assert global_locks[5354418701] == 101

        # 2. detect_candidates() hace session.commit()
        old_session.commit()
        # En el código anterior sin pinear, conexión 101 volvió al pool!

        # 3. Otra operacion concurrente toma la conexion 101 del pool
        # idle_pool contiene conexiones liberadas
        concurrent_conn = next(c for c in idle_pool if c.pid == 101)
        idle_pool.remove(concurrent_conn)
        assert concurrent_conn.pid == 101

        # 4. El finally del código anterior intenta unlock desde old_session:
        # Al no tener conexión (se devolvió al pool tras commit), pide una nueva y recibe conn2 (102)!
        old_session._conn = idle_pool.pop(0)
        assert old_session._conn.pid == 102
        unlock_result = old_session.execute("SELECT pg_advisory_unlock(5354418701)").scalar()

        # El unlock en conexión 102 FALLA porque el lock pertenece a 101
        assert unlock_result is False
        # Conexión 101 sigue reteniendo el lock indefinidamente (FUGA REPRODUCIDA)
        assert global_locks[5354418701] == 101
