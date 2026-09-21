"""Tests for WebhookModeService."""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as redis

from testing.services.webhook_mode import WebhookModeResult, WebhookModeService
from app.services.conversation import ConversationHistoryService, ConversationMessage
from app.services.dispatcher import DispatchResult
from app.api.whatsapp import WhatsAppImage

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def dispatch_result(**overrides):
    defaults = {
        "reply_text": "✅ Registré tu egreso: supermercado por $5000 ARS.",
        "raw_llm_response": {"intent": "expense", "amount": 5000},
        "service_invoked": "finance",
        "intent": "expense",
        "debug_info": {},
    }
    defaults.update(overrides)
    return DispatchResult(**defaults)


@pytest.fixture()
def redis_mocks():
    client = AsyncMock()
    client.ping.return_value = True
    client.ttl.return_value = 1800
    with (
        patch("testing.services.webhook_mode.redis.from_url", return_value=client),
        patch(
            "testing.services.webhook_mode.ConversationHistoryService.get_recent",
            new_callable=AsyncMock,
        ) as get_recent,
        patch(
            "testing.services.webhook_mode.ConversationHistoryService.append_exchange",
            new_callable=AsyncMock,
        ) as append_exchange,
        patch(
            "testing.services.webhook_mode.ConversationHistoryService.clear",
            new_callable=AsyncMock,
        ) as clear,
    ):
        get_recent.return_value = []
        yield SimpleNamespace(
            client=client,
            get_recent=get_recent,
            append_exchange=append_exchange,
            clear=clear,
        )
        client.aclose.assert_awaited_once()


