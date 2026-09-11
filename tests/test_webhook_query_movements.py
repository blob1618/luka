import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.database as database_module
import app.services.dashboard_link as dashboard_link_module
import app.services.dispatcher as dispatcher_module
import app.services.finance as finance_module
import app.services.onboarding as onboarding_module
from app.main import app
from app.models.database import (
    Base,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.webhook_idempotency import InboundMessageClaim

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_webhook_environment(monkeypatch):
    """Disable multi-turn conversational locks and mock inbound idempotency."""
    for method in (
        "is_awaiting_category_confirmation",
        "is_awaiting_rename",
        "is_awaiting_reminder_data",
        "is_awaiting_limit_year_confirmation",
        "is_awaiting_limit_category_confirmation",
        "is_awaiting_limit_data",
        "is_awaiting_limit_delete_category",
        "is_awaiting_limit_month_selection",
    ):
        monkeypatch.setattr(
            f"app.services.dispatcher.ConversationService.{method}",
            AsyncMock(return_value=False),
        )
    claim = InboundMessageClaim("wamid.query.test", "whatsapp:inbound:test", "token")
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.claim",
        AsyncMock(return_value=claim),
    )
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.complete",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.release",
        AsyncMock(return_value=True),
    )


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(onboarding_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(database_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(dispatcher_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(dashboard_link_module, "SessionLocal", testing_session_local)

    monkeypatch.setenv("ONBOARDING_REGISTRATION_URL", "https://example.com/registro")
    monkeypatch.setenv("ONBOARDING_INVITATION_TTL_MINUTES", "30")

    session = testing_session_local()
    try:
        yield {
            "session": session,
            "session_factory": testing_session_local,
        }
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_webhook_payload(
    body="mis últimos gastos",
    sender_phone="5491100000001",
    whatsapp_message_id="wamid.test.query.1",
):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456123456",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": sender_phone}
                            ],
                            "messages": [
                                {
                                    "from": sender_phone,
                                    "id": whatsapp_message_id,
                                    "timestamp": "1603059201",
                                    "text": {"body": body},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def create_user(session, whatsapp_id="5491100000001", auth_user_id=None):
    user = Usuario(
        id=uuid.uuid4(),
        nombre="Luka User",
        email=f"user_{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
        auth_user_id=auth_user_id,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def create_category(session, user_id, nombre="Supermercado"):
    category = Categoria(
        id=uuid.uuid4(),
        usuario_id=user_id,
        nombre=nombre,
        es_default=False,
        esta_eliminado=False,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


def create_movement(
    session,
    user_id,
    tipo="egreso",
    cantidad=Decimal("1200.00"),
    descripcion="Coto",
    categoria_id=None,
    fecha_movimiento=None,
):
    mov = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user_id,
        categoria_id=categoria_id,
        tipo=tipo,
        cantidad=cantidad,
        moneda="ARS",
        descripcion=descripcion,
        fecha_movimiento=fecha_movimiento or date.today(),
        origen="whatsapp_text",
        whatsapp_message_id=f"wamid.{uuid.uuid4()}",
    )
    session.add(mov)
    session.commit()
    session.refresh(mov)
    return mov


class TestWebhookQueryMovementsIntegration:
    """Pruebas end-to-end de STK-151: consulta de movimientos por el webhook de WhatsApp."""

    def test_ca1_single_visible_response_and_ca2_no_data_modification(self, db_context):
        """CA 1: Una interacción genera como máximo una respuesta visible.
        CA 2: No se modifica información financiera.
        """
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001")
        cat = create_category(session, user.id, "Supermercado")
        create_movement(session, user.id, tipo="egreso", cantidad=Decimal("1500"), descripcion="Coto", categoria_id=cat.id)

        count_mov_before = session.query(MovimientoFinanciero).count()
        count_cat_before = session.query(Categoria).count()
        count_usr_before = session.query(Usuario).count()
        count_lim_before = session.query(LimiteCategoria).count()

        payload = make_webhook_payload("mis ultimos gastos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
            "reply_text": "Consultando tus movimientos.",
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text") as mock_register,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

            # CA 1: Se llama a send_whatsapp_message exactamente una sola vez
            mock_send.assert_called_once()
            call_phone, call_reply = mock_send.call_args[0]
            assert call_phone == "5491100000001"
            assert "📋 *Tus últimos gastos:*" in call_reply
            assert "-$1500 ARS (Supermercado)" in call_reply

            # CA 2: No se modifica información financiera ni se llama al servicio de registro
            mock_register.assert_not_called()
            assert session.query(MovimientoFinanciero).count() == count_mov_before
            assert session.query(Categoria).count() == count_cat_before
            assert session.query(Usuario).count() == count_usr_before
            assert session.query(LimiteCategoria).count() == count_lim_before

    def test_ca3_errors_do_not_leak_internal_details(self, db_context):
        """CA 3: Los errores no filtran detalles internos (SQL, tablas, excepciones internas)."""
        session = db_context["session"]
        create_user(session, whatsapp_id="5491100000001")

        payload = make_webhook_payload("mis gastos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch(
                "app.services.dispatcher.FinanceService.query_movements",
                side_effect=RuntimeError("SELECT * FROM internal_sensitive_table FAILED: secret_key_error"),
            ),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            _, reply_text = mock_send.call_args[0]

            # Mensaje amigable y sanitizado
            assert reply_text == "Hubo un problema al consultar tus movimientos. Por favor, intentá nuevamente."

            # Asegurar que nada técnico se filtre
            assert "SELECT" not in reply_text
            assert "internal_sensitive_table" not in reply_text
            assert "secret_key_error" not in reply_text
            assert "RuntimeError" not in reply_text
            assert "Traceback" not in reply_text

    def test_unknown_user_derived_to_stk139_onboarding(self, db_context):
        """Deriva usuarios desconocidos a STK-139 / STK-144 sin consultar finanzas ni LLM."""
        unknown_phone = "5491199998888"
        payload = make_webhook_payload("mis ultimos gastos", sender_phone=unknown_phone)

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock()) as mock_llm,
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_finance,
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            phone, reply = mock_send.call_args[0]
            assert phone == unknown_phone
            assert "Para usar Luka, primero registrate" in reply
            assert "https://example.com/registro" in reply

            # No se invocó LLM ni finanzas
            mock_llm.assert_not_called()
            mock_finance.assert_not_called()

    def test_distinguish_query_register_and_out_of_scope(self, db_context):
        """Verifica la distinción clara entre consulta, registro y fuera de alcance en el webhook."""
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001")
        create_category(session, user.id, "Comida")

        # 1. Consulta: no debe registrar nada
        payload_query = make_webhook_payload("mostrame mis gastos", sender_phone="5491100000001")
        llm_query = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
        }
        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_query)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text") as mock_reg,
        ):
            client.post("/webhook", json=payload_query)
            mock_reg.assert_not_called()
            _, reply = mock_send.call_args[0]
            assert "No tenés movimientos registrados" in reply or "gastos" in reply

        # 2. Registro: debe registrar el gasto
        payload_expense = make_webhook_payload("gasté 2500 en Comida", sender_phone="5491100000001")
        llm_expense = {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 2500.0,
            "currency": "ARS",
            "description": "Comida",
            "category": "Comida",
            "reply_text": "Gasto registrado con éxito.",
        }
        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_expense)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            client.post("/webhook", json=payload_expense)
            _, reply = mock_send.call_args[0]
            assert "Comida" in reply
            # Comprobar que en la base de datos se registró el movimiento
            assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.usuario_id == user.id).count() == 1

        # 3. Fuera de alcance: no debe consultar ni modificar finanzas
        payload_greeting = make_webhook_payload("hola, cómo estás?", sender_phone="5491100000001")
        llm_greeting = {
            "intent": "greeting",
            "reply_text": "¡Hola! ¿En qué te puedo ayudar hoy?",
        }
        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_greeting)),
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_query,
            patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text") as mock_reg,
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            client.post("/webhook", json=payload_greeting)
            mock_query.assert_not_called()
            mock_reg.assert_not_called()
            _, reply = mock_send.call_args[0]
            assert "¡Hola!" in reply

    def test_single_clarification_on_ambiguous_query_without_redis_lock(self, db_context):
        """Solicita una sola aclaración cuando faltan datos esenciales o la consulta es ambigua,
        sin bloquear al usuario en estado conversacional persistente en Redis.
        """
        session = db_context["session"]
        create_user(session, whatsapp_id="5491100000001")

        payload = make_webhook_payload("qué onda con la plata?", sender_phone="5491100000001")
        clarification_text = "¿Querés consultar tus últimos gastos o tus ingresos?"
        llm_ambiguous = {
            "intent": "query_movements",
            "movement_type": None,
            "category": None,
            "date_from": None,
            "date_to": None,
            "reply_text": clarification_text,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_ambiguous)),
            patch("app.services.dispatcher.ConversationService.set_state", AsyncMock()) as mock_set_state,
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_query,
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            client.post("/webhook", json=payload)
            mock_query.assert_not_called()
            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]
            assert reply == clarification_text
            # No se almacena estado multi-turno bloqueante en Redis
            mock_set_state.assert_not_called()

    def test_natural_language_to_structured_filters(self, db_context):
        """Convierte lenguaje natural en filtros estructurados y llama a query_movements con ellos."""
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001")

        payload = make_webhook_payload("gastos en Farmacia en septiembre", sender_phone="5491100000001")
        llm_structured = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": "Farmacia",
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 5,
            "reply_text": "Consultando tus gastos en Farmacia de septiembre.",
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_structured)),
            patch("app.services.dispatcher.FinanceService.query_movements") as mock_query,
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)),
        ):
            from app.services.finance import MovementQueryResult
            mock_query.return_value = MovementQueryResult(status="ok", message="ok", movements=[], total_found=0)

            client.post("/webhook", json=payload)

            mock_query.assert_called_once_with(
                user.id,
                movement_type="egreso",
                category_name="Farmacia",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                limit=5,
            )

    def test_webhook_query_movements_offers_dashboard_link_when_more_found(self, db_context):
        """STK-152: Ofrece enlace canónico al dashboard cuando total_found > cantidad_mostrada (5)."""
        session = db_context["session"]
        # Usuario vinculado con auth_user_id
        linked_auth_id = uuid.uuid4()
        user = create_user(session, whatsapp_id="5491100000001", auth_user_id=linked_auth_id)

        # Crear 8 movimientos
        from datetime import timedelta
        base_date = date(2026, 9, 7)
        for i in range(8):
            create_movement(
                session,
                user.id,
                tipo="egreso",
                cantidad=Decimal(f"100{i}"),
                descripcion=f"Mov {i}",
                fecha_movimiento=base_date - timedelta(days=i),
            )

        payload = make_webhook_payload("mis ultimos gastos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": "2026-08-30",
            "date_to": "2026-09-07",
            "limit": 5,
            "reply_text": "Consultando tus movimientos.",
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]

            # Verificación del texto de movimientos y contador
            assert "Mostrando los últimos 5 de 8 movimientos." in reply

            # Verificación del enlace web ofrecido
            assert "🔗 *Ver este período en tu dashboard:*" in reply
            assert "https://example.com/login?token=" in reply
            assert "date_from=2026-08-30" in reply
            assert "date_to=2026-09-07" in reply
            assert "_(El enlace vence en 10 minutos y sólo se puede usar una vez)_" in reply

            # Validación de seguridad: no expone identificadores sensibles
            assert str(user.id) not in reply
            assert str(linked_auth_id) not in reply
            assert "5491100000001" not in reply

    def test_webhook_query_movements_no_link_when_total_found_lte_displayed(self, db_context):
        """STK-152: No ofrece enlace al dashboard cuando total_found <= cantidad_mostrada (<= 5)."""
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001", auth_user_id=uuid.uuid4())

        # Solo 3 movimientos
        for i in range(3):
            create_movement(session, user.id, descripcion=f"Gasto {i}")

        payload = make_webhook_payload("gastos de esta semana", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": "2026-08-30",
            "date_to": "2026-09-07",
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.DashboardLinkService.generate_or_reuse") as mock_link,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            # DashboardLinkService NO debe ser invocado
            mock_link.assert_not_called()

            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]
            assert "Ver este período en tu dashboard" not in reply

    def test_webhook_query_movements_linked_user_no_dates_omits_link_when_more_found(self, db_context):
        """STK-152: Usuario vinculado con más de 5 resultados pero sin fechas: no debe generarse enlace."""
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001", auth_user_id=uuid.uuid4())

        for i in range(8):
            create_movement(session, user.id, descripcion=f"Gasto {i}")

        payload = make_webhook_payload("mis ultimos gastos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.DashboardLinkService.generate_or_reuse") as mock_link,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_link.assert_not_called()

            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]
            assert "Mostrando los últimos 5 de 8 movimientos." in reply
            assert "Ver este período en tu dashboard" not in reply

    def test_webhook_query_movements_unlinked_user_omits_link_when_more_found(self, db_context):
        """STK-152: Usuario no vinculado (NOT_ELIGIBLE) recibe movimientos pero se omite el enlace silenciosamente."""
        session = db_context["session"]
        # Usuario sin auth_user_id
        user = create_user(session, whatsapp_id="5491100000001", auth_user_id=None)

        for i in range(8):
            create_movement(
                session,
                user.id,
                descripcion=f"Gasto {i}",
                fecha_movimiento=date(2026, 9, 7),
            )

        payload = make_webhook_payload("gastos de esta semana", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": "2026-08-30",
            "date_to": "2026-09-07",
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]

            # Movimientos presentes normalmente
            assert "Mostrando los últimos 5 de 8 movimientos." in reply
            # Enlace omitido
            assert "Ver este período en tu dashboard" not in reply

    def test_webhook_query_movements_registered_user_zero_movements(self, db_context):
        """STK-153: Webhook integral para un usuario registrado sin movimientos."""
        session = db_context["session"]
        create_user(session, whatsapp_id="5491100000001", auth_user_id=uuid.uuid4())

        payload = make_webhook_payload("mis ultimos gastos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": "egreso",
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.DashboardLinkService.generate_or_reuse") as mock_link,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            call_phone, reply = mock_send.call_args[0]
            assert call_phone == "5491100000001"
            assert "No encontré gastos registrados." in reply
            assert "Ver este período en tu dashboard" not in reply
            assert "token=" not in reply

            mock_link.assert_not_called()

    def test_webhook_query_movements_strict_financial_immutability(self, db_context):
        """STK-153: Verificación fuerte de inmutabilidad financiera en webhook integral con sesión fresca y escalares."""
        session = db_context["session"]
        session_factory = db_context["session_factory"]
        user = create_user(session, whatsapp_id="5491100000001", auth_user_id=uuid.uuid4())
        cat = create_category(session, user.id, "Supermercado")
        lim = LimiteCategoria(
            id=uuid.uuid4(),
            usuario_id=user.id,
            categoria_id=cat.id,
            cantidad_max=Decimal("45000.00"),
            moneda="ARS",
            inicio_periodo=date(2026, 9, 1),
            fin_periodo=date(2026, 9, 30),
        )
        session.add(lim)
        create_movement(session, user.id, tipo="egreso", cantidad=Decimal("1500.50"), descripcion="Coto", categoria_id=cat.id)
        create_movement(session, user.id, tipo="ingreso", cantidad=Decimal("5000.00"), descripcion="Transferencia")
        session.commit()

        def get_financial_snapshot():
            with session_factory() as fresh_session:
                movements = fresh_session.query(
                    MovimientoFinanciero.id,
                    MovimientoFinanciero.usuario_id,
                    MovimientoFinanciero.categoria_id,
                    MovimientoFinanciero.tipo,
                    MovimientoFinanciero.cantidad,
                    MovimientoFinanciero.moneda,
                    MovimientoFinanciero.descripcion,
                    MovimientoFinanciero.fecha_movimiento,
                    MovimientoFinanciero.origen,
                    MovimientoFinanciero.whatsapp_message_id,
                    MovimientoFinanciero.creado_en,
                    MovimientoFinanciero.actualizado_en,
                ).order_by(MovimientoFinanciero.id).all()

                categories = fresh_session.query(
                    Categoria.id,
                    Categoria.usuario_id,
                    Categoria.nombre,
                    Categoria.es_default,
                    Categoria.esta_eliminado,
                    Categoria.creado_en,
                ).order_by(Categoria.id).all()

                limits = fresh_session.query(
                    LimiteCategoria.id,
                    LimiteCategoria.usuario_id,
                    LimiteCategoria.categoria_id,
                    LimiteCategoria.cantidad_max,
                    LimiteCategoria.moneda,
                    LimiteCategoria.inicio_periodo,
                    LimiteCategoria.fin_periodo,
                    LimiteCategoria.creado_en,
                    LimiteCategoria.actualizado_en,
                ).order_by(LimiteCategoria.id).all()

                return movements, categories, limits

        snapshot_before = get_financial_snapshot()

        payload = make_webhook_payload("mis ultimos movimientos", sender_phone="5491100000001")
        llm_response = {
            "intent": "query_movements",
            "movement_type": None,
            "category": None,
            "date_from": None,
            "date_to": None,
            "limit": 5,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text") as mock_reg,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            mock_send.assert_called_once()
            mock_reg.assert_not_called()

            snapshot_after = get_financial_snapshot()
            # Los movimientos, montos, categorías y límites deben permanecer 100% inmutables
            assert snapshot_after == snapshot_before

    def test_period_summary_phrase_normalizes_to_query_movements_not_legacy_summary_or_register(self, db_context):
        """STK-153: Frase inequívoca como 'resumen de gastos de septiembre' se normaliza a query_movements,
        conserva filtros temporales, no cae en el flujo legacy expense_summary y no registra movimientos.
        """
        session = db_context["session"]
        user = create_user(session, whatsapp_id="5491100000001")
        cat = create_category(session, user.id, "Comida")

        # Movimiento de septiembre (debe aparecer)
        create_movement(
            session,
            user.id,
            tipo="egreso",
            cantidad=Decimal("3500.00"),
            descripcion="Supermercado Septiembre",
            categoria_id=cat.id,
            fecha_movimiento=date(2026, 9, 15),
        )
        # Movimiento de agosto (fuera de período, no debe aparecer)
        create_movement(
            session,
            user.id,
            tipo="egreso",
            cantidad=Decimal("1200.00"),
            descripcion="Supermercado Agosto",
            categoria_id=cat.id,
            fecha_movimiento=date(2026, 8, 20),
        )

        payload = make_webhook_payload("resumen de gastos de septiembre", sender_phone="5491100000001")
        # El LLM clasifica inicialmente como expense_summary con fechas de septiembre
        llm_response = {
            "intent": "expense_summary",
            "movement_type": "egreso",
            "category": None,
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 5,
            "reply_text": "Resumen de tus gastos de septiembre.",
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", AsyncMock(return_value=llm_response)),
            patch("app.main.send_whatsapp_message", AsyncMock(return_value=True)) as mock_send,
            patch("app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text") as mock_reg,
        ):
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200

            # No debe llamar al registro de movimientos
            mock_reg.assert_not_called()

            # Debe enviar respuesta visible con la consulta de movimientos del período
            mock_send.assert_called_once()
            _, reply = mock_send.call_args[0]
            assert "📋 *Tus últimos gastos:*" in reply
            assert "Supermercado Septiembre" in reply
            assert "-$3500.00 ARS" in reply or "-$3500 ARS" in reply
            assert "Supermercado Agosto" not in reply
