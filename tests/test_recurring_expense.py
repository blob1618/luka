"""Pruebas focalizadas para el detector de gastos recurrentes (STK-186).

Cubre los 15 casos mínimos de TASK.md más las pruebas de regresión de la revisión independiente:
1. Detecta tres meses consecutivos dentro de ±3 días.
2. No detecta sólo dos meses.
3. No detecta meses no consecutivos.
4. Excluye movimientos anulados.
5. Aísla dos usuarios con datos similares.
6. Separa categorías y monedas diferentes.
7. Normaliza mayúsculas, espacios, acentos o puntuación.
8. Descarta descripciones nulas, vacías o no informativas.
9. Admite montos variables (no exige monto idéntico).
10. Calcula cambio de año.
11. Ajusta día 31, febrero y año bisiesto.
12. Es idempotente en dos ejecuciones sucesivas.
13. Actualiza un candidato existente con evidencia nueva sin duplicarlo.
14. Modo dry-run no persiste en base de datos.
15. No produce efectos secundarios (no crea MovimientoFinanciero ni Recordatorio).
16. Proyección posterior a as_of_date (Item 4).
17. Invalidación automática ante pérdida de evidencia válida (Item 5).
18. Actualización en dry-run sin mutar objeto persistido ni sesión (Item 6).
19. Manejo de descripciones largas con SHA-256 de 64 caracteres (Item 7).
20. Algoritmo acotado sin producto cartesiano desmedido (Item 8).
21. Validación de lookback_months >= 3 y tolerance_days >= 0 (Item 9).
22. Recuperación transaccional segura ante inserción concurrente (Item 3).
23. Streaming y paginación entre límites de lote sin N+1 (Items 1 y 2).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import (
    AvisoRecordatorio,
    Base,
    CandidatoGastoRecurrente,
    Categoria,
    MovimientoFinanciero,
    Recordatorio,
    Usuario,
)
from app.services.recurring_expense import (
    RecurringExpenseService,
    calculate_next_occurrence,
    calculate_pattern_hash,
    normalize_description,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _create_user(session, name="Test User", email=None, whatsapp_id=None) -> Usuario:
    user = Usuario(
        id=uuid.uuid4(),
        nombre=name,
        email=email or f"user_{uuid.uuid4().hex[:8]}@example.com",
        whatsapp_id=whatsapp_id or f"54911{uuid.uuid4().int % 100000000:08d}",
    )
    session.add(user)
    session.commit()
    return user


def _create_category(session, user_id: uuid.UUID, name="Servicios") -> Categoria:
    cat = Categoria(
        id=uuid.uuid4(),
        usuario_id=user_id,
        nombre=name,
    )
    session.add(cat)
    session.commit()
    return cat


def _create_movement(
    session,
    user_id: uuid.UUID,
    cantidad: Decimal | str | int,
    descripcion: str | None,
    fecha_movimiento: date,
    tipo: str = "egreso",
    categoria_id: uuid.UUID | None = None,
    moneda: str = "ARS",
    anulado_en: datetime | None = None,
) -> MovimientoFinanciero:
    mov = MovimientoFinanciero(
        id=uuid.uuid4(),
        usuario_id=user_id,
        tipo=tipo,
        cantidad=Decimal(str(cantidad)),
        descripcion=descripcion,
        fecha_movimiento=fecha_movimiento,
        categoria_id=categoria_id,
        moneda=moneda,
        anulado_en=anulado_en,
    )
    session.add(mov)
    session.commit()
    return mov


class TestPureFunctions:
    def test_normalize_description_variants(self):
        assert normalize_description("Netflix") == "netflix"
        assert normalize_description("  NETFLIX  ") == "netflix"
        assert normalize_description("netFLIX!") == "netflix"
        assert normalize_description("Internet Fibertel") == "internet fibertel"
        assert normalize_description("Alquiler Depto.") == "alquiler depto"
        assert normalize_description("Luz (Edenor)") == "luz edenor"
        assert normalize_description("Café") == "cafe"

    def test_normalize_description_discards_non_informative(self):
        assert normalize_description(None) is None
        assert normalize_description("") is None
        assert normalize_description("   ") is None
        assert normalize_description("???") is None
        assert normalize_description("12345") is None
        assert normalize_description("$ 100") is None
        assert normalize_description("a") is None
        assert normalize_description("gasto") is None
        assert normalize_description("pago") is None
        assert normalize_description("compra") is None
        assert normalize_description("varios") is None
        assert normalize_description("otros") is None

    def test_calculate_pattern_hash(self):
        cat_id = uuid.uuid4()
        h1 = calculate_pattern_hash("netflix", cat_id, "ARS")
        h2 = calculate_pattern_hash("netflix", cat_id, "ars")
        h3 = calculate_pattern_hash("netflix", None, "ARS")
        h4 = calculate_pattern_hash("spotify", cat_id, "ARS")

        assert len(h1) == 64
        assert h1 == h2  # Case insensitive on currency
        assert h1 != h3  # Category difference changes hash
        assert h1 != h4  # Description difference changes hash

    def test_calculate_next_occurrence_year_rollover(self):
        next_date = calculate_next_occurrence(date(2025, 12, 15), target_day=15)
        assert next_date == date(2026, 1, 15)

    def test_calculate_next_occurrence_adjusts_day_31_and_february(self):
        next_date = calculate_next_occurrence(date(2026, 1, 31), target_day=31)
        assert next_date == date(2026, 2, 28)

        next_date_leap = calculate_next_occurrence(date(2028, 1, 31), target_day=31)
        assert next_date_leap == date(2028, 2, 29)

        next_date_apr = calculate_next_occurrence(date(2026, 3, 31), target_day=31)
        assert next_date_apr == date(2026, 4, 30)

        next_date_30 = calculate_next_occurrence(date(2026, 1, 30), target_day=30)
        assert next_date_30 == date(2026, 2, 28)

    def test_calculate_next_occurrence_day_29_in_non_leap_year(self):
        # Caso 6 Jira: ajuste de día 29 en febrero de año no bisiesto (2026)
        next_date_29 = calculate_next_occurrence(date(2026, 1, 29), target_day=29)
        assert next_date_29 == date(2026, 2, 28)

    def test_calculate_next_occurrence_advances_past_as_of_date(self):
        # Si la proyección inmediata ya pasó respecto a as_of_date, avanza los meses necesarios
        last_date = date(2026, 5, 10)
        # as_of_date es 20 de julio: el 10 de junio y 10 de julio ya pasaron
        projected = calculate_next_occurrence(
            last_date,
            target_day=10,
            as_of_date=date(2026, 7, 20),
        )
        assert projected == date(2026, 8, 10)
        assert projected > date(2026, 7, 20)

        # Con fin de mes 31: as_of_date es 1 de marzo 2026
        # feb 28 ya pasó -> avanza a marzo 31
        projected_31 = calculate_next_occurrence(
            date(2026, 1, 31),
            target_day=31,
            as_of_date=date(2026, 3, 1),
        )
        assert projected_31 == date(2026, 3, 31)
        assert projected_31 > date(2026, 3, 1)


class TestRecurringExpenseService:
    def test_detects_three_consecutive_months_within_tolerance(self, db_session):
        user = _create_user(db_session)
        m1 = _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 5, 10))
        m2 = _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 6, 12))
        m3 = _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 7, 11))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
            tolerance_days=3,
        )

        assert res.status == "ok"
        assert res.metrics.candidates_created == 1
        assert len(res.candidates) == 1

        candidate = res.candidates[0]
        assert candidate.usuario_id == user.id
        assert candidate.descripcion_normalizada == "netflix"
        assert candidate.concepto == "Netflix"
        assert candidate.dia_estimado == 11
        assert candidate.proxima_fecha_estimada == date(2026, 8, 11)
        assert candidate.proxima_fecha_estimada > date(2026, 7, 20)
        assert candidate.estado == "pendiente"
        assert candidate.ultima_fecha_movimiento == date(2026, 7, 11)
        assert candidate.evidencia_movimiento_ids == [str(m1.id), str(m2.id), str(m3.id)]
        assert len(candidate.patron_hash) == 64

    def test_rejects_only_two_months(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 5, 10))
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 6, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 6, 20),
        )

        assert res.metrics.candidates_created == 0
        assert len(res.candidates) == 0
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 0

    def test_rejects_non_consecutive_months(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 5, 10))
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 6, 10))
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 8, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 8, 20),
        )

        assert res.metrics.candidates_created == 0
        assert len(res.candidates) == 0

    def test_excludes_annulled_movements(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 5, 10))
        _create_movement(
            db_session,
            user.id,
            "15000",
            "Netflix",
            date(2026, 6, 10),
            anulado_en=datetime.now(timezone.utc),
        )
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 7, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 0
        assert len(res.candidates) == 0

    def test_isolates_users(self, db_session):
        user1 = _create_user(db_session, name="User One")
        user2 = _create_user(db_session, name="User Two")

        for u in (user1, user2):
            _create_movement(db_session, u.id, "10000", "Spotify", date(2026, 5, 5))
            _create_movement(db_session, u.id, "10000", "Spotify", date(2026, 6, 5))
            _create_movement(db_session, u.id, "10000", "Spotify", date(2026, 7, 5))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.users_processed == 2
        assert res.metrics.candidates_created == 2

        candidates_u1 = db_session.execute(
            select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.usuario_id == user1.id)
        ).scalars().all()
        candidates_u2 = db_session.execute(
            select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.usuario_id == user2.id)
        ).scalars().all()

        assert len(candidates_u1) == 1
        assert len(candidates_u2) == 1
        assert candidates_u1[0].id != candidates_u2[0].id

    def test_separates_categories_and_currencies(self, db_session):
        user = _create_user(db_session)
        cat_a = _create_category(db_session, user.id, name="Servicios A")
        cat_b = _create_category(db_session, user.id, name="Servicios B")

        for m in (5, 6, 7):
            _create_movement(
                db_session,
                user.id,
                "5000",
                "Internet",
                date(2026, m, 10),
                categoria_id=cat_a.id,
                moneda="ARS",
            )
            _create_movement(
                db_session,
                user.id,
                "5000",
                "Internet",
                date(2026, m, 10),
                categoria_id=cat_b.id,
                moneda="ARS",
            )
            _create_movement(
                db_session,
                user.id,
                "20",
                "Internet",
                date(2026, m, 10),
                categoria_id=cat_a.id,
                moneda="USD",
            )

        res = RecurringExpenseService.detect_candidates(
            db_session,
            user_id=user.id,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 3
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 3
        hashes = {c.patron_hash for c in persisted}
        assert len(hashes) == 3

    def test_normalizes_description_and_groups_exact(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "12000", "Fibertel", date(2026, 5, 15))
        _create_movement(db_session, user.id, "12000", "  fibertel! ", date(2026, 6, 15))
        _create_movement(db_session, user.id, "12000", "FIBERTEL", date(2026, 7, 15))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 1
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 1
        assert persisted[0].descripcion_normalizada == "fibertel"

    def test_discards_non_informative_descriptions(self, db_session):
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "1000", "Gasto", date(2026, m, 10))
            _create_movement(db_session, user.id, "2000", "12345", date(2026, m, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 0
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 0

    def test_allows_variable_amounts(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "18500.50", "Edenor", date(2026, 5, 10))
        _create_movement(db_session, user.id, "21200.00", "Edenor", date(2026, 6, 11))
        _create_movement(db_session, user.id, "24350.75", "Edenor", date(2026, 7, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 1
        candidate = res.candidates[0]
        assert candidate.descripcion_normalizada == "edenor"
        assert candidate.monto_estimado == Decimal("24350.75")

    def test_calculates_year_rollover(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "30000", "Seguro Auto", date(2025, 10, 15))
        _create_movement(db_session, user.id, "30000", "Seguro Auto", date(2025, 11, 15))
        _create_movement(db_session, user.id, "30000", "Seguro Auto", date(2025, 12, 15))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2025, 12, 20),
        )

        assert res.metrics.candidates_created == 1
        candidate = res.candidates[0]
        assert candidate.proxima_fecha_estimada == date(2026, 1, 15)

    def test_adjusts_day_31_february_and_leap_year(self, db_session):
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "80000", "Alquiler", date(2025, 11, 30))
        _create_movement(db_session, user.id, "80000", "Alquiler", date(2025, 12, 31))
        _create_movement(db_session, user.id, "80000", "Alquiler", date(2026, 1, 31))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 2, 5),
        )

        assert res.metrics.candidates_created == 1
        candidate = res.candidates[0]
        assert candidate.proxima_fecha_estimada == date(2026, 2, 28)

    def test_idempotent_multiple_runs(self, db_session):
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "5000", "Gimnasio", date(2026, m, 10))

        res1 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res1.metrics.candidates_created == 1
        assert res1.metrics.candidates_unchanged == 0

        res2 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res2.metrics.candidates_created == 0
        assert res2.metrics.candidates_unchanged == 1

        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 1

    def test_updates_existing_candidate_with_newer_evidence(self, db_session):
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "10000", "Cuota Club", date(2026, m, 10))

        res1 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res1.metrics.candidates_created == 1
        initial_cand = res1.candidates[0]

        m8 = _create_movement(db_session, user.id, "12500", "Cuota Club", date(2026, 8, 12))

        res2 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 8, 20),
        )
        assert res2.metrics.candidates_created == 0
        assert res2.metrics.candidates_updated == 1

        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 1
        updated = persisted[0]
        assert updated.id == initial_cand.id
        assert updated.ultima_fecha_movimiento == date(2026, 8, 12)
        assert updated.dia_estimado == 12
        assert updated.proxima_fecha_estimada == date(2026, 9, 12)
        assert updated.monto_estimado == Decimal("12500")
        assert str(m8.id) in updated.evidencia_movimiento_ids

    def test_dry_run_does_not_persist(self, db_session):
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "4000", "Hosting", date(2026, m, 1))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
            dry_run=True,
        )

        assert res.dry_run is True
        assert res.metrics.candidates_created == 1
        assert len(res.candidates) == 1
        assert res.candidates[0].descripcion_normalizada == "hosting"

        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 0

    def test_no_side_effects(self, db_session):
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "5000", "Seguro", date(2026, m, 10))

        initial_movements_count = len(db_session.execute(select(MovimientoFinanciero)).scalars().all())

        RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        final_movements_count = len(db_session.execute(select(MovimientoFinanciero)).scalars().all())
        assert final_movements_count == initial_movements_count

        reminders = db_session.execute(select(Recordatorio)).scalars().all()
        assert len(reminders) == 0

    def test_invalidation_when_evidence_becomes_annulled(self, db_session):
        # Item 5: Si un movimiento de evidencia es anulado, el candidato pendiente pasa a 'invalidado'
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "10000", "Alquiler Cochera", date(2026, 5, 5))
        m2 = _create_movement(db_session, user.id, "10000", "Alquiler Cochera", date(2026, 6, 5))
        _create_movement(db_session, user.id, "10000", "Alquiler Cochera", date(2026, 7, 5))

        res1 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res1.metrics.candidates_created == 1
        cand = db_session.execute(select(CandidatoGastoRecurrente)).scalar_one()
        assert cand.estado == "pendiente"

        # Se anula el movimiento de junio
        m2.anulado_en = datetime.now(timezone.utc)
        db_session.commit()

        res2 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res2.metrics.candidates_invalidated == 1

        cand_after = db_session.execute(select(CandidatoGastoRecurrente)).scalar_one()
        assert cand_after.estado == "invalidado"

    def test_dry_run_update_returns_new_projection_without_mutating_persisted_object_or_session(self, db_session):
        # Item 6: En dry-run una actualización devuelve la nueva proyección sin mutar el objeto en sesión
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "10000", "Software SaaS", date(2026, m, 10))

        # Crear candidato en BD
        RecurringExpenseService.detect_candidates(db_session, as_of_date=date(2026, 7, 20))
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalar_one()
        original_fecha = persisted.proxima_fecha_estimada
        original_monto = persisted.monto_estimado
        original_ultima_fecha = persisted.ultima_fecha_movimiento

        # Nuevo movimiento mes 8
        _create_movement(db_session, user.id, "18000", "Software SaaS", date(2026, 8, 12))

        # Ejecutar en dry-run
        res_dry = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 8, 20),
            dry_run=True,
        )

        assert res_dry.metrics.candidates_updated == 1
        assert len(res_dry.candidates) == 1
        dry_cand = res_dry.candidates[0]

        # El candidato devuelto en dry-run tiene la nueva proyección completa
        assert dry_cand.monto_estimado == Decimal("18000")
        assert dry_cand.ultima_fecha_movimiento == date(2026, 8, 12)
        assert dry_cand.proxima_fecha_estimada == date(2026, 9, 12)

        # Pero el objeto en la sesión NO fue mutado ni la sesión quedó sucia
        assert not db_session.is_modified(persisted)
        assert persisted.monto_estimado == original_monto
        assert persisted.ultima_fecha_movimiento == original_ultima_fecha
        assert persisted.proxima_fecha_estimada == original_fecha

    def test_handles_very_long_descriptions_with_sha256_hash(self, db_session):
        # Item 7: Descripción larga (1000 caracteres) genera hash de 64 caracteres y se persiste sin error
        user = _create_user(db_session)
        long_desc = "Servicio Corporativo de Telecomunicaciones Especiales " + "X" * 900

        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "50000", long_desc, date(2026, m, 10))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.metrics.candidates_created == 1
        cand = db_session.execute(select(CandidatoGastoRecurrente)).scalar_one()
        assert len(cand.patron_hash) == 64
        assert cand.descripcion_normalizada == normalize_description(long_desc)

    def test_validates_lookback_months_and_tolerance_days(self, db_session):
        # Item 9: Validar lookback_months >= 3 y tolerance_days >= 0, batch_size >= 1
        with pytest.raises(ValueError, match="lookback_months debe ser mayor o igual a 3"):
            RecurringExpenseService.detect_candidates(db_session, lookback_months=2)

        with pytest.raises(ValueError, match="tolerance_days debe ser mayor o igual a 0"):
            RecurringExpenseService.detect_candidates(db_session, tolerance_days=-1)

        with pytest.raises(ValueError, match="batch_size debe ser mayor o igual a 1"):
            RecurringExpenseService.detect_candidates(db_session, batch_size=0)

        with pytest.raises(ValueError, match="batch_size debe ser mayor o igual a 1"):
            RecurringExpenseService.detect_candidates(db_session, batch_size=-1)

    def test_bounded_day_matching_avoids_cartesian_product(self, db_session):
        # Item 8: Múltiples movimientos por mes (incluso duplicados de día)
        user = _create_user(db_session)
        # 15 movimientos por mes en días repetidos
        for m in (5, 6, 7):
            for day in (5, 5, 10, 10, 12, 12, 15, 15, 20, 20, 25, 25):
                _create_movement(db_session, user.id, "1000", "Multiples Consumos", date(2026, m, day))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 28),
            tolerance_days=2,
        )

        assert res.status == "ok"
        assert res.metrics.candidates_created == 1
        cand = res.candidates[0]
        assert cand.dia_estimado in (25, 20, 15, 12, 10, 5)

    def test_unique_collision_savepoint_recovery_after_preload(self, db_session, monkeypatch):
        """Verifica la recuperación transaccional mediante savepoint ante una colisión de clave UNIQUE.

        Fuerza la colisión después de que la precarga de candidatos ya concluyó vacía.
        Comprueba que:
        - el savepoint absorbe solamente el conflicto esperado;
        - la sesión exterior sigue siendo utilizable;
        - se recupera el candidato ganador;
        - no queda un candidato duplicado;
        - el resultado y las métricas son coherentes.
        Nota: La concurrencia real con workers y conexiones múltiples en PostgreSQL queda para STK-189.
        """
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "7000", "Seguro Vida", date(2026, m, 10))

        p_hash = calculate_pattern_hash("seguro vida", None, "ARS")
        winner_id = uuid.uuid4()

        orig_flush = db_session.flush
        collision_triggered = False

        def flush_with_concurrent_collision(*args, **kwargs):
            nonlocal collision_triggered
            # Solo interceptar el flush que intenta insertar el CandidatoGastoRecurrente
            if not collision_triggered and any(isinstance(obj, CandidatoGastoRecurrente) for obj in db_session.new):
                collision_triggered = True
                # Simular que otro worker insertó el candidato en la BD justo antes de este flush
                SessionMaker = sessionmaker(bind=db_session.bind)
                with SessionMaker() as other_sess:
                    winner_cand = CandidatoGastoRecurrente(
                        id=winner_id,
                        usuario_id=user.id,
                        patron_hash=p_hash,
                        descripcion_normalizada="seguro vida",
                        categoria_id=None,
                        moneda="ARS",
                        concepto="Seguro Vida Ganador",
                        monto_estimado=Decimal("7000"),
                        dia_estimado=10,
                        proxima_fecha_estimada=date(2026, 8, 10),
                        estado="pendiente",
                        ultima_fecha_movimiento=date(2026, 7, 10),
                        evidencia_movimiento_ids=["dummy1", "dummy2", "dummy3"],
                    )
                    other_sess.add(winner_cand)
                    other_sess.commit()

                raise SqlIntegrityError(
                    "INSERT INTO candidato_gasto_recurrente...",
                    {},
                    Exception("UNIQUE constraint failed: candidato_gasto_recurrente.usuario_id, candidato_gasto_recurrente.patron_hash"),
                )
            return orig_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", flush_with_concurrent_collision)

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        assert res.status == "ok"
        assert res.metrics.candidates_created == 0
        assert res.metrics.candidates_unchanged == 1
        assert len(res.candidates) == 1

        # Verificar que la sesión exterior sigue siendo utilizable tras el savepoint
        users_in_session = db_session.execute(select(Usuario)).scalars().all()
        assert len(users_in_session) >= 1

        # Se recuperó el candidato ganador y no quedó duplicado
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 1
        assert persisted[0].id == winner_id
        assert persisted[0].concepto == "Seguro Vida Ganador"

    def test_integrity_error_without_conflicting_record_raises_runtime_error(self, db_session, monkeypatch):
        """Verifica que si ocurre un IntegrityError sin registro concurrente, no se continúe silenciosamente."""
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "5000", "Luz", date(2026, m, 10))

        orig_flush = db_session.flush

        def failing_flush(*args, **kwargs):
            if any(isinstance(obj, CandidatoGastoRecurrente) for obj in db_session.new):
                raise SqlIntegrityError(
                    "INSERT INTO candidato_gasto_recurrente...",
                    {},
                    Exception("foreign key or check constraint failure"),
                )
            return orig_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", failing_flush)

        with pytest.raises(RuntimeError, match="pero el registro concurrente no fue encontrado"):
            RecurringExpenseService.detect_candidates(
                db_session,
                as_of_date=date(2026, 7, 20),
            )



    def test_streaming_and_batch_processing_across_chunk_boundaries(self, db_session):
        # Items 1 y 2: Probar streaming con batch_size pequeño (ej. 2)
        # verificando que los movimientos de un usuario acumulados a través de límites de lote se procesen correctamente
        u1 = _create_user(db_session, name="User A")
        u2 = _create_user(db_session, name="User B")

        for u in (u1, u2):
            for m in (5, 6, 7):
                _create_movement(db_session, u.id, "3000", "Cuota Mensual", date(2026, m, 15))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
            batch_size=2,  # Fuerza cruzar límites de lote durante el streaming
        )

        assert res.metrics.users_processed == 2
        assert res.metrics.candidates_created == 2
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 2

    def test_rejects_movements_outside_three_days_tolerance(self, db_session):
        # Caso 2 Jira: fechas fuera de tolerancia (diff 4 > 3 días) en 3 meses consecutivos
        user = _create_user(db_session)
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 5, 10))
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 6, 10))
        _create_movement(db_session, user.id, "15000", "Netflix", date(2026, 7, 14))

        res = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
            tolerance_days=3,
        )

        assert res.metrics.candidates_created == 0
        assert len(res.candidates) == 0
        persisted = db_session.execute(select(CandidatoGastoRecurrente)).scalars().all()
        assert len(persisted) == 0

    def test_detector_preserves_accepted_state_when_updating_projection(self, db_session):
        # Caso 7 Jira: candidato aceptado actualiza proyección sin mutar ni revertir estado
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "10000", "Streaming", date(2026, m, 10))

        res1 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )
        assert res1.metrics.candidates_created == 1
        cand = db_session.execute(select(CandidatoGastoRecurrente)).scalar_one()
        assert cand.estado == "pendiente"

        cand.estado = "aceptado"
        cand.decision_en = datetime.now(timezone.utc)
        cand.decision_origen = "interactivo"
        db_session.commit()

        m8 = _create_movement(db_session, user.id, "12000", "Streaming", date(2026, 8, 11))

        res2 = RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 8, 20),
        )
        assert res2.metrics.candidates_created == 0
        assert res2.metrics.candidates_updated == 1

        db_session.refresh(cand)
        assert cand.ultima_fecha_movimiento == date(2026, 8, 11)
        assert cand.dia_estimado == 11
        assert cand.proxima_fecha_estimada == date(2026, 9, 11)
        assert cand.monto_estimado == Decimal("12000")
        assert str(m8.id) in cand.evidencia_movimiento_ids
        assert cand.estado == "aceptado"

    def test_detection_does_not_create_avisos(self, db_session):
        # Caso 8 Jira: detección determinista no inserta filas en AvisoRecordatorio
        user = _create_user(db_session)
        for m in (5, 6, 7):
            _create_movement(db_session, user.id, "5000", "Seguro", date(2026, m, 10))

        RecurringExpenseService.detect_candidates(
            db_session,
            as_of_date=date(2026, 7, 20),
        )

        avisos = db_session.execute(select(AvisoRecordatorio)).scalars().all()
        assert len(avisos) == 0
        reminders = db_session.execute(select(Recordatorio)).scalars().all()
        assert len(reminders) == 0