class TestWebhookModeResult:
    def test_result_has_required_fields(self):
        result = WebhookModeResult(
            reply_text="ok",
            raw_llm_response=None,
            service_invoked=None,
            intent=None,
            latency_ms=10.0,
            provider="gemini",
            prompt_path="prompt.md",
            redis_state=None,
        )
        assert result.reply_text == "ok"
        assert result.memory is None
        assert result.memory_ttl_seconds is None
        assert result.image_png is None


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_captures_chart_png_from_dispatch_result(self, redis_mocks):
        service = WebhookModeService()
        chart_png = b"\x89PNG\r\n\x1a\nchart"

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(
                    reply_text="Gastos por categoría",
                    reply_message=WhatsAppImage(
                        content=chart_png,
                        caption="Gastos por categoría",
                    ),
                ),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await service.send_message(
                "Mostrame un gráfico de gastos",
                "12345",
                "gemini",
                "prompt.md",
            )

        assert result.image_png == chart_png

    @pytest.mark.asyncio
    async def test_routes_through_dispatcher(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ) as mock_dispatch,
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await service.send_message(
                text="Gasté 5000 en super",
                phone="12345",
                provider="gemini",
                prompt_path="prompt.md",
            )

        mock_dispatch.assert_awaited_once_with(
            sender_phone="12345",
            text_body="Gasté 5000 en super",
            whatsapp_message_id=None,
            conversation_history=[],
        )
        assert "5000" in result.reply_text
        assert result.service_invoked == "finance"
        assert result.intent == "expense"

    @pytest.mark.asyncio
    async def test_measures_latency(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await service.send_message("test", "12345", "gemini", "prompt.md")

        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_captures_redis_state(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
            patch(
                "testing.services.webhook_mode.ConversationService.get_state",
                new_callable=AsyncMock,
            ) as mock_state,
        ):
            from app.services.conversation import ConversationState
            mock_state.return_value = ConversationState.empty()

            result = await service.send_message("test", "12345", "gemini", "prompt.md")

        assert result.redis_state is not None
        assert result.redis_state["step"] == "none"

    @pytest.mark.asyncio
    async def test_handles_dispatcher_error(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection failed"),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await service.send_message("test", "12345", "gemini", "prompt.md")

        assert "error" in result.reply_text.lower()
        assert result.latency_ms >= 0
        assert result.memory is None

    @pytest.mark.asyncio
    async def test_redis_state_read_failure_returns_error_dict(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
            patch(
                "testing.services.webhook_mode.ConversationService.get_state",
                new_callable=AsyncMock,
                side_effect=Exception("redis down"),
            ),
        ):
            result = await service.send_message("test", "12345", "gemini", "prompt.md")

        assert result.redis_state == {"error": "Could not read Redis state"}

    @pytest.mark.asyncio
    async def test_sets_model_via_env(self, redis_mocks):
        service = WebhookModeService()

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
            patch.dict("os.environ", {}, clear=False),
        ):
            result = await service.send_message(
                "test", "12345", "gemini", "prompt.md", model="gemini-3.5-flash"
            )
            assert os.environ["GEMINI_MODEL"] == "gemini-3.5-flash"
            assert result.model == "gemini-3.5-flash"


class TestConversationMemory:
    @pytest.mark.asyncio
    async def test_passes_recent_history_to_dispatcher(self, redis_mocks):
        previous = [
            ConversationMessage("user", "hola"),
            ConversationMessage("assistant", "buenas"),
        ]
        final = previous + [
            ConversationMessage("user", "Gasté 5000 en super"),
            ConversationMessage(
                "assistant",
                "✅ Registré tu egreso: supermercado por $5000 ARS.",
            ),
        ]
        redis_mocks.get_recent.side_effect = [previous, final]

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ) as mock_dispatch,
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        assert mock_dispatch.await_args.kwargs["conversation_history"] == [
            message.to_dict() for message in previous
        ]
        assert result.memory == [message.to_dict() for message in final]

    @pytest.mark.parametrize(
        ("ttl", "expected"),
        [(-2, None), (-1, None), (0, 0)],
    )
    @pytest.mark.asyncio
    async def test_memory_ttl_negative_is_none(self, redis_mocks, ttl, expected):
        redis_mocks.client.ttl.return_value = ttl

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        assert result.memory_ttl_seconds == expected

    @pytest.mark.asyncio
    async def test_appends_exchange_with_visible_reply(self, redis_mocks):
        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        redis_mocks.append_exchange.assert_awaited_once_with(
            redis_mocks.client,
            "12345",
            "Gasté 5000 en super",
            "✅ Registré tu egreso: supermercado por $5000 ARS.",
            message_id=None,
        )
        redis_mocks.clear.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_memory_clears_without_appending(self, redis_mocks):
        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(clear_memory=True),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        redis_mocks.clear.assert_awaited_once_with(redis_mocks.client, "12345")
        redis_mocks.append_exchange.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_connection_failure_keeps_reply(self):
        with (
            patch(
                "testing.services.webhook_mode.redis.from_url",
                side_effect=Exception("redis down"),
            ),
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ) as mock_dispatch,
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        assert "5000" in result.reply_text
        assert result.memory is None
        assert result.memory_ttl_seconds is None
        assert mock_dispatch.await_args.kwargs["conversation_history"] == []

    @pytest.mark.asyncio
    async def test_ping_failure_keeps_reply_and_closes_client(self):
        client = AsyncMock()
        client.ping.side_effect = ConnectionError("redis down")

        with (
            patch("testing.services.webhook_mode.redis.from_url", return_value=client),
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ) as mock_dispatch,
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        assert "5000" in result.reply_text
        assert result.memory is None
        assert result.memory_ttl_seconds is None
        assert mock_dispatch.await_args.kwargs["conversation_history"] == []
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_memory_service_errors_keep_reply(self, redis_mocks):
        redis_mocks.get_recent.side_effect = Exception("redis down")
        redis_mocks.append_exchange.side_effect = Exception("redis down")

        with (
            patch(
                "testing.services.webhook_mode.process_incoming_message",
                new_callable=AsyncMock,
                return_value=dispatch_result(),
            ),
            patch("testing.services.webhook_mode.LLMService.reset_provider"),
            patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
        ):
            result = await WebhookModeService().send_message(
                "Gasté 5000 en super", "12345", "gemini", "prompt.md"
            )

        assert "5000" in result.reply_text
        assert result.memory is None
        assert result.memory_ttl_seconds is None


class TestRealRedisIntegration:
    @pytest.mark.asyncio
    async def test_history_is_injected_and_memory_exposed(self):
        client = redis.from_url(REDIS_URL, decode_responses=True)
        try:
            await client.ping()
        except Exception:
            await client.aclose()
            pytest.skip("Redis no disponible")

        phone = f"p1-{uuid.uuid4()}"
        try:
            await ConversationHistoryService.append_exchange(
                client, phone, "mensaje previo", "respuesta previa"
            )

            with (
                patch(
                    "testing.services.webhook_mode.process_incoming_message",
                    new_callable=AsyncMock,
                    return_value=dispatch_result(),
                ) as mock_dispatch,
                patch("testing.services.webhook_mode.LLMService.reset_provider"),
                patch("testing.services.webhook_mode.LLMService.set_prompt_path"),
            ):
                result = await WebhookModeService().send_message(
                    "Gasté 5000 en super", phone, "gemini", "prompt.md"
                )

            assert mock_dispatch.await_args.kwargs["conversation_history"] == [
                {"role": "user", "content": "mensaje previo"},
                {"role": "assistant", "content": "respuesta previa"},
            ]
            assert [message["content"] for message in result.memory] == [
                "mensaje previo",
                "respuesta previa",
                "Gasté 5000 en super",
                "✅ Registré tu egreso: supermercado por $5000 ARS.",
            ]
            assert result.memory_ttl_seconds is not None
            assert result.memory_ttl_seconds > 0
        finally:
            await ConversationHistoryService.clear(client, phone)
            await client.aclose()
