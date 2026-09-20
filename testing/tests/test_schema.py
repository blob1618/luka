"""Tests de `ensure_testing_schema` (testing/services/schema.py).

Verifican que la DB aislada de testing recree el schema cuando los modelos
driftaron (perdiendo datos viejos) y que conserve los datos cuando el schema
ya está al día.
"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.database import Usuario
from testing.services.schema import ensure_testing_schema


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'testing.db'}")


def _column_names(engine, table):
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_rebuilds_schema_when_model_column_missing(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE usuario ("
                "id CHAR(32) PRIMARY KEY, "
                "nombre VARCHAR NOT NULL, "
                "email VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO usuario (id, nombre, email) "
                "VALUES ('1', 'Viejo', 'viejo@example.com')"
            )
        )

    assert ensure_testing_schema(engine) is True

    columns = _column_names(engine, "usuario")
    assert "proactivo_habilitado" in columns
    assert "proactivo_ultimo_envio" in columns

    with Session(engine) as session:
        assert session.query(Usuario).count() == 0
        session.add(
            Usuario(
                nombre="Ana",
                email="ana@example.com",
                whatsapp_id="5491100000000",
            )
        )
        session.commit()
        assert session.query(Usuario).one().whatsapp_id == "5491100000000"


def test_creates_missing_tables_on_empty_db(tmp_path):
    engine = _engine(tmp_path)

    assert ensure_testing_schema(engine) is False

    with Session(engine) as session:
        session.add(Usuario(nombre="Ana", email="ana@example.com"))
        session.commit()
        assert session.query(Usuario).count() == 1


def test_keeps_data_when_schema_matches(tmp_path):
    engine = _engine(tmp_path)
    assert ensure_testing_schema(engine) is False

    with Session(engine) as session:
        session.add(
            Usuario(
                nombre="Ana",
                email="ana@example.com",
                whatsapp_id="5490000000000",
            )
        )
        session.commit()

    assert ensure_testing_schema(engine) is False

    with Session(engine) as session:
        assert session.query(Usuario).one().whatsapp_id == "5490000000000"
