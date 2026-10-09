"""
Tests unitarios del modelo Billetera y relacion con MovimientoFinanciero (STK-237).

LIMITACION DE EVIDENCIA:
SQLite con SQLAlchemy Numeric sin escala explicita almacena valores numericos
como float o texto internamente dependiendo del dialecto y no emula la precision
arbitraria ni el comportamiento exacto de tipo Numeric de PostgreSQL.
Estos tests en SQLite validan la capa de modelos ORM, restricciones de integridad
referencial compuesta (foreign keys), unicidad y check constraints en memoria.
El arnes dedicado de PostgreSQL (supabase/tests/stk237_wallets.sql) ha sido
preparado para ejecucion sobre base desechable pero no ha sido ejecutado en este entorno.
"""

from datetime import datetime
from decimal import Decimal
import unittest
import uuid

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Usuario, Billetera, MovimientoFinanciero


class TestWalletModel(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()

    def tearDown(self):
        self.session.rollback()
        self.session.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _crear_usuario(self, nombre="Test User", email=None):
        if email is None:
            email = f"user_{uuid.uuid4().hex[:8]}@example.com"
        u = Usuario(
            id=uuid.uuid4(),
            nombre=nombre,
            email=email,
        )
        self.session.add(u)
        self.session.commit()
        return u

    def test_usuario_sin_movimientos(self):
        """Usuario sin movimientos no tiene billeteras ni movimientos asociados."""
        u = self._crear_usuario("Usuario Sin Movimientos")
        wallets = self.session.query(Billetera).filter_by(usuario_id=u.id).all()
        movs = self.session.query(MovimientoFinanciero).filter_by(usuario_id=u.id).all()
        self.assertEqual(len(wallets), 0)
        self.assertEqual(len(movs), 0)

    def test_wallet_valida_y_movimiento(self):
        """Creacion de billetera valida y movimiento asociado con persistencia correcta."""
        u = self._crear_usuario()
        w = Billetera(
            id=uuid.uuid4(),
            usuario_id=u.id,
            nombre="Principal",
            moneda="ARS",
        )
        self.session.add(w)
        self.session.commit()

        m = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u.id,
            billetera_id=w.id,
            tipo="ingreso",
            cantidad=Decimal("1500.50"),
            moneda="ARS",
            descripcion="Ingreso de prueba",
        )
        self.session.add(m)
        self.session.commit()

        m_db = self.session.query(MovimientoFinanciero).filter_by(id=m.id).one()
        self.assertEqual(m_db.billetera_id, w.id)
        self.assertEqual(m_db.usuario_id, u.id)
        self.assertEqual(m_db.moneda, "ARS")
        self.assertEqual(m_db.cantidad, Decimal("1500.50"))

    def test_dos_monedas(self):
        """Un usuario puede tener billeteras y movimientos en multiples monedas independientes."""
        u = self._crear_usuario()
        w_ars = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Billetera ARS", moneda="ARS")
        w_usd = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Billetera USD", moneda="USD")
        self.session.add_all([w_ars, w_usd])
        self.session.commit()

        m_ars = MovimientoFinanciero(
            id=uuid.uuid4(), usuario_id=u.id, billetera_id=w_ars.id,
            tipo="egreso", cantidad=Decimal("2000.00"), moneda="ARS",
        )
        m_usd = MovimientoFinanciero(
            id=uuid.uuid4(), usuario_id=u.id, billetera_id=w_usd.id,
            tipo="ingreso", cantidad=Decimal("100.00"), moneda="USD",
        )
        self.session.add_all([m_ars, m_usd])
        self.session.commit()

        self.assertEqual(self.session.query(MovimientoFinanciero).filter_by(moneda="ARS").count(), 1)
        self.assertEqual(self.session.query(MovimientoFinanciero).filter_by(moneda="USD").count(), 1)

    def test_anulados_conservados(self):
        """Movimientos anulados preservan timestamp anulado_en y campos relevantes."""
        u = self._crear_usuario()
        w = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Principal", moneda="ARS")
        self.session.add(w)
        self.session.commit()

        ahora = datetime(2026, 10, 6, 20, 0, 0)
        m = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u.id,
            billetera_id=w.id,
            tipo="egreso",
            cantidad=Decimal("500.00"),
            moneda="ARS",
            anulado_en=ahora,
        )
        self.session.add(m)
        self.session.commit()

        m_db = self.session.query(MovimientoFinanciero).filter_by(id=m.id).one()
        self.assertEqual(m_db.id, m.id)
        self.assertEqual(m_db.anulado_en, ahora)
        self.assertEqual(m_db.billetera_id, w.id)
        self.assertEqual(m_db.usuario_id, u.id)
        self.assertEqual(m_db.moneda, "ARS")
        self.assertEqual(m_db.cantidad, Decimal("500.00"))
        self.assertEqual(m_db.tipo, "egreso")

    def test_misma_moneda_multiples_wallets(self):
        """Multiples billeteras con distinto nombre en la misma moneda para un mismo usuario."""
        u = self._crear_usuario()
        w1 = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Efectivo", moneda="ARS")
        w2 = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Banco", moneda="ARS")
        self.session.add_all([w1, w2])
        self.session.commit()

        wallets_ars = self.session.query(Billetera).filter_by(usuario_id=u.id, moneda="ARS").all()
        self.assertEqual(len(wallets_ars), 2)

    def test_nombre_vacio_o_blancos_rechaza(self):
        """Rechazo por check constraint de nombres vacios o solo espacios."""
        u = self._crear_usuario()
        w_vacio = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="", moneda="ARS")
        self.session.add(w_vacio)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

        w_espacios = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="   ", moneda="ARS")
        self.session.add(w_espacios)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_nombre_duplicado_normalizado_rechaza(self):
        """Rechazo de duplicados bajo lower(trim(nombre)) por usuario y moneda."""
        u = self._crear_usuario()
        w1 = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Efectivo", moneda="ARS")
        self.session.add(w1)
        self.session.commit()

        w2 = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="  efectivo  ", moneda="ARS")
        self.session.add(w2)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

        w_usd = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Efectivo", moneda="USD")
        self.session.add(w_usd)
        self.session.commit()
        self.assertEqual(self.session.query(Billetera).filter_by(usuario_id=u.id, nombre="Efectivo").count(), 2)

    def test_foreign_owner_rechaza(self):
        """Rechazo si el movimiento referencia una billetera de otro usuario (FK compuesta)."""
        u1 = self._crear_usuario("Usuario 1")
        u2 = self._crear_usuario("Usuario 2")
        w_u2 = Billetera(id=uuid.uuid4(), usuario_id=u2.id, nombre="Billetera U2", moneda="ARS")
        self.session.add(w_u2)
        self.session.commit()

        m_invalido = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u1.id,
            billetera_id=w_u2.id,
            tipo="ingreso",
            cantidad=Decimal("100.00"),
            moneda="ARS",
        )
        self.session.add(m_invalido)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_foreign_currency_rechaza(self):
        """Rechazo si la moneda del movimiento difiere de la de la billetera (FK compuesta)."""
        u = self._crear_usuario()
        w_usd = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Caja USD", moneda="USD")
        self.session.add(w_usd)
        self.session.commit()

        m_invalido = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u.id,
            billetera_id=w_usd.id,
            tipo="ingreso",
            cantidad=Decimal("500.00"),
            moneda="ARS",
        )
        self.session.add(m_invalido)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_null_billetera_rechaza(self):
        """Rechazo si billetera_id es nulo (NOT NULL final del contrato)."""
        u = self._crear_usuario()
        m_sin_wallet = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u.id,
            billetera_id=None,
            tipo="ingreso",
            cantidad=Decimal("300.00"),
            moneda="ARS",
        )
        self.session.add(m_sin_wallet)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_decimal_relevante_y_documentacion_limitacion(self):
        """Persistencia de Decimal representable exactamente en SQLite."""
        u = self._crear_usuario()
        w = Billetera(id=uuid.uuid4(), usuario_id=u.id, nombre="Precision", moneda="ARS")
        self.session.add(w)
        self.session.commit()

        cantidad_exacta = Decimal("1234.50")
        m = MovimientoFinanciero(
            id=uuid.uuid4(),
            usuario_id=u.id,
            billetera_id=w.id,
            tipo="ingreso",
            cantidad=cantidad_exacta,
            moneda="ARS",
        )
        self.session.add(m)
        self.session.commit()

        m_db = self.session.query(MovimientoFinanciero).filter_by(id=m.id).one()
        self.assertEqual(m_db.cantidad, cantidad_exacta)


if __name__ == "__main__":
    unittest.main()
