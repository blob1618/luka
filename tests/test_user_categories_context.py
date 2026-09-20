"""Tests de inyección de categorías del usuario en el contexto del LLM."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.dispatcher as dispatcher_module
from app.models.database import Base, Categoria, Usuario


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(dispatcher_module, "SessionLocal", testing_session_local)

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


def create_user(session, whatsapp_id="5491111111111"):
    user = Usuario(
        nombre="Test User",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.commit()
    return user


def create_category(session, user_id, nombre="Comida", esta_eliminado=False):
    category = Categoria(
        usuario_id=user_id,
        nombre=nombre,
        es_default=False,
        esta_eliminado=esta_eliminado,
    )
    session.add(category)
    session.commit()
    return category


def test_build_user_context_lists_active_categories(db_context, monkeypatch):
    user = create_user(db_context["session"])
    create_category(db_context["session"], user.id, nombre="Comida")
    create_category(db_context["session"], user.id, nombre="Servicios")

    ctx = dispatcher_module.build_user_context(user.whatsapp_id)

    assert "FECHA ACTUAL:" in ctx
    assert "CATEGORÍAS DISPONIBLES DEL USUARIO:" in ctx
    assert "Comida" in ctx and "Servicios" in ctx


def test_build_user_context_excludes_deleted_categories(db_context, monkeypatch):
    user = create_user(db_context["session"])
    create_category(db_context["session"], user.id, nombre="Comida")
    create_category(db_context["session"], user.id, nombre="Borrada", esta_eliminado=True)

    ctx = dispatcher_module.build_user_context(user.whatsapp_id)

    assert "Comida" in ctx
    assert "Borrada" not in ctx


def test_build_user_context_no_categories_only_date(db_context, monkeypatch):
    user = create_user(db_context["session"])

    ctx = dispatcher_module.build_user_context(user.whatsapp_id)

    assert ctx.startswith("FECHA ACTUAL:")
    assert "CATEGORÍAS DISPONIBLES" not in ctx


def test_build_user_context_unknown_user_only_date(db_context, monkeypatch):
    ctx = dispatcher_module.build_user_context("5490000000000")

    assert ctx.startswith("FECHA ACTUAL:")
    assert "CATEGORÍAS DISPONIBLES" not in ctx


def test_build_user_context_db_failure_returns_date_only(db_context, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dispatcher_module, "SessionLocal", boom)

    ctx = dispatcher_module.build_user_context("5491111111111")

    assert ctx.startswith("FECHA ACTUAL:")
    assert "CATEGORÍAS DISPONIBLES" not in ctx


@pytest.mark.asyncio
async def test_expense_flow_passes_context_to_llm(db_context, monkeypatch):
    from app.services.dispatcher import process_incoming_message
    from app.services.finance import MovementRegistrationResult
    from app.services.onboarding import OnboardingDecision, OnboardingResult

    sender = "5491111111111"
    user = create_user(db_context["session"], whatsapp_id=sender)
    create_category(db_context["session"], user.id, nombre="Comida")

    llm_mock = AsyncMock(
        return_value={
            "intent": "expense",
            "movement_type": "egreso",
            "amount": 300,
            "currency": "ARS",
            "description": "comida",
            "expense": "comida",
            "category": "comida",
        }
    )

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch("app.services.dispatcher.LLMService.process_message", llm_mock),
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=MovementRegistrationResult(
                status="registered", message="ok", movement_id="m1",
                user_id=str(user.id), duplicate=False,
            ),
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_rename",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_reminder_data",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_year_confirmation",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_category_confirmation",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_data",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_delete_category",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.is_awaiting_limit_month_selection",
            new_callable=AsyncMock, return_value=False,
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_last_movement",
            new_callable=AsyncMock,
        ),
        patch("app.services.dispatcher._update_ultimo_mensaje"),
    ):
        await process_incoming_message(sender, "gasté 300 en comida", "wamid.1")

    assert llm_mock.called
    kwargs = llm_mock.call_args.kwargs
    assert "CATEGORÍAS DISPONIBLES" in kwargs["context"]
    assert "Comida" in kwargs["context"]
