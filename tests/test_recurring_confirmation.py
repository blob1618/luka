"""Suite completa de pruebas para STK-187: Confirmación, preferencias y entrega idempotente por WhatsApp.

Cubre:
1. Composición y acotamiento (<= 1024 caracteres) de propuesta interactiva de candidatos.
2. Supresión de propuesta si usuario.proactivo_habilitado es False o por cooldown de 30 días.
3. Respuestas interactivas a botones (aceptar, rechazar, expiración a 7 días, anti-tampering).
4. Conversión atómica a recordatorio compatible con HU-REM-01 (dias_anticipacion=3, origen='recurrente_inteligente').
5. Sincronización transaccional de pausa, reanudación y eliminación con el candidato.
6. Invariante del detector batch: nunca revierte decisiones explícitas del usuario.
7. Cálculo de 3 días de anticipación y manejo de fin de mes / año.
8. Supresión determinista de aviso por gasto registrado en el período (AvisoRecordatorio.estado='suprimido').
9. Entrega desacoplada at-most-once (estados sending, sent, failed reintentable/permanente, unknown).
10. Reconciliación de reclamos 'sending' varados a 'unknown' (> 15 min).
11. Detección background con advisory lock PostgreSQL y fallback en SQLite.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.whatsapp import (
    WhatsAppDeliveryStatus,
    WhatsAppReplyButton,
    WhatsAppReplyButtons,
    WhatsAppSendResult,
)
from app.services.conversation import ConversationHistoryService
from app.services.webhook_idempotency import process_text_message_once
from tests.conftest import FakeRedis
from app.models.database import (
    AvisoRecordatorio,
    Base,
    CandidatoGastoRecurrente,
    Categoria,
    CronJobClaim,
    MovimientoFinanciero,
    Recordatorio,
    Usuario,
)
from app.scheduler import (
    _alert_day,
    _run_daily_recurring_detection_sync,
    check_reminders,
    reconcile_stranded_sending_claims,
    run_daily_recurring_detection,
    start_scheduler,
)
from app.services.dispatcher import (
    _check_pending_candidate_proposal,
    _record_proposal_sent,
    process_incoming_interactive_reply,
    process_incoming_message,
)
from app.services.onboarding import OnboardingDecision, OnboardingResult
from app.services.recurring_expense import (
    RecurringExpenseService,
    calculate_pattern_hash,
    normalize_description,
)
from app.services.reminder import ReminderService

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _create_user(session, phone="5491100001111", proactivo_habilitado=True):
    user = Usuario(
        id=uuid.uuid4(),
        nombre="Usuario Test",
        email=f"user_{uuid.uuid4().hex[:8]}@test.com",
        whatsapp_id=phone,
        proactivo_habilitado=proactivo_habilitado,
    )
    session.add(user)
    session.commit()
    return user


def _create_candidate(
    session,
    user_id,
    concepto="Internet Fibertel",
    dia_estimado=15,
    monto=Decimal("15000"),
    estado="pendiente",
    propuesta_en=None,
    propuesta_conteo=0,
    proxima_fecha=None,
    categoria_id=None,
):
    norm_desc = normalize_description(concepto)
    p_hash = calculate_pattern_hash(norm_desc, categoria_id, "ARS")
    proxima = proxima_fecha or (date.today() + timedelta(days=15))
    cand = CandidatoGastoRecurrente(
        id=uuid.uuid4(),
        usuario_id=user_id,
        patron_hash=p_hash,
        descripcion_normalizada=norm_desc,
        categoria_id=categoria_id,
        moneda="ARS",
        concepto=concepto,
        monto_estimado=monto,
        dia_estimado=dia_estimado,
        proxima_fecha_estimada=proxima,
        estado=estado,
        ultima_fecha_movimiento=date.today() - timedelta(days=15),
        evidencia_movimiento_ids=[],
        propuesta_en=propuesta_en,
        propuesta_conteo=propuesta_conteo,
    )
    session.add(cand)
    session.commit()
    return cand


# ===========================================================================
# 1. Composición y acotamiento de propuesta interactiva
# ===========================================================================

class TestCandidateProposalFlow:
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_proposal_attached_to_expense_registration(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15, categoria_id=cat.id)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.reminder.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000")

        assert "Noté que solés pagar *Internet Fibertel* alrededor del día 15." in result.reply_text
        assert "¿Querés que te avise 3 días antes de cada vencimiento?" in result.reply_text
        assert isinstance(result.reply_message, WhatsAppReplyButtons)
        assert len(result.reply_message.buttons) == 2
        assert result.reply_message.buttons[0].id == f"rec_cand:accept:{cand.id}"
        assert result.reply_message.buttons[1].id == f"rec_cand:reject:{cand.id}"
        assert result.proposal_candidate_id == str(cand.id)
        assert result.proposal_delivery_mode == "primary"

        # La construcción del mensaje NO activa el cooldown ni la telemetría antes del envío real
        session.refresh(cand)
        assert cand.propuesta_en is None
        assert cand.propuesta_conteo == 0

        # Al completarse el envío efectivo por WhatsApp, se persiste propuesta_en y propuesta_conteo
        redis = FakeRedis()
        send_mock = AsyncMock(return_value=True)
        status = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Internet Fibertel 15000",
            whatsapp_message_id="wamid.proposal_send_success",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
            send_message=send_mock,
        )
        assert status == "completed"
        assert send_mock.await_count == 1

        session.refresh(cand)
        assert cand.propuesta_en is not None
        assert cand.propuesta_conteo == 1

    @pytest.mark.asyncio
    async def test_proposal_suppressed_when_proactivo_disabled(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=False)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15, categoria_id=cat.id)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000")

        assert "Noté que solés pagar" not in result.reply_text
        assert result.reply_message is None
        assert result.proposal_candidate_id is None
        session.refresh(cand)
        assert cand.propuesta_en is None
        assert cand.propuesta_conteo == 0

    @pytest.mark.asyncio
    async def test_proposal_suppressed_by_30_day_cooldown(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(
            session, user.id, concepto="Internet Fibertel", dia_estimado=15,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=10),
            propuesta_conteo=1,
            categoria_id=cat.id,
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000")

        assert "Noté que solés pagar" not in result.reply_text
        assert result.proposal_candidate_id is None
        session.refresh(cand)
        assert cand.propuesta_conteo == 1

    @pytest.mark.asyncio
    async def test_proposal_allowed_after_30_days(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(
            session, user.id, concepto="Internet Fibertel", dia_estimado=15,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=31),
            propuesta_conteo=1,
            categoria_id=cat.id,
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000")

        assert "Noté que solés pagar" in result.reply_text
        assert result.proposal_candidate_id == str(cand.id)
        assert result.proposal_delivery_mode == "primary"

        # Cooldown count is incremented on actual send
        redis = FakeRedis()
        send_mock = AsyncMock(return_value=True)
        await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Internet Fibertel 15000",
            whatsapp_message_id="wamid.proposal_30d_send",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
            send_message=send_mock,
        )
        session.refresh(cand)
        assert cand.propuesta_conteo == 2

    @pytest.mark.asyncio
    async def test_proposal_followup_when_total_length_exceeds_1024(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        long_desc = "Internet Fibertel con servicio ultra extendido " * 20
        cand = _create_candidate(session, user.id, concepto=long_desc, dia_estimado=15, categoria_id=cat.id)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )
        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": long_desc,
                "expense": long_desc,
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Gasto largo")

        # Texto principal preservado sin propuesta embebida
        assert "Noté que solés pagar" not in result.reply_text
        # Pero entregada vía follow-up message para no violar el límite de 1024 caracteres
        assert len(result.followup_messages) == 1
        assert "Noté que solés pagar" in result.followup_messages[0].body
        assert result.proposal_delivery_mode == "followup"
        session.refresh(cand)
        assert cand.propuesta_conteo == 0

    @pytest.mark.asyncio
    async def test_configurable_response_movement_registered_with_followup_proposal(self, monkeypatch):
        """La propuesta recurrente se entrega como follow-up sin perder una respuesta interactiva configurada."""
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15, categoria_id=cat.id)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        # Simular que movement.registered tiene una respuesta interactiva configurada (ej: botones)
        configured_msg = WhatsAppReplyButtons(
            body="Movimiento guardado con éxito",
            buttons=(WhatsAppReplyButton(id="view_dashboard", title="Ver panel"),),
        )
        monkeypatch.setattr(
            "app.services.dispatcher.ConversationFlowRuntime.render_event",
            AsyncMock(return_value=configured_msg),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000")

        # 1. Respuesta principal preservada íntegramente
        assert result.reply_message == configured_msg
        assert result.reply_message.body == "Movimiento guardado con éxito"

        # 2. Propuesta entregada en follow-up
        assert len(result.followup_messages) == 1
        assert isinstance(result.followup_messages[0], WhatsAppReplyButtons)
        assert "Noté que solés pagar *Internet Fibertel*" in result.followup_messages[0].body
        assert result.proposal_candidate_id == str(cand.id)
        assert result.proposal_delivery_mode == "followup"

        # 3. Enviar a través de la tubería de idempotencia
        redis = FakeRedis()
        sent_messages = []

        async def mock_send(to, msg):
            sent_messages.append((to, msg))
            return True

        status = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Internet Fibertel 15000",
            whatsapp_message_id="wamid.configurable_followup",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
            send_message=mock_send,
        )

        assert status == "completed"
        # 2 mensajes enviados a WhatsApp: el principal configurado y el follow-up de propuesta
        assert len(sent_messages) == 2
        assert sent_messages[0][1] == configured_msg
        assert sent_messages[1][1] == result.followup_messages[0]

        session.refresh(cand)
        assert cand.propuesta_en is not None
        assert cand.propuesta_conteo == 1

    @pytest.mark.asyncio
    async def test_single_indexed_eligibility_query_and_no_http_when_no_candidate_eligible(self, monkeypatch):
        """Un egreso sin candidato elegible ejecuta a lo sumo una única consulta indexada de elegibilidad y exactamente 1 llamada HTTP a Meta."""
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        # No se crea ningún candidato para este patrón

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 5000,
                "currency": "ARS",
                "description": "Gasto Casual Unico",
                "expense": "Gasto Casual Unico",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        result = await process_incoming_message(sender_phone=phone, text_body="Pagué Gasto Casual Unico 5000")

        assert result.proposal_candidate_id is None
        assert len(result.followup_messages) == 0

        redis = FakeRedis()
        send_mock = AsyncMock(return_value=True)
        status = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Gasto Casual Unico 5000",
            whatsapp_message_id="wamid.no_proposal",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Gasto Casual Unico 5000"),
            send_message=send_mock,
        )
        assert status == "completed"
        # Exactamente 1 envío HTTP a Meta
        assert send_mock.await_count == 1

    def test_sql_statement_counts_for_eligibility_lookup_and_update(self, monkeypatch):
        """La verificación de elegibilidad ejecuta a lo sumo 1 SELECT y la persistencia de propuesta 1 UPDATE atómico."""
        session_factory = _make_db()
        session = session_factory()
        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        user = _create_user(session, proactivo_habilitado=True)
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15)

        engine = session.get_bind()
        queries = []

        def capture_sql(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        event.listen(engine, "before_cursor_execute", capture_sql)

        # 1. Consulta de elegibilidad para un patrón inexistente
        queries.clear()
        res_none = _check_pending_candidate_proposal(user_id=user.id, patron_hash="hash_inexistente")
        assert res_none is None
        # Exactamente 1 sentencia SELECT (sin query separada de Usuario)
        assert len(queries) == 1
        assert "SELECT" in queries[0].upper()
        assert "candidato_gasto_recurrente" in queries[0].lower()
        assert "usuario" in queries[0].lower()

        # 2. Consulta de elegibilidad para un patrón existente
        queries.clear()
        res_found = _check_pending_candidate_proposal(user_id=user.id, patron_hash=cand.patron_hash)
        assert res_found is not None
        assert res_found["id"] == str(cand.id)
        # Exactamente 1 sentencia SELECT
        assert len(queries) == 1
        assert "SELECT" in queries[0].upper()

        # 3. Registro de propuesta enviada: UPDATE atómico directo sin SELECT previo
        queries.clear()
        _record_proposal_sent(candidate_id=cand.id)
        # Exactamente 1 sentencia UPDATE (cero SELECTs)
        assert len(queries) == 1
        assert "UPDATE" in queries[0].upper()
        assert "SELECT" not in queries[0].upper()

        session.refresh(cand)
        assert cand.propuesta_conteo == 1
        assert cand.propuesta_en is not None

    def test_utc_argentina_boundary_proposal_eligibility_and_expiration(self):
        """Validación de timestamps en UTC y comparación de proxima_fecha_estimada en fecha calendario de Argentina."""
        session_factory = _make_db()
        session = session_factory()
        user = _create_user(session)

        # 01:30:00 UTC del 2026-03-15 corresponde a las 22:30:00 del 2026-03-14 en Argentina
        boundary_now_utc = datetime(2026, 3, 15, 1, 30, 0, tzinfo=timezone.utc)

        # Caso A: proxima_fecha_estimada es 2026-03-14.
        # En UTC ya es 15 de marzo (parecería vencido), pero en Argentina aún es 14 de marzo (es hoy).
        cand_today_ar = _create_candidate(
            session,
            user.id,
            concepto="Abono Gym",
            dia_estimado=14,
            proxima_fecha=date(2026, 3, 14),
        )
        assert RecurringExpenseService.is_candidate_eligible_for_proposal(cand_today_ar, as_of=boundary_now_utc) is True
        assert RecurringExpenseService.is_proposal_expired(cand_today_ar, as_of=boundary_now_utc) is False

        # Caso B: proxima_fecha_estimada es 2026-03-13.
        # Vencido tanto en Argentina como en UTC.
        cand_past = _create_candidate(
            session,
            user.id,
            concepto="Abono Club",
            dia_estimado=13,
            proxima_fecha=date(2026, 3, 13),
        )
        assert RecurringExpenseService.is_candidate_eligible_for_proposal(cand_past, as_of=boundary_now_utc) is False
        assert RecurringExpenseService.is_proposal_expired(cand_past, as_of=boundary_now_utc) is True

        # Caso C: Cooldown de 30 días evaluado en UTC
        prop_time_utc = boundary_now_utc - timedelta(days=29, hours=23)
        cand_cooldown = _create_candidate(
            session,
            user.id,
            concepto="Seguro",
            dia_estimado=25,
            proxima_fecha=date(2026, 3, 25),
            propuesta_en=prop_time_utc,
        )
        assert RecurringExpenseService.is_candidate_eligible_for_proposal(cand_cooldown, as_of=boundary_now_utc) is False
        # Cooldown cumplido (30 días y 1 hora en UTC)
        cand_cooldown.propuesta_en = boundary_now_utc - timedelta(days=30, hours=1)
        assert RecurringExpenseService.is_candidate_eligible_for_proposal(cand_cooldown, as_of=boundary_now_utc) is True

    @pytest.mark.asyncio
    async def test_proposal_send_failure_does_not_activate_cooldown_and_does_not_penalize_movement(self, monkeypatch):
        """Un fallo en el envío de la propuesta no activa el cooldown, no se agrega a memoria y no pierde el movimiento registrado."""
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone, proactivo_habilitado=True)
        cat = Categoria(id=uuid.uuid4(), usuario_id=user.id, nombre="Servicios")
        session.add(cat)
        session.commit()
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15, categoria_id=cat.id)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)
        monkeypatch.setattr("app.services.finance.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            lambda *args, **kwargs: OnboardingResult(OnboardingDecision.KNOWN_USER),
        )

        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 15000,
                "currency": "ARS",
                "description": "Internet Fibertel",
                "expense": "Internet Fibertel",
                "category": "Servicios",
                "reply_text": "Registrado",
            }),
        )

        # Caso A: Fallo en envío de mensaje principal -> se rechaza, cooldown no activado
        redis = FakeRedis()
        send_fail_mock = AsyncMock(return_value=False)
        with pytest.raises(RuntimeError):
            await process_text_message_once(
                redis_client=redis,
                sender_phone=phone,
                text_body="Pagué Internet Fibertel 15000",
                whatsapp_message_id="wamid.fail_primary",
                process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
                send_message=send_fail_mock,
            )
        session.refresh(cand)
        assert cand.propuesta_en is None
        assert cand.propuesta_conteo == 0

        # Caso B: Respuesta principal interactiva configurada tiene éxito, pero el follow-up devuelve False
        configured_msg = WhatsAppReplyButtons(
            body="Movimiento guardado con éxito",
            buttons=(WhatsAppReplyButton(id="view_dash", title="Ver"),),
        )
        monkeypatch.setattr(
            "app.services.dispatcher.ConversationFlowRuntime.render_event",
            AsyncMock(return_value=configured_msg),
        )

        call_idx = 0
        async def mock_send_partial(to, msg):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return True  # Mensaje principal tiene éxito
            return False  # Follow-up de propuesta falla

        status = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Internet Fibertel 15000",
            whatsapp_message_id="wamid.fail_followup",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
            send_message=mock_send_partial,
        )

        # El movimiento principal se completa
        assert status == "completed"
        # El cooldown NO se activa porque la propuesta no fue entregada
        session.refresh(cand)
        assert cand.propuesta_en is None
        assert cand.propuesta_conteo == 0

        # La memoria conversacional NO contiene el texto del follow-up fallido
        history = await ConversationHistoryService.get_recent(redis, phone)
        assert len(history) == 2
        assert "Movimiento guardado con éxito" in history[1].content
        assert "Noté que solés pagar" not in history[1].content

        # Caso C: Follow-up lanza una excepción
        call_idx = 0
        async def mock_send_exception(to, msg):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return True
            raise ConnectionError("Network timeout during follow-up")

        status_c = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body="Pagué Internet Fibertel 15000",
            whatsapp_message_id="wamid.exc_followup",
            process_message=lambda **kw: process_incoming_message(sender_phone=phone, text_body="Pagué Internet Fibertel 15000"),
            send_message=mock_send_exception,
        )
        assert status_c == "completed"
        session.refresh(cand)
        assert cand.propuesta_en is None
        assert cand.propuesta_conteo == 0
        history_c = await ConversationHistoryService.get_recent(redis, phone)
        assert len(history_c) == 4
        assert "Movimiento guardado con éxito" in history_c[3].content
        assert "Noté que solés pagar" not in history_c[3].content


# ===========================================================================
# 2. Respuestas interactivas (Aceptar, Rechazar, Expiración, Anti-tampering)
# ===========================================================================

class TestInteractiveConfirmationButtons:
    @pytest.mark.asyncio
    async def test_accept_candidate_creates_intelligent_reminder(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(
            session, user.id, concepto="Fibertel Hogar", dia_estimado=10, monto=Decimal("12000"),
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        result = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand.id}",
            reply_type="button_reply",
        )

        assert "Agendé el recordatorio para *Fibertel Hogar* los días 10 de cada mes con aviso 3 días antes." in result.reply_text

        rec = session.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).first()
        assert rec is not None
        assert rec.usuario_id == user.id
        assert rec.titulo == "Fibertel Hogar"
        assert rec.dia_del_mes == 10
        assert rec.monto == Decimal("12000")
        assert rec.dias_anticipacion == 3
        assert rec.origen == "recurrente_inteligente"
        assert rec.estado == "activo"

        session.refresh(cand)
        assert cand.estado == "aceptado"
        assert cand.decision_en is not None
        assert cand.decision_origen == "interactivo"

    @pytest.mark.asyncio
    async def test_accept_candidate_idempotent(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(
            session, user.id, concepto="Fibertel Hogar", dia_estimado=10,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        res1 = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand.id}",
            reply_type="button_reply",
        )
        assert "Agendé el recordatorio" in res1.reply_text

        res2 = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand.id}",
            reply_type="button_reply",
        )
        assert "Ya tenés este recordatorio activo en tu cuenta." in res2.reply_text

        assert session.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).count() == 1

    @pytest.mark.asyncio
    async def test_reject_candidate_persists_rejection(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(
            session, user.id, concepto="Gimnasio", dia_estimado=5,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        result = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand.id}",
            reply_type="button_reply",
        )

        assert "Entendido, no volveré a sugerirte este recordatorio." in result.reply_text
        session.refresh(cand)
        assert cand.estado == "rechazado"
        assert cand.decision_en is not None
        assert cand.decision_origen == "interactivo"
        assert session.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).first() is None

    @pytest.mark.asyncio
    async def test_interactive_button_expired_after_7_days(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(
            session, user.id, concepto="Seguro Auto", dia_estimado=20,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=8),
        )

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        result = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand.id}",
            reply_type="button_reply",
        )

        assert "Esta sugerencia ya venció" in result.reply_text
        assert session.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).first() is None

    @pytest.mark.asyncio
    async def test_anti_tampering_other_user_candidate_rejected(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        user1 = _create_user(session, phone="5491111111111")
        user2 = _create_user(session, phone="5491122222222")

        cand_user1 = _create_candidate(session, user1.id, concepto="Luz Edenor", dia_estimado=12)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        result = await process_incoming_interactive_reply(
            sender_phone=user2.whatsapp_id,
            option_id=f"rec_cand:accept:{cand_user1.id}",
            reply_type="button_reply",
        )

        assert "No encontré la sugerencia solicitada." in result.reply_text
        session.refresh(cand_user1)
        assert cand_user1.estado == "pendiente"

    def test_conflicting_acceptance_and_rejection_decisions(self):
        """Consistencia lógica ante decisiones conflictivas y doble ejecución secuencial/simulada en SQLite.

        Nota de evidencia: Valida que la exclusión mutua transaccional evita estados contradictorios
        y recordatorios duplicados. No reproduce una carrera concurrente distribuida de conexiones PostgreSQL.
        """
        session_factory = _make_db()
        session1 = session_factory()
        session2 = session_factory()

        user = _create_user(session1)
        cand = _create_candidate(
            session1,
            user.id,
            concepto="Streaming Spotify",
            dia_estimado=20,
            monto=Decimal("4000"),
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )

        # Caso 1: Aceptación ocurre primero o gana la carrera
        status_acc, rec = RecurringExpenseService.convert_candidate_to_reminder(
            session=session1,
            user_id=user.id,
            candidate_id=cand.id,
        )
        assert status_acc == "converted"
        assert rec is not None

        # Intento de rechazo concurrente/posterior sobre el mismo candidato
        status_rej, cand_rej = RecurringExpenseService.reject_candidate(
            session=session2,
            user_id=user.id,
            candidate_id=cand.id,
        )
        # Debe retornar 'conflict' y no sobreescribir el estado a rechazado
        assert status_rej == "conflict"
        assert cand_rej is not None
        assert cand_rej.estado == "aceptado"

        # No hay duplicados de recordatorio y el candidato permanece aceptado
        recs = session1.query(Recordatorio).filter(Recordatorio.candidato_id == cand.id).all()
        assert len(recs) == 1
        assert recs[0].id == rec.id

        session1.refresh(cand)
        assert cand.estado == "aceptado"

        # Caso 2: Si un candidato es rechazado primero, la aceptación posterior es rechazada
        cand2 = _create_candidate(
            session1,
            user.id,
            concepto="Gimnasio Pase",
            dia_estimado=5,
            monto=Decimal("15000"),
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )
        status_rej2, _ = RecurringExpenseService.reject_candidate(
            session=session1,
            user_id=user.id,
            candidate_id=cand2.id,
        )
        assert status_rej2 == "rejected"

        status_acc2, rec2 = RecurringExpenseService.convert_candidate_to_reminder(
            session=session2,
            user_id=user.id,
            candidate_id=cand2.id,
        )
        assert status_acc2 == "already_rejected"
        assert rec2 is None

        recs2 = session1.query(Recordatorio).filter(Recordatorio.candidato_id == cand2.id).all()
        assert len(recs2) == 0

        session1.refresh(cand2)
        assert cand2.estado == "rechazado"

    @pytest.mark.asyncio
    async def test_late_expired_rejection_and_state_conflicts(self, monkeypatch):
        """Rechazo tardío/expirado y conflictos de estado informan adecuadamente al usuario."""
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)

        monkeypatch.setattr("app.services.dispatcher.SessionLocal", session_factory)

        # 1. Rechazo tardío/expirado (> 7 días desde propuesta_en)
        cand_expired = _create_candidate(
            session,
            user.id,
            concepto="Club Social",
            dia_estimado=10,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=8),
        )
        res_exp = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand_expired.id}",
            reply_type="button_reply",
        )
        assert res_exp.reply_text == "Esta sugerencia ya había vencido."
        session.refresh(cand_expired)
        assert cand_expired.estado == "pendiente"

        # 2. Rechazo idempotente (ya rechazado previamente)
        cand_already_rej = _create_candidate(
            session,
            user.id,
            concepto="Clases de Inglés",
            dia_estimado=12,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=2),
        )
        res_rej1 = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand_already_rej.id}",
            reply_type="button_reply",
        )
        assert res_rej1.reply_text == "Entendido, no volveré a sugerirte este recordatorio."

        res_rej2 = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand_already_rej.id}",
            reply_type="button_reply",
        )
        assert res_rej2.reply_text == "Esta sugerencia ya había sido rechazada anteriormente."

        # 3. Conflicto de estado: intentar rechazar cuando ya fue aceptado
        cand_accepted = _create_candidate(
            session,
            user.id,
            concepto="Netflix 4K",
            dia_estimado=15,
            propuesta_en=datetime.now(timezone.utc) - timedelta(days=1),
        )
        res_acc = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:accept:{cand_accepted.id}",
            reply_type="button_reply",
        )
        assert "Agendé el recordatorio para *Netflix 4K*" in res_acc.reply_text

        res_conflict = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{cand_accepted.id}",
            reply_type="button_reply",
        )
        assert res_conflict.reply_text == "No se pudo rechazar la sugerencia porque ya tenés este recordatorio configurado."

        # 4. Anti-tampering / ID no encontrado
        fake_id = uuid.uuid4()
        res_nf = await process_incoming_interactive_reply(
            sender_phone=phone,
            option_id=f"rec_cand:reject:{fake_id}",
            reply_type="button_reply",
        )
        assert res_nf.reply_text == "No encontré la sugerencia solicitada."



# ===========================================================================
# 3. Sincronización transaccional de comandos HU-REM-01 con el candidato
# ===========================================================================

class TestReminderCandidateSynchronization:
    def test_pause_activate_and_delete_synchronization(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Alquiler", dia_estimado=1)

        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)
        assert status == "converted"
        assert cand.estado == "aceptado"

        monkeypatch.setattr("app.services.reminder.SessionLocal", session_factory)

        # 1. Pausar recordatorio -> candidato pasa a 'pausado'
        res_pause = ReminderService.pause_reminder(phone, str(rec.id))
        assert res_pause.status == "paused"
        session.refresh(cand)
        session.refresh(rec)
        assert rec.estado == "pausado"
        assert cand.estado == "pausado"

        # 2. Reactivar recordatorio -> candidato pasa a 'aceptado'
        res_act = ReminderService.activate_reminder(phone, str(rec.id))
        assert res_act.status == "activated"
        session.refresh(cand)
        session.refresh(rec)
        assert rec.estado == "activo"
        assert cand.estado == "aceptado"

        # 3. Eliminar recordatorio -> candidato pasa a 'desactivado'
        res_del = ReminderService.delete_reminder(phone, str(rec.id))
        assert res_del.status == "deleted"
        session.refresh(cand)
        session.refresh(rec)
        assert rec.estado == "eliminado"
        assert cand.estado == "desactivado"


# ===========================================================================
# 4. Invariante del detector batch
# ===========================================================================

class TestDetectorNeverRevertsUserDecisions:
    def test_detector_preserves_accepted_rejected_paused_desactivated(self):
        session_factory = _make_db()
        session = session_factory()
        user = _create_user(session)

        c_aceptado = _create_candidate(session, user.id, concepto="Gasto Aceptado", estado="aceptado")
        c_rechazado = _create_candidate(session, user.id, concepto="Gasto Rechazado", estado="rechazado")
        c_pausado = _create_candidate(session, user.id, concepto="Gasto Pausado", estado="pausado")
        c_desactivado = _create_candidate(session, user.id, concepto="Gasto Desactivado", estado="desactivado")

        RecurringExpenseService.detect_candidates(session, user_id=user.id)

        session.refresh(c_aceptado)
        session.refresh(c_rechazado)
        session.refresh(c_pausado)
        session.refresh(c_desactivado)

        assert c_aceptado.estado == "aceptado"
        assert c_rechazado.estado == "rechazado"
        assert c_pausado.estado == "pausado"
        assert c_desactivado.estado == "desactivado"


# ===========================================================================
# 5. Scheduler: Cálculo de anticipación, supresión y entrega desacoplada
# ===========================================================================

class TestSchedulerIntelligentReminders:
    def test_alert_day_calculation_across_months_and_years(self):
        ref = date(2026, 3, 12)
        assert _alert_day(15, ref, dias_anticipacion=3) == date(2026, 3, 12)

        ref_jan = date(2026, 1, 29)
        assert _alert_day(1, ref_jan, dias_anticipacion=3) == date(2026, 1, 29)

        ref_dec = date(2026, 12, 29)
        assert _alert_day(1, ref_dec, dias_anticipacion=3) == date(2026, 12, 29)

        ref_default = date(2026, 3, 14)
        assert _alert_day(15, ref_default, dias_anticipacion=1) == date(2026, 3, 14)

    @pytest.mark.asyncio
    async def test_suppression_when_period_expense_is_registered(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)

        mov = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=user.id,
            tipo="egreso",
            cantidad=Decimal("15000"),
            moneda="ARS",
            descripcion="Internet Fibertel",
            fecha_movimiento=date(2026, 3, 5),
        )
        session.add(mov)
        session.commit()

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.patron_hash == cand.patron_hash,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "suprimido"
        assert aviso.motivo_supresion == "gasto_registrado"

        session.refresh(rec)
        assert rec.ultimo_aviso_enviado is None

    @pytest.mark.asyncio
    async def test_decoupled_delivery_success(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Luz Edenor", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)

        async def mock_send_detailed(*args, **kwargs):
            return WhatsAppSendResult(
                status=WhatsAppDeliveryStatus.SUCCESS,
                message_id="wamid.12345",
            )

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_detailed)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "sent"
        assert aviso.whatsapp_message_id == "wamid.12345"
        assert aviso.enviado_en is not None

        session.refresh(rec)
        assert rec.ultimo_aviso_enviado == date(2026, 3, 12)

    @pytest.mark.asyncio
    async def test_decoupled_delivery_retryable_failure_and_backoff(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Gas Fenosa", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)

        async def mock_send_detailed(*args, **kwargs):
            return WhatsAppSendResult(
                status=WhatsAppDeliveryStatus.RETRYABLE,
                status_code=503,
                error_message="Service unavailable",
            )

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_detailed)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "failed"
        assert aviso.intentos == 1
        assert aviso.es_reintentable is True
        assert aviso.reintentar_en is not None

        session.refresh(rec)
        assert rec.ultimo_aviso_enviado is None

    @pytest.mark.asyncio
    async def test_reconcile_stranded_sending_claims(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        user = _create_user(session)
        rec = Recordatorio(
            id=uuid.uuid4(),
            usuario_id=user.id,
            titulo="Servicio",
            dia_del_mes=15,
        )
        session.add(rec)
        session.commit()

        now_utc = datetime.now(timezone.utc)
        stranded = AvisoRecordatorio(
            id=uuid.uuid4(),
            usuario_id=user.id,
            patron_hash="hash_stranded_1",
            periodo="2026-03",
            recordatorio_id=rec.id,
            estado="sending",
            intentos=1,
            ultimo_intento_en=now_utc - timedelta(minutes=20),
        )
        active_sending = AvisoRecordatorio(
            id=uuid.uuid4(),
            usuario_id=user.id,
            patron_hash="hash_active_2",
            periodo="2026-03",
            recordatorio_id=rec.id,
            estado="sending",
            intentos=1,
            ultimo_intento_en=now_utc - timedelta(minutes=2),
        )
        session.add_all([stranded, active_sending])
        session.commit()

        reconciled_count = reconcile_stranded_sending_claims(session, now_utc, cutoff_minutes=15)
        assert reconciled_count == 1

        session.refresh(stranded)
        session.refresh(active_sending)

        assert stranded.estado == "unknown"
        assert stranded.es_reintentable is False
        assert stranded.error_detalle == "stranded_sending_reconciled"

        assert active_sending.estado == "sending"

    @pytest.mark.asyncio
    async def test_two_workers_claiming_same_aviso_concurrently(self, monkeypatch):
        """Dos workers intentando reclamar el mismo aviso: sólo uno envía a Meta.

        Nota de evidencia: Valida la exclusión mutua a través del reclamo atómico condicional
        en SQLite in-memory compartido; no reproduce una carrera concurrente distribuida en PostgreSQL.
        """
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Seguro Auto", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)

        meta_send_calls = []

        async def mock_send_detailed(*args, **kwargs):
            meta_send_calls.append((args, kwargs))
            await asyncio.sleep(0)
            return WhatsAppSendResult(
                status=WhatsAppDeliveryStatus.SUCCESS,
                message_id=f"wamid.worker_{len(meta_send_calls)}",
            )

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_detailed)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        session.close()

        # Ejecución concurrente simulada de dos workers
        await asyncio.gather(
            check_reminders(_now=now_dt),
            check_reminders(_now=now_dt),
        )

        # Exactamente un worker envió el mensaje a Meta
        assert len(meta_send_calls) == 1

        verify_session = session_factory()
        aviso = verify_session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "sent"
        assert aviso.intentos == 1
        assert aviso.whatsapp_message_id == "wamid.worker_1"

    @pytest.mark.asyncio
    async def test_check_period_expense_registered_ignores_annulled_movements(self, monkeypatch):
        # Caso 3 Jira: gasto del período con anulado_en IS NOT NULL no debe suprimir el aviso
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Internet Fibertel", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)
        assert status == "converted"

        # Movimiento en el período pero anulado
        mov = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=user.id,
            tipo="egreso",
            cantidad=Decimal("15000"),
            moneda="ARS",
            descripcion="Internet Fibertel",
            fecha_movimiento=date(2026, 3, 5),
            anulado_en=datetime.now(timezone.utc),
        )
        session.add(mov)
        session.commit()

        # check_period_expense_registered debe retornar False
        is_registered = RecurringExpenseService.check_period_expense_registered(
            session=session,
            user_id=user.id,
            patron_hash=cand.patron_hash,
            reference_date=date(2026, 3, 15),
        )
        assert is_registered is False

        # En scheduler, el aviso no se suprime y se envía normalmente
        async def mock_send_detailed(*args, **kwargs):
            return WhatsAppSendResult(
                status=WhatsAppDeliveryStatus.SUCCESS,
                message_id="wamid.annulled_test",
            )

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_detailed)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "sent"
        assert aviso.motivo_supresion is None

    @pytest.mark.asyncio
    async def test_pending_candidate_never_triggers_scheduler_reminders(self, monkeypatch):
        # Caso 9 Jira: candidato en pendiente nunca emite avisos ni ejecuta envíos en check_reminders
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(
            session,
            user.id,
            concepto="Abono Gimnasio",
            dia_estimado=15,
            estado="pendiente",
        )

        meta_send_calls = []

        async def mock_send_detailed(*args, **kwargs):
            meta_send_calls.append((args, kwargs))
            return WhatsAppSendResult(status=WhatsAppDeliveryStatus.SUCCESS, message_id="wamid.pending_cand")

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_detailed)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        assert len(meta_send_calls) == 0
        avisos = session.query(AvisoRecordatorio).all()
        assert len(avisos) == 0
        session.refresh(cand)
        assert cand.estado == "pendiente"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("inactive_state", ["pausado", "rechazado"])
    async def test_scheduler_suppression_when_candidate_is_inactive(self, monkeypatch, inactive_state):
        # Caso 10 Jira: supresión con motivo candidato_no_activo si el candidato está pausado o rechazado
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Seguro Auto", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)
        assert status == "converted"

        cand.estado = inactive_state
        session.commit()

        send_mock = AsyncMock()
        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", send_mock)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        now_dt = datetime(2026, 3, 12, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        await check_reminders(_now=now_dt)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "suprimido"
        assert aviso.motivo_supresion == "candidato_no_activo"
        assert send_mock.await_count == 0
        session.refresh(rec)
        assert rec.ultimo_aviso_enviado is None

    @pytest.mark.asyncio
    async def test_retryable_failure_succeeds_on_next_run_after_backoff(self, monkeypatch):
        # Caso 11 Jira: reintento exitoso tras error transitorio con reloj controlado
        session_factory = _make_db()
        session = session_factory()
        phone = "5491100001111"
        user = _create_user(session, phone=phone)
        cand = _create_candidate(session, user.id, concepto="Gas Fenosa", dia_estimado=15)
        status, rec = RecurringExpenseService.convert_candidate_to_reminder(session, user.id, cand.id)

        # Control del reloj del scheduler en UTC
        current_virtual_time = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)

        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return current_virtual_time.astimezone(tz)
                return current_virtual_time

        monkeypatch.setattr("app.scheduler.datetime", MockDateTime)

        # Intento 1: falla reintentable (503)
        mock_send_fail = AsyncMock(return_value=WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.RETRYABLE,
            status_code=503,
            error_message="Service unavailable",
        ))

        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_fail)
        monkeypatch.setattr("app.scheduler._window_open", lambda *args: True)

        await check_reminders(_now=current_virtual_time)

        aviso = session.query(AvisoRecordatorio).filter(
            AvisoRecordatorio.usuario_id == user.id,
            AvisoRecordatorio.periodo == "2026-03",
        ).first()

        assert aviso is not None
        assert aviso.estado == "failed"
        assert aviso.intentos == 1
        assert aviso.es_reintentable is True
        assert aviso.reintentar_en is not None
        retry_time = aviso.reintentar_en
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=timezone.utc)

        # Intento intermedio: reloj antes de reintentar_en -> no reintenta
        current_virtual_time = retry_time - timedelta(seconds=10)
        send_early_mock = AsyncMock()
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", send_early_mock)
        await check_reminders(_now=current_virtual_time)
        assert send_early_mock.await_count == 0
        session.refresh(aviso)
        assert aviso.estado == "failed"
        assert aviso.intentos == 1

        # Intento posterior: reloj después de reintentar_en -> reintento exitoso
        current_virtual_time = retry_time + timedelta(seconds=1)
        mock_send_success = AsyncMock(return_value=WhatsAppSendResult(
            status=WhatsAppDeliveryStatus.SUCCESS,
            message_id="wamid.retry_success_test",
        ))
        monkeypatch.setattr("app.scheduler.send_whatsapp_message_detailed", mock_send_success)
        await check_reminders(_now=current_virtual_time)

        session.refresh(aviso)
        assert aviso.estado == "sent"
        assert aviso.intentos == 2
        assert aviso.whatsapp_message_id == "wamid.retry_success_test"
        session.refresh(rec)
        assert rec.ultimo_aviso_enviado == date(2026, 3, 12)


# ===========================================================================
# 6. Detección en background con coalescencia
# ===========================================================================

class TestSingleWorkerDailyDetection:
    @pytest.mark.asyncio
    async def test_sqlite_cron_job_claim_deduplication(self, monkeypatch):
        session_factory = _make_db()
        session = session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)

        await run_daily_recurring_detection(as_of_date=date(2026, 3, 15))

        claims = session.query(CronJobClaim).filter(
            CronJobClaim.job_name == "daily_recurring_detection",
            CronJobClaim.fecha_ejecucion == date(2026, 3, 15),
        ).all()
        assert len(claims) == 1

        await run_daily_recurring_detection(as_of_date=date(2026, 3, 15))

        claims2 = session.query(CronJobClaim).filter(
            CronJobClaim.job_name == "daily_recurring_detection",
            CronJobClaim.fecha_ejecucion == date(2026, 3, 15),
        ).all()
        assert len(claims2) == 1

    def test_cron_and_logical_date_in_argentina_timezone(self, monkeypatch):
        """Cron y fecha lógica se calculan en zona horaria de Argentina."""
        session_factory = _make_db()
        session = session_factory()
        monkeypatch.setattr("app.scheduler.SessionLocal", session_factory)

        # 1. Verificar configuración del scheduler para detección diaria en ARGENTINA_TZ
        class FakeScheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, func, trigger, **kwargs):
                self.jobs.append((func, trigger, kwargs))

            def start(self):
                pass

        fake_sched = FakeScheduler()
        monkeypatch.setattr("app.scheduler.scheduler", fake_sched)
        start_scheduler()

        detection_jobs = [j for j in fake_sched.jobs if j[0] == run_daily_recurring_detection]
        assert len(detection_jobs) == 1
        func, trigger, kwargs = detection_jobs[0]
        assert trigger == "cron"
        assert kwargs.get("hour") == 3
        assert kwargs.get("minute") == 0
        assert kwargs.get("timezone") == ARGENTINA_TZ

        # 2. Verificar que cuando as_of_date es None, _run_daily_recurring_detection_sync
        # usa datetime.now(ARGENTINA_TZ).date() y NO UTC.
        # Simulamos las 01:30 UTC del 2026-03-15 (en Argentina son las 22:30 del 2026-03-14)
        mock_now_utc = datetime(2026, 3, 15, 1, 30, 0, tzinfo=timezone.utc)

        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return mock_now_utc.astimezone(tz)
                return mock_now_utc

        monkeypatch.setattr("app.scheduler.datetime", MockDateTime)

        captured_dates = []
        orig_run_daily = RecurringExpenseService.run_daily_detection

        def fake_run_daily(sess, as_of_date=None, **kw):
            captured_dates.append(as_of_date)
            return orig_run_daily(sess, as_of_date=as_of_date, **kw)

        monkeypatch.setattr(RecurringExpenseService, "run_daily_detection", fake_run_daily)

        _run_daily_recurring_detection_sync(as_of_date=None)

        # En UTC sería 2026-03-15, pero en Argentina son las 22:30 del 2026-03-14!
        assert len(captured_dates) == 1
        assert captured_dates[0] == date(2026, 3, 14)

        # Verificar que el claim en CronJobClaim se registró con fecha 2026-03-14
        claim = session.query(CronJobClaim).filter(
            CronJobClaim.job_name == "daily_recurring_detection"
        ).first()
        assert claim is not None
        assert claim.fecha_ejecucion == date(2026, 3, 14)

