import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date
from uuid import UUID

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


# =============================================================================
# Tests de validación (STK-85)
# =============================================================================

def test_validate_budget_amount_valid():
    is_valid, msg = FinanceService.validate_budget_amount(50000.0)
    assert is_valid is True
    assert msg == ""


def test_validate_budget_amount_zero():
    is_valid, msg = FinanceService.validate_budget_amount(0)
    assert is_valid is False
    assert "mayor a cero" in msg


def test_validate_budget_amount_negative():
    is_valid, msg = FinanceService.validate_budget_amount(-100)
    assert is_valid is False
    assert "mayor a cero" in msg


def test_validate_budget_amount_none():
    is_valid, msg = FinanceService.validate_budget_amount(None)
    assert is_valid is False
    assert "vacío" in msg


def test_validate_budget_amount_too_high():
    is_valid, msg = FinanceService.validate_budget_amount(10_000_000_000)
    assert is_valid is False
    assert "demasiado alto" in msg


def test_validate_budget_amount_string():
    """Validación con string inválido."""
    is_valid, msg = FinanceService.validate_budget_amount("abc")
    assert is_valid is False
    assert "numérico" in msg


# =============================================================================
# Tests de _obtener_rango_mes
# =============================================================================

def test_obtener_rango_mes_default():
    """Sin parámetro, usa el mes actual."""
    inicio, fin = FinanceService._obtener_rango_mes()
    hoy = datetime.utcnow()
    assert inicio.month == hoy.month
    assert inicio.year == hoy.year
    assert inicio.day == 1


def test_obtener_rango_mes_especifico():
    """Con mes específico."""
    inicio, fin = FinanceService._obtener_rango_mes("2026-07")
    assert inicio == date(2026, 7, 1)
    assert fin == date(2026, 7, 31)


def test_obtener_rango_mes_diciembre():
    """Diciembre (mes 12) debe calcular fin correctamente."""
    inicio, fin = FinanceService._obtener_rango_mes("2026-12")
    assert inicio == date(2026, 12, 1)
    assert fin == date(2026, 12, 31)


def test_obtener_rango_mes_invalido():
    """Formato inválido debe lanzar ValueError."""
    with pytest.raises(ValueError, match="inválido"):
        FinanceService._obtener_rango_mes("julio-2026")


# =============================================================================
# Tests de _obtener_categoria_por_nombre
# =============================================================================

def test_obtener_categoria_por_nombre_usuario():
    """Busca categoría del usuario primero."""
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    mock_categoria = MagicMock()
    mock_categoria.nombre = "Comida"
    mock_categoria.id = UUID("00000000-0000-0000-0000-000000000010")

    # Configurar el primer filter (usuario) para retornar la categoría
    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = mock_categoria
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "comida")
    assert result is not None
    assert result.nombre == "Comida"


def test_obtener_categoria_por_nombre_default():
    """Si no encuentra del usuario, busca en default."""
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    mock_categoria = MagicMock()
    mock_categoria.nombre = "Supermercado"
    mock_categoria.id = UUID("00000000-0000-0000-0000-000000000020")

    # Primer filter (usuario) retorna None
    mock_query1 = MagicMock()
    mock_filter1 = MagicMock()
    mock_filter1.first.return_value = None
    mock_query1.filter.return_value = mock_filter1

    # Segundo filter (default) retorna la categoría
    mock_query2 = MagicMock()
    mock_filter2 = MagicMock()
    mock_filter2.first.return_value = mock_categoria
    mock_query2.filter.return_value = mock_filter2

    mock_db.query.side_effect = [mock_query1, mock_query2]

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "supermercado")
    assert result is not None
    assert result.nombre == "Supermercado"


def test_obtener_categoria_por_nombre_no_encontrada():
    """Categoría inexistente retorna None."""
    mock_db = MagicMock()
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = None
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService._obtener_categoria_por_nombre(mock_db, user_id, "inexistente")
    assert result is None


# =============================================================================
# Tests de set_budget_limit (STK-86 + STK-87)
# =============================================================================

@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_create(mock_session_local):
    """Crear un nuevo límite presupuestario."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    # Mock de categoría encontrada
    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    # Mock de query para categoría
    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    # Mock de query para límite existente (None = crear nuevo)
    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = None
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="salidas",
        amount=50000.0,
        month="2026-07",
    )

    assert result["success"] is True
    assert "$50,000" in result["message"]
    assert "Salidas" in result["message"]
    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_update(mock_session_local):
    """Actualizar un límite existente."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 30000.0
    mock_limite.id = UUID("00000000-0000-0000-0000-000000000100")

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = mock_limite
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="salidas",
        amount=50000.0,
        month="2026-07",
    )

    assert result["success"] is True
    assert "Actualicé" in result["message"]
    assert mock_limite.cantidad_max == 50000.0
    mock_db.commit.assert_called_once()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_invalid_amount(mock_session_local):
    """Monto inválido debe fallar sin tocar la BD."""
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="comida",
        amount=-100,
    )

    assert result["success"] is False
    assert "mayor a cero" in result["message"]
    mock_session_local.return_value.commit.assert_not_called()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_invalid_month_format(mock_session_local):
    """Formato de mes inválido debe fallar."""
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="comida",
        amount=10000,
        month="julio-2026",
    )

    assert result["success"] is False
    assert "inválido" in result["message"].lower()
    mock_session_local.return_value.commit.assert_not_called()


