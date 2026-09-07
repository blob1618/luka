import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.models.database import (
    Base,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.finance import FinanceService


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)

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


def create_user(session, whatsapp_id="5491111111111", email=None):
    user = Usuario(
        id=uuid.uuid4(),
        whatsapp_id=whatsapp_id,
        nombre="Test User",
        email=email or f"user_{uuid.uuid4()}@example.com",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def create_category(session, user_id, nombre="Comida", es_default=False):
    cat = Categoria(
        id=uuid.uuid4(),
        usuario_id=user_id,
        nombre=nombre,
        es_default=es_default,
        esta_eliminado=False,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def create_movement(
    session,
    user_id,
    tipo="egreso",
    cantidad=Decimal("1000.00"),
    moneda="ARS",
    descripcion="super",
    categoria_id=None,
    fecha_movimiento=None,
):
    mov = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user_id,
        categoria_id=categoria_id,
        tipo=tipo,
        cantidad=cantidad,
        moneda=moneda,
        descripcion=descripcion,
        fecha_movimiento=fecha_movimiento or date.today(),
        origen="whatsapp_text",
        whatsapp_message_id=f"wamid.{uuid.uuid4()}",
    )
    session.add(mov)
    session.commit()
    session.refresh(mov)
    return mov


class TestFinanceQueryUserIsolation:
    """Verifica el aislamiento estricto entre usuarios (criterio crítico STK-150)."""

    def test_query_movements_isolates_user_data(self, db_context):
        session = db_context["session"]
        user_a = create_user(session, whatsapp_id="5491100000001")
        user_b = create_user(session, whatsapp_id="5491100000002")

        cat_a = create_category(session, user_a.id, "Comida")
        cat_b = create_category(session, user_b.id, "Transporte")

        # User A movements
        create_movement(session, user_a.id, tipo="egreso", cantidad=Decimal("500.00"), descripcion="Gasto A1", categoria_id=cat_a.id)
        create_movement(session, user_a.id, tipo="ingreso", cantidad=Decimal("2000.00"), descripcion="Ingreso A2")

        # User B movements
        create_movement(session, user_b.id, tipo="egreso", cantidad=Decimal("150.00"), descripcion="Gasto B1", categoria_id=cat_b.id)

        # Consulta de User A
        result_a = FinanceService.query_movements(user_a.id, session=session)
        assert result_a.status == "ok"
        assert result_a.total_found == 2
        assert len(result_a.movements) == 2
        descriptions_a = {m.descripcion for m in result_a.movements}
        assert descriptions_a == {"Gasto A1", "Ingreso A2"}
        # Ningún movimiento de User B debe aparecer
        assert "Gasto B1" not in descriptions_a

        # Consulta de User B
        result_b = FinanceService.query_movements(user_b.id, session=session)
        assert result_b.status == "ok"
        assert result_b.total_found == 1
        assert len(result_b.movements) == 1
        assert result_b.movements[0].descripcion == "Gasto B1"
        assert result_b.movements[0].categoria_nombre == "Transporte"

    def test_query_by_phone_isolates_users(self, db_context):
        session = db_context["session"]
        user_a = create_user(session, whatsapp_id="5491100000001")
        user_b = create_user(session, whatsapp_id="5491100000002")

        create_movement(session, user_a.id, descripcion="User A secret")
        create_movement(session, user_b.id, descripcion="User B secret")

        result_a = FinanceService.query_movements_by_phone("5491100000001")
        assert result_a.status == "ok"
        assert result_a.total_found == 1
        assert result_a.movements[0].descripcion == "User A secret"

        result_b = FinanceService.query_movements_by_phone("5491100000002")
        assert result_b.status == "ok"
        assert result_b.total_found == 1
        assert result_b.movements[0].descripcion == "User B secret"

    def test_query_movements_category_outerjoin_tenant_isolation(self, db_context):
        """Verifica que no se filtre ni exponga el nombre de una categoría perteneciente a otro usuario."""
        session = db_context["session"]
        user_a = create_user(session, whatsapp_id="5491100000001")
        user_b = create_user(session, whatsapp_id="5491100000002")

        cat_b = create_category(session, user_b.id, "Categoría Privada B")

        # Movimiento donde el movimiento es de user_a pero apunta al categoria_id de user_b
        create_movement(session, user_a.id, descripcion="Gasto anómalo A", categoria_id=cat_b.id)

        # Al consultar user_a, no debe resolverse el nombre de la categoría de user_b
        res_a = FinanceService.query_movements(user_a.id, session=session)
        assert res_a.status == "ok"
        assert res_a.total_found == 1
        assert res_a.movements[0].descripcion == "Gasto anómalo A"
        assert res_a.movements[0].categoria_nombre is None

        # Si user_a intenta filtrar explícitamente por el categoria_id de user_b, no debe devolver nada
        res_cat_b = FinanceService.query_movements(user_a.id, categoria_id=cat_b.id, session=session)
        assert res_cat_b.status == "ok"
        assert res_cat_b.total_found == 0
        assert res_cat_b.movements == []

    def test_unknown_user_returns_user_not_found(self, db_context):
        result = FinanceService.query_movements(uuid.uuid4())
        assert result.status == "user_not_found"

        result_phone = FinanceService.query_movements_by_phone("5499999999999")
        assert result_phone.status == "user_not_found"


class TestFinanceQueryReadOnly:
    """Verifica que la consulta no produzca inserciones ni modificaciones financieras."""

    def test_query_movements_does_not_modify_database(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        cat = create_category(session, user.id, "Comida")
        create_movement(session, user.id, categoria_id=cat.id)

        # Contar filas antes de la consulta
        count_mov_before = session.query(MovimientoFinanciero).count()
        count_cat_before = session.query(Categoria).count()
        count_usr_before = session.query(Usuario).count()
        count_lim_before = session.query(LimiteCategoria).count()

        # Ejecutar varias consultas con distintos filtros
        FinanceService.query_movements(user.id, session=session)
        FinanceService.query_movements(user.id, movement_type="egreso", session=session)
        FinanceService.query_movements(user.id, category_name="Comida", session=session)
        FinanceService.query_movements_by_phone(user.whatsapp_id)

        # Verificar que el número de filas permanece idéntico
        assert session.query(MovimientoFinanciero).count() == count_mov_before
        assert session.query(Categoria).count() == count_cat_before
        assert session.query(Usuario).count() == count_usr_before
        assert session.query(LimiteCategoria).count() == count_lim_before


class TestFinanceQueryFilters:
    """Verifica el filtrado por tipo, categoría, rango de fechas y límites (STK-149/150)."""

    def test_filter_by_movement_type(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        create_movement(session, user.id, tipo="egreso", descripcion="Gasto 1")
        create_movement(session, user.id, tipo="egreso", descripcion="Gasto 2")
        create_movement(session, user.id, tipo="ingreso", descripcion="Sueldo")

        egresos = FinanceService.query_movements(user.id, movement_type="egreso", session=session)
        assert egresos.status == "ok"
        assert egresos.total_found == 2
        assert all(m.tipo == "egreso" for m in egresos.movements)

        ingresos = FinanceService.query_movements(user.id, movement_type="ingreso", session=session)
        assert ingresos.status == "ok"
        assert ingresos.total_found == 1
        assert ingresos.movements[0].tipo == "ingreso"
        assert ingresos.movements[0].descripcion == "Sueldo"

    def test_filter_by_category_name_and_synonym(self, db_context):
        session = db_context["session"]
        user = create_user(session)
        cat_comida = create_category(session, user.id, "Comida")
        cat_salud = create_category(session, user.id, "Salud")

        create_movement(session, user.id, descripcion="Supermercado", categoria_id=cat_comida.id)
        create_movement(session, user.id, descripcion="Farmacia", categoria_id=cat_salud.id)
        create_movement(session, user.id, descripcion="Sin categoría", categoria_id=None)

        # Coincidencia exacta de nombre
        res_comida = FinanceService.query_movements(user.id, category_name="Comida", session=session)
        assert res_comida.status == "ok"
        assert res_comida.total_found == 1
        assert res_comida.movements[0].descripcion == "Supermercado"

        # Coincidencia por sinónimo de taxonomía ("super" -> "Comida")
        res_super = FinanceService.query_movements(user.id, category_name="super", session=session)
        assert res_super.status == "ok"
        assert res_super.total_found == 1
        assert res_super.movements[0].descripcion == "Supermercado"

        # Categoría que no existe para el usuario
        res_viajes = FinanceService.query_movements(user.id, category_name="Viajes", session=session)
        assert res_viajes.status == "ok"
        assert res_viajes.total_found == 0
        assert res_viajes.movements == []

    def test_filter_by_date_range(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        today = date.today()
        yesterday = today - timedelta(days=1)
        last_week = today - timedelta(days=7)

        create_movement(session, user.id, descripcion="Hoy", fecha_movimiento=today)
        create_movement(session, user.id, descripcion="Ayer", fecha_movimiento=yesterday)
        create_movement(session, user.id, descripcion="Semana pasada", fecha_movimiento=last_week)

        # Rango: desde ayer hasta hoy
        res_recientes = FinanceService.query_movements(
            user.id, start_date=yesterday, end_date=today, session=session
        )
        assert res_recientes.total_found == 2
        descs = {m.descripcion for m in res_recientes.movements}
        assert descs == {"Hoy", "Ayer"}

        # Rango inválido (start > end)
        res_invalido = FinanceService.query_movements(
            user.id, start_date=today, end_date=yesterday, session=session
        )
        assert res_invalido.status == "invalid_filters"

    def test_max_limit_enforced_to_five(self, db_context):
        """STK-149 exige como máximo 5 movimientos en la respuesta."""
        session = db_context["session"]
        user = create_user(session)

        # Crear 8 movimientos
        for i in range(8):
            create_movement(
                session,
                user.id,
                descripcion=f"Movimiento {i}",
                fecha_movimiento=date.today() - timedelta(days=i),
            )

        # Solicitando 10 -> debe estar acotado a 5
        res_10 = FinanceService.query_movements(user.id, limit=10, session=session)
        assert res_10.status == "ok"
        assert res_10.total_found == 8
        assert len(res_10.movements) == 5

        # Solicitud por defecto -> 5
        res_default = FinanceService.query_movements(user.id, session=session)
        assert len(res_default.movements) == 5

        # Solicitando menor cantidad (3) -> respeta 3
        res_3 = FinanceService.query_movements(user.id, limit=3, session=session)
        assert res_3.total_found == 8
        assert len(res_3.movements) == 3

    def test_deterministic_ordering_newest_first(self, db_context):
        session = db_context["session"]
        user = create_user(session)

        d1 = date(2026, 9, 1)
        d2 = date(2026, 9, 2)
        d3 = date(2026, 9, 3)

        create_movement(session, user.id, descripcion="D1", fecha_movimiento=d1)
        create_movement(session, user.id, descripcion="D3", fecha_movimiento=d3)
        create_movement(session, user.id, descripcion="D2", fecha_movimiento=d2)

        res = FinanceService.query_movements(user.id, session=session)
        assert [m.descripcion for m in res.movements] == ["D3", "D2", "D1"]

    def test_deterministic_ordering_tiebreaker_with_more_than_five_ties(self, db_context):
        """Verifica que ante empate exacto de fecha y creación en >5 filas, id.desc() desempate de forma determinista."""
        session = db_context["session"]
        user = create_user(session)

        fixed_date = date(2026, 9, 5)
        fixed_dt = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

        created_ids = []
        for i in range(7):
            mov = MovimientoFinanciero(
                id=uuid.uuid4(),
                usuario_id=user.id,
                tipo="egreso",
                cantidad=Decimal("100.00"),
                moneda="ARS",
                descripcion=f"Tie {i}",
                fecha_movimiento=fixed_date,
                creado_en=fixed_dt,
                origen="whatsapp_text",
            )
            session.add(mov)
            created_ids.append(mov.id)
        session.commit()

        res1 = FinanceService.query_movements(user.id, limit=5, session=session)
        res2 = FinanceService.query_movements(user.id, limit=5, session=session)

        assert res1.total_found == 7
        assert len(res1.movements) == 5

        # En la DB, id.desc() selecciona determinísticamente los 5 mayores ids
        # (al comparar UUIDs o sus representaciones)
        expected_ids = [str(uid) for uid in sorted(created_ids, reverse=True)[:5]]
        actual_ids = [m.id for m in res1.movements]
        assert actual_ids == expected_ids
        assert [m.id for m in res1.movements] == [m.id for m in res2.movements]
