"""Script de adopcion verificable y nativa de Alembic para bases preexistentes (STK-210).

Implementa la adopcion segura sin blind stamp con guardas estrictas:
1. Respaldo verificado obligatorio (backup_verified=True).
2. Reporte de auditoria valido con paridad confirmada (is_parity_confirmed=True).
3. Fingerprint SHA-256 obligatorio y coincidente con el reporte y la base en vivo.
4. Estado previo de alembic_version conocido (tabla inexistente o vacia).
5. Ejecucion del stamp nativo de Alembic (alembic.command.stamp).
6. Post-check de persistencia verificado.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect, text

# Asegurar resolucion del root del proyecto en sys.path antes de imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from scripts.audit_schema_adoption import audit_schema, normalize_database_url
except ImportError:
    from audit_schema_adoption import audit_schema, normalize_database_url  # type: ignore[no-redef]


def adopt_database(
    db_url: str | None = None,
    connection=None,
    parity_report: dict[str, Any] | None = None,
    expected_fingerprint: str | None = None,
    backup_verified: bool = False,
) -> bool:
    """Ejecuta la adopcion verificable garantizando todas las precondiciones de seguridad."""

    # 1. Guarda de Respaldo Verificado
    if not backup_verified:
        print("[ERROR] Adopcion bloqueada: No se ha verificado formalmente la existencia de un respaldo previo (backup_verified=False).", file=sys.stderr)
        return False

    # 2. Guarda de Reporte de Paridad Valido
    if not parity_report or not parity_report.get("is_parity_confirmed"):
        print("[ERROR] Adopcion bloqueada: Reporte de paridad ausente o con paridad no confirmada (is_parity_confirmed=False).", file=sys.stderr)
        return False

    # 3. Guarda de Fingerprint Obligatorio y Coincidente con el Reporte
    if not expected_fingerprint:
        print("[ERROR] Adopcion bloqueada: El parametro expected_fingerprint es obligatorio.", file=sys.stderr)
        return False

    report_fingerprint = parity_report.get("fingerprint_sha256")
    if expected_fingerprint != report_fingerprint:
        print(f"[ERROR] Adopcion bloqueada: Mismatch entre expected_fingerprint ('{expected_fingerprint}') y el reporte ('{report_fingerprint}').", file=sys.stderr)
        return False

    # Ejecutar adopción sobre connection provista o crear engine con URL normalizada
    if connection is None:
        if not db_url:
            raise ValueError("Se requiere db_url si no se proporciona connection.")
        engine = create_engine(normalize_database_url(db_url), echo=False)
        with engine.connect() as conn:
            return _execute_adoption(
                conn,
                db_url=db_url,
                expected_fingerprint=expected_fingerprint,
            )
    else:
        return _execute_adoption(
            connection,
            db_url=db_url,
            expected_fingerprint=expected_fingerprint,
        )


def _execute_adoption(
    conn,
    db_url: str | None,
    expected_fingerprint: str,
) -> bool:
    # 4. Guarda de Auditoria en Vivo y Fingerprint (Anti-Drift Guard)
    live_audit = audit_schema(conn)
    if not live_audit.get("is_parity_confirmed"):
        print(
            f"[ERROR] Adopcion bloqueada: La auditoria en vivo no confirmo paridad contra el contrato canonico: {live_audit.get('discrepancies')}",
            file=sys.stderr,
        )
        return False

    live_fingerprint = live_audit.get("fingerprint_sha256")
    if live_fingerprint != expected_fingerprint:
        print(
            f"[ERROR] Adopcion bloqueada: Mismatch de fingerprint en vivo. El esquema cambio desde la auditoria (esperado={expected_fingerprint}, en vivo={live_fingerprint}).",
            file=sys.stderr,
        )
        return False

    # 5. Guarda de Estado Previo de alembic_version Conocido
    inspector = inspect(conn)
    tables = inspector.get_table_names()
    if "alembic_version" in tables:
        res = conn.execute(text("SELECT version_num FROM alembic_version;"))
        rows = [row[0] for row in res]
        if rows:
            print(f"[ERROR] Adopcion bloqueada: alembic_version ya contiene versionado previo: {rows}.", file=sys.stderr)
            return False

    print("[INFO] Todas las guardas de seguridad superadas. Ejecutando stamp nativo...")

    # 6. Ejecutar Stamp Nativo de Alembic
    root_dir = Path(__file__).resolve().parent.parent
    config = Config(str(root_dir / "alembic.ini"))
    if db_url:
        config.set_main_option("sqlalchemy.url", normalize_database_url(db_url))
    config.set_main_option("script_location", str(root_dir / "alembic"))
    config.attributes["connection"] = conn

    command.stamp(config, "20260922_0001")
    conn.commit()

    # 7. Post-check de Persistencia
    res = conn.execute(text("SELECT version_num FROM alembic_version;"))
    rows = [row[0] for row in res]
    if rows != ["20260922_0001"]:
        print(f"[ERROR] Post-check fallido: alembic_version contiene {rows}, esperado ['20260922_0001'].", file=sys.stderr)
        return False

    print("[OK] Adopcion completada exitosamente. Revision '20260922_0001' estampada.")
    return True


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Adopcion Verificable de Base Preexistente (STK-210)")
    parser.add_argument(
        "--db-url",
        default=os.getenv("DATABASE_URL"),
        help="URL de conexion PostgreSQL (por defecto toma variable de entorno DATABASE_URL)",
    )
    parser.add_argument("--parity-report", required=True, help="Ruta al archivo JSON de auditoria de paridad previa")
    parser.add_argument("--expected-fingerprint", required=True, help="Huella SHA-256 esperada")
    parser.add_argument("--backup-verified", action="store_true", help="Confirmacion formal de respaldo previo verificado")
    args = parser.parse_args()

    if not args.db_url:
        print("[ERROR] Debe especificarse --db-url o definirse la variable de entorno DATABASE_URL.", file=sys.stderr)
        sys.exit(1)

    with open(args.parity_report, "r", encoding="utf-8") as f:
        parity_data = json.load(f)

    success = adopt_database(
        db_url=args.db_url,
        parity_report=parity_data,
        expected_fingerprint=args.expected_fingerprint,
        backup_verified=args.backup_verified,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
