from unittest.mock import patch
import pytest
from app.services.llm import LLMService

@pytest.mark.asyncio
async def test_process_text_expense_valid():
    # Mocking the Gemini API response
    mock_response = {
        "is_expense": True,
        "amount": 2500.50,
        "expense": "Supermercado",
        "currency": "ARS",
        "reply_text": "✅✨ Gasto registrado con éxito: Supermercado por 2500.50 ARS."
    }
    
    with patch("app.services.llm.LLMService.process_text_expense", return_value=mock_response):
        result = await LLMService.process_text_expense("Gasté 2500.5 en el supermercado")
        
        assert result.get("is_expense") is True
        assert result.get("amount") == 2500.50
        assert result.get("expense") == "Supermercado"

@pytest.mark.asyncio
async def test_process_text_expense_not_expense():
    # Mocking the Gemini API response for a non-expense message
    mock_response = {
        "is_expense": False,
        "reply_text": "📌 Este bot solo registra gastos."
    }
    
    with patch("app.services.llm.LLMService.process_text_expense", return_value=mock_response):
        result = await LLMService.process_text_expense("Hola, ¿cómo estás?")
        
        assert result.get("is_expense") is False
        assert "bot solo registra gastos" in result.get("reply_text")
