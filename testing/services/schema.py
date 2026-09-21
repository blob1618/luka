"""Schema drift repair for the isolated testing SQLite database."""

import logging

from sqlalchemy import inspect

from app.models.database import Base

logger = logging.getLogger(__name__)


def ensure_testing_schema(engine) -> bool:
    """Create missing tables and rebuild the schema when the models drifted.

    # ponytail: detecta solo columnas faltantes; migraciones reales viven en supabase/migrations
    Returns True when the schema was rebuilt (existing data discarded).
    """
    inspector = inspect(engine)
    has_drift = any(
        {column.name for column in table.columns}
        - {column["name"] for column in inspector.get_columns(table.name)}
        for table in Base.metadata.sorted_tables
        if inspector.has_table(table.name)
    )
    if has_drift:
        logger.warning("Schema de testing desactualizado: se recrea la base aislada")
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return has_drift