@patch("app.services.finance.SessionLocal")
def test_set_budget_limit_categoria_no_encontrada(mock_session_local):
    """Categoría inexistente debe fallar."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")

    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = None
    mock_query.filter.return_value = mock_filter
    mock_db.query.return_value = mock_query

    result = FinanceService.set_budget_limit(
        user_id=user_id,
        category="inexistente",
        amount=10000,
        month="2026-07",
    )

    assert result["success"] is False
    assert "no encontré" in result["message"].lower()


# =============================================================================
# Tests de get_budget_limit (STK-88)
# =============================================================================

@patch("app.services.finance.SessionLocal")
def test_get_budget_limit_found(mock_session_local):
    """Límite encontrado."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Salidas"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 50000.0
    mock_limite.inicio_periodo = date(2026, 7, 1)
    mock_limite.fin_periodo = date(2026, 7, 31)

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = mock_limite
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.get_budget_limit(
        user_id=user_id,
        category="salidas",
        month="2026-07",
    )

    assert result["found"] is True
    assert result["category"] == "Salidas"
    assert result["amount"] == 50000.0
    assert result["month"] == "2026-07"


@patch("app.services.finance.SessionLocal")
def test_get_budget_limit_not_found(mock_session_local):
    """Límite no encontrado."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_query_cat = MagicMock()
    mock_filter_cat = MagicMock()
    mock_filter_cat.first.return_value = mock_categoria
    mock_query_cat.filter.return_value = mock_filter_cat

    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.first.return_value = None
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_db.query.side_effect = [mock_query_cat, mock_query_lim]

    result = FinanceService.get_budget_limit(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["found"] is False
    assert "no tenés" in result["message"].lower()


# =============================================================================
# Tests de get_all_budget_limits
# =============================================================================

@patch("app.services.finance.SessionLocal")
def test_get_all_budget_limits(mock_session_local):
    """Listar todos los límites de un usuario."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    cat_salidas = UUID("00000000-0000-0000-0000-000000000010")
    cat_comida = UUID("00000000-0000-0000-0000-000000000020")

    mock_lim1 = MagicMock()
    mock_lim1.id = UUID("00000000-0000-0000-0000-000000000100")
    mock_lim1.categoria_id = cat_salidas
    mock_lim1.cantidad_max = 50000.0
    mock_lim1.inicio_periodo = date(2026, 7, 1)

    mock_lim2 = MagicMock()
    mock_lim2.id = UUID("00000000-0000-0000-0000-000000000200")
    mock_lim2.categoria_id = cat_comida
    mock_lim2.cantidad_max = 30000.0
    mock_lim2.inicio_periodo = date(2026, 7, 1)

    mock_cat1 = MagicMock()
    mock_cat1.nombre = "Salidas"
    mock_cat2 = MagicMock()
    mock_cat2.nombre = "Comida"

    # Configurar queries
    mock_query_lim = MagicMock()
    mock_filter_lim = MagicMock()
    mock_filter_lim.all.return_value = [mock_lim1, mock_lim2]
    mock_query_lim.filter.return_value = mock_filter_lim

    mock_query_cat1 = MagicMock()
    mock_filter_cat1 = MagicMock()
    mock_filter_cat1.first.return_value = mock_cat1
    mock_query_cat1.filter.return_value = mock_filter_cat1

    mock_query_cat2 = MagicMock()
    mock_filter_cat2 = MagicMock()
    mock_filter_cat2.first.return_value = mock_cat2
    mock_query_cat2.filter.return_value = mock_filter_cat2

    mock_db.query.side_effect = [mock_query_lim, mock_query_cat1, mock_query_cat2]

    result = FinanceService.get_all_budget_limits(user_id=user_id, month="2026-07")

    assert len(result) == 2
    assert result[0]["category"] == "Salidas"
    assert result[1]["category"] == "Comida"


# =============================================================================
# Tests de check_budget_status
# =============================================================================

@patch("app.services.finance.SessionLocal")
def test_check_budget_status_under_limit(mock_session_local):
    """Gasto por debajo del límite."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_limite = MagicMock()
    mock_limite.cantidad_max = 30000.0
    mock_limite.inicio_periodo = date(2026, 7, 1)
    mock_limite.fin_periodo = date(2026, 7, 31)

    # Query 1: categoría
    mock_q1 = MagicMock()
    mock_f1 = MagicMock()
    mock_f1.first.return_value = mock_categoria
    mock_q1.filter.return_value = mock_f1

    # Query 2: límite
    mock_q2 = MagicMock()
    mock_f2 = MagicMock()
    mock_f2.first.return_value = mock_limite
    mock_q2.filter.return_value = mock_f2

    # Query 3: gastos
    mock_q3 = MagicMock()
    mock_f3 = MagicMock()
    mock_f3.all.return_value = [(15000.0,)]
    mock_q3.filter.return_value = mock_f3

    mock_db.query.side_effect = [mock_q1, mock_q2, mock_q3]

    result = FinanceService.check_budget_status(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["has_limit"] is True
    assert result["limit"] == 30000.0
    assert result["spent"] == 15000.0
    assert result["remaining"] == 15000.0
    assert result["percentage"] == 50.0


@patch("app.services.finance.SessionLocal")
def test_check_budget_status_no_limit(mock_session_local):
    """Sin límite configurado."""
    mock_db = mock_session_local.return_value
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    categoria_id = UUID("00000000-0000-0000-0000-000000000010")

    mock_categoria = MagicMock()
    mock_categoria.id = categoria_id
    mock_categoria.nombre = "Comida"

    mock_q1 = MagicMock()
    mock_f1 = MagicMock()
    mock_f1.first.return_value = mock_categoria
    mock_q1.filter.return_value = mock_f1

    mock_q2 = MagicMock()
    mock_f2 = MagicMock()
    mock_f2.first.return_value = None
    mock_q2.filter.return_value = mock_f2

    mock_db.query.side_effect = [mock_q1, mock_q2]

    result = FinanceService.check_budget_status(
        user_id=user_id,
        category="comida",
        month="2026-07",
    )

    assert result["has_limit"] is False