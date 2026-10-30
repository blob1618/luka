"""Deployment metadata and migration drift for /health and /version."""
from __future__ import annotations

import os
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
APPLIED_MIGRATIONS_QUERY = (
    "select version from supabase_migrations.schema_migrations order by version"
)


def service_name() -> str:
    return os.getenv("RENDER_SERVICE_NAME", "").strip() or "luka"


def commit_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "").strip() or "unknown"


def local_migration_versions() -> list[str]:
    """Versions of the migration files shipped with this build."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(path.stem.split("_", 1)[0] for path in MIGRATIONS_DIR.glob("*.sql"))


def applied_migration_versions() -> list[str] | None:
    """Applied versions, or None when the Supabase migrations table is unavailable.

    SQLite/local builds have no `supabase_migrations` schema, so the state is
    reported as unknown instead of failing a liveness check.
    """
    from sqlalchemy import text
    from app.services import finance

    try:
        with finance.SessionLocal() as session:
            rows = session.execute(text(APPLIED_MIGRATIONS_QUERY)).fetchall()
    except Exception:
        return None
    return [str(row[0]) for row in rows]


def migration_status() -> dict:
    local = local_migration_versions()
    applied = applied_migration_versions()
    if applied is None:
        return {"state": "unknown", "local": len(local), "applied": None, "pending": []}
    applied_set = set(applied)
    pending = [version for version in local if version not in applied_set]
    return {
        "state": "pending" if pending else "ok",
        "local": len(local),
        "applied": len(applied),
        "pending": pending,
    }
