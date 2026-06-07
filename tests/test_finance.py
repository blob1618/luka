from app.services.finance import FinanceService

def test_check_dynamic_budget():
    # Basic test for dynamic budget reallocation message
    user_id = 1
    new_expense = 5000.0
    category = "ocio"
    
    result = FinanceService.check_dynamic_budget(user_id, new_expense, category)
    
    assert "buen registro" in result.lower()
    assert "ropa" in result.lower()

def test_generate_expense_chart():
    # Test that the chart generation returns bytes representing an image
    expenses = {
        "Comida": 15000.0,
        "Transporte": 5000.0,
        "Ocio": 2000.0
    }
    
    chart_bytes = FinanceService.generate_expense_chart(expenses)
    
    assert isinstance(chart_bytes, bytes)
    assert len(chart_bytes) > 0
    # PNG magic number check
    assert chart_bytes.startswith(b'\x89PNG')
