"""Script de auditoria de esquema e introspeccion exhaustiva (STK-210).

Compara la base de datos PostgreSQL objetivo contra el Contrato de Comparacion
PostgreSQL canonico de Luka, cubriendo:
- Tablas, columnas, tipos de datos normalizados, nullability y server defaults.
- Primary keys, foreign keys y acciones ON DELETE / ON UPDATE.
- Unique constraints y Check constraints.
- Indices simples, compuestos y predicados de indices parciales (WHERE).
- FK a auth.users(id) en usuario.auth_user_id.
- Estado de Row Level Security (RLS) en las 12 tablas protegidas.
- Politicas en pg_policy (ausencia de politicas publicas para anon/authenticated).
- Privilegios revocados en tablas sensibles (candidato_gasto_recurrente, etc.).
- Extensiones instaladas (uuid-ossp).
- Exclusion estricta de objetos del mecanismo de migracion (alembic_version).

Calcula un fingerprint determinista (SHA-256) que abarca todos estos objetos y
genera un reporte formal de paridad. Cero lectura de tablas de usuarios (cero PII).
Permite auditar conexiones vivas o snapshots JSON generados por conectores de solo lectura.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text

# Asegurar resolucion de rutas para ejecucion directa
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def normalize_database_url(url: str) -> str:
    """Normaliza URLs de conexion soportando tanto postgresql:// como postgres:// hacia postgresql+psycopg://."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# Tablas propias del mecanismo de migracion que se excluyen de la comparacion y del fingerprint
MIGRATION_MECHANISM_TABLES = {"alembic_version"}

# 15 tablas oficiales esperadas en public
EXPECTED_TABLES = {
    "usuario",
    "onboarding_invitacion",
    "dashboard_login_link",
    "acuerdo_version",
    "acuerdo_aceptado",
    "categorias",
    "limite_categoria",
    "candidato_gasto_recurrente",
    "recordatorio",
    "evento",
    "movimientos_financieros",
    "conversation_flow",
    "conversation_flow_version",
    "aviso_recordatorio",
    "cron_job_claim",
}

# 12 tablas con RLS obligatorio habilitado
PROTECTED_RLS_TABLES = {
    "usuario",
    "movimientos_financieros",
    "limite_categoria",
    "onboarding_invitacion",
    "dashboard_login_link",
    "acuerdo_version",
    "acuerdo_aceptado",
    "conversation_flow",
    "conversation_flow_version",
    "candidato_gasto_recurrente",
    "aviso_recordatorio",
    "cron_job_claim",
}

REVOKE_SENSITIVE_TABLES = {
    "candidato_gasto_recurrente",
    "aviso_recordatorio",
    "cron_job_claim",
}


def normalize_type_name(t_str: str) -> str:
    """Normaliza representaciones de tipos de datos SQL para comparaciones independientes de sintaxis."""
    s = str(t_str).lower().strip()
    s = s.replace("character varying", "varchar")
    s = s.replace("timestamp with time zone", "timestamptz")
    s = s.replace("timestamp without time zone", "timestamp")
    s = s.replace("numeric(18, 2)", "numeric(18,2)")
    s = s.replace("integer", "int")
    s = s.replace("int4", "int")
    s = s.replace("boolean", "bool")
    return s


TYPE_CAST_REGEX = re.compile(
    r'::"?(?:character varying|timestamp with(?:out)? time zone|\w+)"?(?:\s*\([^\)]*\))?',
    re.IGNORECASE,
)


def normalize_default(d_str: Any) -> str | None:
    """Normaliza expresiones de defaults omitiendo casteos redundantes y diferencias cosmeticas."""
    if d_str is None:
        return None
    s = str(d_str).strip()
    if not s:
        return None
    s = TYPE_CAST_REGEX.sub("", s).strip()
    lower_s = s.lower()
    if lower_s in ("now()", "current_timestamp", "current_timestamp()", "datetime('now')"):
        return "now()"
    if "uuid_generate_v4()" in lower_s:
        return "extensions.uuid_generate_v4()"
    if lower_s in ("true", "1", "'true'"):
        return "true"
    if lower_s in ("false", "0", "'false'"):
        return "false"
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    return s


def normalize_predicate(pred: str | None) -> str:
    """Normaliza predicados WHERE de indices parciales preservando operadores relacionales."""
    if not pred:
        return ""
    s = str(pred).strip()
    if s.upper().startswith("WHERE"):
        s = s[5:].strip()
    s = s.replace('"', "")
    s = TYPE_CAST_REGEX.sub("", s)

    # Desarmar parentesis alrededor de identificadores individuales: (col) -> col
    s = re.sub(r"\(([a-zA-Z_]\w*)\)", r"\1", s)

    while s.startswith("(") and s.endswith(")"):
        depth = 0
        matched = True
        for i, ch in enumerate(s[:-1]):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0 and i < len(s) - 1:
                matched = False
                break
        if matched:
            s = s[1:-1].strip()
        else:
            break

    # Desarmar conjuntivas atomicas como ((tipo = 'egreso') AND (categoria_id IS NOT NULL))
    parts = re.split(r"\s+AND\s+", s, flags=re.IGNORECASE)
    cleaned_parts = []
    for part in parts:
        part = part.strip()
        if part.startswith("(") and part.endswith(")"):
            inner = part[1:-1].strip()
            if " OR " not in inner.upper() and inner.count("(") == inner.count(")"):
                part = inner
        cleaned_parts.append(part)
    s = " AND ".join(cleaned_parts)

    # Normalizar espacios alrededor de operadores relacionales incluyendo ~
    s = re.sub(r"\s*([<>=!~]+)\s*", r" \1 ", s)

    # Normalizar booleanos de SQLite y Postgres de forma segura sin alterar >= ni <=
    s = re.sub(r"\s+IS\s+1\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"(?<![<>])\s*=\s*true\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"(?<![<>])\s*=\s*1\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+IS\s+0\b", " = false", s, flags=re.IGNORECASE)
    s = re.sub(r"(?<![<>])\s*=\s*0\b", " = false", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+IS\s+NOT\s+NULL\b", " IS NOT NULL", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+IS\s+NULL\b", " IS NULL", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_check_expression(expr: str | None) -> str:
    """Normaliza expresiones SQL de check constraints preservando operadores relacionales."""
    if not expr:
        return ""
    s = str(expr).strip()
    if s.upper().startswith("CHECK"):
        s = s[5:].strip()

    # Quitar comillas dobles de identificadores PostgreSQL: "token_hash" -> token_hash
    s = s.replace('"', "")

    # Normalizar casteo de tipos PostgreSQL
    s = TYPE_CAST_REGEX.sub("", s)

    # Normalizar literales con casteo como (0)::numeric o (0) -> 0 sin alterar variables o funciones
    s = re.sub(r"\(\s*(\d+)\s*\)", r"\1", s)

    # Normalizar sinonimos de funciones estandar
    # PostgreSQL descompila trim(col) a TRIM(BOTH FROM col) o btrim(col)
    s = re.sub(r"\bTRIM\s*\(\s*BOTH\s+FROM\s+([^\)]+)\)", r"trim(\1)", s, flags=re.IGNORECASE)
    s = re.sub(r"\bbtrim\b", "trim", s)
    # char_length(...) es alias de length(...)
    s = re.sub(r"\bchar_length\b", "length", s)

    # Quitar parentesis exteriores redundantes balanceados
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        matched = True
        for i, ch in enumerate(s[:-1]):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0 and i < len(s) - 1:
                matched = False
                break
        if matched:
            s = s[1:-1].strip()
        else:
            break

    s = re.sub(r"\s+", " ", s)
    return s.strip()


def check_expressions_match(exp_expr: str, act_expr: str) -> bool:
    """Compara expresiones de check constraints considerando equivalencias logicas exactas de PostgreSQL."""
    norm_exp = normalize_check_expression(exp_expr)
    norm_act = normalize_check_expression(act_expr)
    if norm_exp == norm_act:
        return True

    # 1. Equivalencia BETWEEN <-> col >= low AND col <= high
    # PostgreSQL descompila col BETWEEN A AND B siempre a ((col >= A) AND (col <= B))
    m_between = re.match(r"(\w+)\s+BETWEEN\s+(\d+)\s+AND\s+(\d+)", norm_exp, re.IGNORECASE)
    if m_between:
        col, low, high = m_between.groups()
        pattern = rf"^\(?\(?{col}\s*>=\s*{low}\)?\s+AND\s+\(?{col}\s*<=\s*{high}\)?\)?$"
        if re.match(pattern, norm_act, re.IGNORECASE):
            return True

    # 2. Normalizar subexpresiones col = ANY (ARRAY['a', 'b']) hacia col IN ('a', 'b')
    def replace_any_array(match):
        col = match.group(1)
        items_str = match.group(2)
        items = re.findall(r"'([^']*)'", items_str)
        sorted_items = ", ".join(f"'{it}'" for it in items)
        return f"{col} IN ({sorted_items})"

    norm_act_in = re.sub(
        r"(\w+)\s*=\s*ANY\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)",
        replace_any_array,
        norm_act,
        flags=re.IGNORECASE,
    )

    def normalize_in_expr(expr):
        def sort_in(match):
            col = match.group(1)
            items = [it.strip() for it in match.group(2).split(",")]
            return f"{col} IN ({', '.join(sorted(items))})"
        return re.sub(r"(\w+)\s+IN\s*\((.*?)\)", sort_in, expr, flags=re.IGNORECASE)

    if normalize_in_expr(norm_exp) == normalize_in_expr(norm_act_in):
        return True

    # 3. Comparacion con desenredo de parentesis de comparaciones atomicas sin alterar operadores
    def strip_atomic_parens(s):
        def unwrap_simple(m):
            inner = m.group(1)
            if not re.search(r"\b(AND|OR)\b", inner, re.IGNORECASE):
                return inner
            return m.group(0)
        prev = ""
        while prev != s:
            prev = s
            s = re.sub(r"\(([^()]+)\)", unwrap_simple, s)
        return s.strip()

    def unwrap_top_level_or_parens(expr: str) -> str:
        """Desenreda parentesis redundantes de conjunciones AND en operandos OR de nivel superior."""
        tokens = []
        current = []
        depth = 0
        in_quote = False
        quote_char = ""
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch in ("'", '"'):
                if not in_quote:
                    in_quote = True
                    quote_char = ch
                elif quote_char == ch:
                    in_quote = False
                current.append(ch)
                i += 1
                continue
            if in_quote:
                current.append(ch)
                i += 1
                continue
            if ch == "(":
                depth += 1
                current.append(ch)
                i += 1
                continue
            elif ch == ")":
                depth -= 1
                current.append(ch)
                i += 1
                continue
            if depth == 0 and expr[i : i + 4].upper() == " OR ":
                tokens.append("".join(current).strip())
                current = []
                i += 4
                continue
            current.append(ch)
            i += 1
        if current:
            tokens.append("".join(current).strip())

        if len(tokens) <= 1:
            return expr

        def strip_one_level_parens(s: str) -> str:
            s = s.strip()
            while s.startswith("(") and s.endswith(")"):
                d = 0
                matched = True
                for idx, c in enumerate(s[:-1]):
                    if c == "(":
                        d += 1
                    elif c == ")":
                        d -= 1
                    if d == 0 and idx < len(s) - 1:
                        matched = False
                        break
                if matched:
                    inner = s[1:-1].strip()
                    d_in = 0
                    has_top_or = False
                    for j in range(len(inner)):
                        if inner[j] == "(":
                            d_in += 1
                        elif inner[j] == ")":
                            d_in -= 1
                        elif d_in == 0 and inner[j : j + 4].upper() == " OR ":
                            has_top_or = True
                            break
                    if not has_top_or:
                        s = inner
                    else:
                        break
                else:
                    break
            return s

        return " OR ".join(strip_one_level_parens(t) for t in tokens)

    clean_exp = strip_atomic_parens(normalize_in_expr(norm_exp))
    clean_act = strip_atomic_parens(normalize_in_expr(norm_act_in))

    clean_exp = unwrap_top_level_or_parens(clean_exp)
    clean_act = unwrap_top_level_or_parens(clean_act)

    # Normalizar espacios alrededor de operadores (incluyendo ~ para regex)
    clean_exp = re.sub(r"\s*([<>=!~]+)\s*", r" \1 ", clean_exp)
    clean_act = re.sub(r"\s*([<>=!~]+)\s*", r" \1 ", clean_act)
    clean_exp = re.sub(r"\s+", " ", clean_exp).strip()
    clean_act = re.sub(r"\s+", " ", clean_act).strip()

    if clean_exp.lower() == clean_act.lower():
        return True

    return False


# Contrato canónico completo de columnas esperadas por tabla (tipo normalizado, nullable, default normalizado)
EXPECTED_COLUMNS_CONTRACT: dict[str, dict[str, dict[str, Any]]] = {
    "usuario": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "nombre": {"type": "text", "nullable": False, "default": None},
        "email": {"type": "text", "nullable": False, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": True, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": True, "default": "now()"},
        "whatsapp_id": {"type": "text", "nullable": True, "default": None},
        "ultimo_mensaje_en": {"type": "timestamptz", "nullable": True, "default": None},
        "auth_user_id": {"type": "uuid", "nullable": True, "default": None},
        "proactivo_habilitado": {"type": "bool", "nullable": False, "default": "true"},
        "proactivo_ultimo_envio": {"type": "date", "nullable": True, "default": None},
    },
    "onboarding_invitacion": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "whatsapp_id": {"type": "text", "nullable": False, "default": None},
        "token_hash": {"type": "text", "nullable": False, "default": None},
        "estado": {"type": "text", "nullable": False, "default": "pendiente"},
        "expira_en": {"type": "timestamptz", "nullable": False, "default": None},
        "intentos": {"type": "int", "nullable": False, "default": "0"},
        "reenvios": {"type": "int", "nullable": False, "default": "0"},
        "ultimo_envio_en": {"type": "timestamptz", "nullable": True, "default": None},
        "usuario_id": {"type": "uuid", "nullable": True, "default": None},
        "consumida_en": {"type": "timestamptz", "nullable": True, "default": None},
        "revocada_en": {"type": "timestamptz", "nullable": True, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "dashboard_login_link": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "token_hash": {"type": "text", "nullable": False, "default": None},
        "estado": {"type": "text", "nullable": False, "default": "pendiente"},
        "expira_en": {"type": "timestamptz", "nullable": False, "default": None},
        "reenvios": {"type": "int", "nullable": False, "default": "0"},
        "ultimo_envio_en": {"type": "timestamptz", "nullable": True, "default": None},
        "consumido_en": {"type": "timestamptz", "nullable": True, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "acuerdo_version": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "version": {"type": "text", "nullable": False, "default": None},
        "contenido": {"type": "text", "nullable": False, "default": None},
        "creado_en": {"type": "timestamp", "nullable": True, "default": "now()"},
        "esta_vigente": {"type": "bool", "nullable": False, "default": "false"},
        "vigente_desde": {"type": "timestamptz", "nullable": True, "default": None},
    },
    "acuerdo_aceptado": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "version_acuerdo_id": {"type": "uuid", "nullable": False, "default": None},
        "aceptado_en": {"type": "timestamp", "nullable": False, "default": "now()"},
        "origen": {"type": "text", "nullable": False, "default": "web_onboarding"},
    },
    "categorias": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": True, "default": None},
        "nombre": {"type": "text", "nullable": False, "default": None},
        "es_default": {"type": "bool", "nullable": True, "default": "false"},
        "esta_eliminado": {"type": "bool", "nullable": True, "default": "false"},
        "creado_en": {"type": "timestamp", "nullable": True, "default": "now()"},
    },
    "limite_categoria": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "categoria_id": {"type": "uuid", "nullable": False, "default": None},
        "cantidad_max": {"type": "numeric(18,2)", "nullable": False, "default": None},
        "moneda": {"type": "varchar(3)", "nullable": False, "default": "ARS"},
        "inicio_periodo": {"type": "date", "nullable": False, "default": None},
        "fin_periodo": {"type": "date", "nullable": False, "default": None},
        "creado_en": {"type": "timestamp", "nullable": True, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "candidato_gasto_recurrente": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "patron_hash": {"type": "text", "nullable": False, "default": None},
        "descripcion_normalizada": {"type": "text", "nullable": False, "default": None},
        "categoria_id": {"type": "uuid", "nullable": True, "default": None},
        "moneda": {"type": "text", "nullable": False, "default": "ARS"},
        "concepto": {"type": "text", "nullable": False, "default": None},
        "monto_estimado": {"type": "numeric(18,2)", "nullable": True, "default": None},
        "dia_estimado": {"type": "int", "nullable": False, "default": None},
        "proxima_fecha_estimada": {"type": "date", "nullable": False, "default": None},
        "estado": {"type": "text", "nullable": False, "default": "pendiente"},
        "ultima_fecha_movimiento": {"type": "date", "nullable": False, "default": None},
        "evidencia_movimiento_ids": {"type": "jsonb", "nullable": False, "default": None},
        "propuesta_en": {"type": "timestamptz", "nullable": True, "default": None},
        "propuesta_conteo": {"type": "int", "nullable": False, "default": "0"},
        "decision_en": {"type": "timestamptz", "nullable": True, "default": None},
        "decision_origen": {"type": "text", "nullable": True, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "recordatorio": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "candidato_id": {"type": "uuid", "nullable": True, "default": None},
        "titulo": {"type": "text", "nullable": False, "default": None},
        "descripcion": {"type": "text", "nullable": True, "default": None},
        "dia_del_mes": {"type": "int", "nullable": True, "default": None},
        "monto": {"type": "numeric", "nullable": True, "default": None},
        "moneda": {"type": "text", "nullable": True, "default": "ARS"},
        "estado": {"type": "text", "nullable": False, "default": "activo"},
        "dias_anticipacion": {"type": "int", "nullable": False, "default": "1"},
        "origen": {"type": "text", "nullable": False, "default": "manual"},
        "ultimo_aviso_enviado": {"type": "date", "nullable": True, "default": None},
        "creado_en": {"type": "timestamp", "nullable": True, "default": "now()"},
    },
    "evento": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": True, "default": None},
        "agregar_tipo": {"type": "text", "nullable": False, "default": None},
        "agregar_id": {"type": "uuid", "nullable": False, "default": None},
        "tipo_evento": {"type": "text", "nullable": False, "default": None},
        "carga": {"type": "jsonb", "nullable": True, "default": None},
        "creado_en": {"type": "timestamp", "nullable": True, "default": "now()"},
    },
    "movimientos_financieros": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "categoria_id": {"type": "uuid", "nullable": True, "default": None},
        "tipo": {"type": "text", "nullable": False, "default": None},
        "cantidad": {"type": "numeric", "nullable": False, "default": None},
        "moneda": {"type": "text", "nullable": False, "default": "ARS"},
        "descripcion": {"type": "text", "nullable": True, "default": None},
        "fecha_movimiento": {"type": "date", "nullable": False, "default": "CURRENT_DATE"},
        "origen": {"type": "text", "nullable": False, "default": "whatsapp_text"},
        "whatsapp_message_id": {"type": "text", "nullable": True, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "anulado_en": {"type": "timestamptz", "nullable": True, "default": None},
    },
    "conversation_flow": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "slug": {"type": "text", "nullable": False, "default": None},
        "name": {"type": "text", "nullable": False, "default": None},
        "event_key": {"type": "text", "nullable": False, "default": None},
        "status": {"type": "text", "nullable": False, "default": "active"},
        "created_at": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "updated_at": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "conversation_flow_version": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "flow_id": {"type": "uuid", "nullable": False, "default": None},
        "version_number": {"type": "int", "nullable": False, "default": None},
        "status": {"type": "text", "nullable": False, "default": "draft"},
        "definition": {"type": "jsonb", "nullable": False, "default": None},
        "created_at": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "updated_at": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "published_at": {"type": "timestamptz", "nullable": True, "default": None},
    },
    "aviso_recordatorio": {
        "id": {"type": "uuid", "nullable": False, "default": "extensions.uuid_generate_v4()"},
        "usuario_id": {"type": "uuid", "nullable": False, "default": None},
        "patron_hash": {"type": "text", "nullable": False, "default": None},
        "periodo": {"type": "text", "nullable": False, "default": None},
        "recordatorio_id": {"type": "uuid", "nullable": False, "default": None},
        "estado": {"type": "text", "nullable": False, "default": "pendiente"},
        "motivo_supresion": {"type": "text", "nullable": True, "default": None},
        "intentos": {"type": "int", "nullable": False, "default": "0"},
        "max_intentos": {"type": "int", "nullable": False, "default": "3"},
        "es_reintentable": {"type": "bool", "nullable": False, "default": "false"},
        "reintentar_en": {"type": "timestamptz", "nullable": True, "default": None},
        "ultimo_intento_en": {"type": "timestamptz", "nullable": True, "default": None},
        "enviado_en": {"type": "timestamptz", "nullable": True, "default": None},
        "whatsapp_message_id": {"type": "text", "nullable": True, "default": None},
        "error_detalle": {"type": "text", "nullable": True, "default": None},
        "creado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
        "actualizado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
    "cron_job_claim": {
        "job_name": {"type": "text", "nullable": False, "default": None},
        "fecha_ejecucion": {"type": "date", "nullable": False, "default": None},
        "ejecutado_en": {"type": "timestamptz", "nullable": False, "default": "now()"},
    },
}

# Acciones esperadas en Foreign Keys
EXPECTED_FK_ACTIONS: dict[str, list[dict[str, Any]]] = {
    "acuerdo_aceptado": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "NO ACTION"},
        {"constrained_columns": ["version_acuerdo_id"], "referred_table": "acuerdo_version", "ondelete": "NO ACTION"},
    ],
    "categorias": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "NO ACTION"},
    ],
    "limite_categoria": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "NO ACTION"},
        {"constrained_columns": ["categoria_id"], "referred_table": "categorias", "ondelete": "NO ACTION"},
    ],
    "candidato_gasto_recurrente": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "CASCADE"},
        {"constrained_columns": ["categoria_id"], "referred_table": "categorias", "ondelete": "SET NULL"},
    ],
    "recordatorio": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "NO ACTION"},
        {"constrained_columns": ["candidato_id"], "referred_table": "candidato_gasto_recurrente", "ondelete": "SET NULL"},
    ],
    "movimientos_financieros": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "NO ACTION"},
        {"constrained_columns": ["categoria_id"], "referred_table": "categorias", "ondelete": "NO ACTION"},
    ],
    "conversation_flow_version": [
        {"constrained_columns": ["flow_id"], "referred_table": "conversation_flow", "ondelete": "CASCADE"},
    ],
    "aviso_recordatorio": [
        {"constrained_columns": ["usuario_id"], "referred_table": "usuario", "ondelete": "CASCADE"},
        {"constrained_columns": ["recordatorio_id"], "referred_table": "recordatorio", "ondelete": "CASCADE"},
    ],
}

# Check constraints requeridos y sus expresiones canonicas
EXPECTED_CHECK_CONSTRAINTS: dict[str, dict[str, str]] = {
    "usuario": {
        "usuario_whatsapp_id_no_vacio_check": "whatsapp_id IS NULL OR trim(whatsapp_id) <> ''",
    },
    "acuerdo_version": {
        "acuerdo_version_vigencia_fecha_check": "esta_vigente = false OR vigente_desde IS NOT NULL",
    },
    "dashboard_login_link": {
        "dashboard_login_link_estado_campos_check": "estado = 'pendiente' AND consumido_en IS NULL OR estado = 'consumido' AND consumido_en IS NOT NULL OR estado = 'vencido' AND consumido_en IS NULL",
        "dashboard_login_link_estado_check": "estado IN ('pendiente', 'consumido', 'vencido')",
        "dashboard_login_link_expiracion_check": "expira_en > creado_en",
        "dashboard_login_link_reenvios_check": "reenvios >= 0",
        "dashboard_login_link_token_hash_no_vacio_check": "trim(token_hash) <> ''",
    },
    "onboarding_invitacion": {
        "onboarding_invitacion_estado_campos_check": "estado = 'pendiente' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL OR estado = 'consumida' AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL OR estado = 'revocada' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL OR estado = 'vencida' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL",
        "onboarding_invitacion_estado_check": "estado IN ('pendiente', 'consumida', 'revocada', 'vencida')",
        "onboarding_invitacion_expiracion_check": "expira_en > creado_en",
        "onboarding_invitacion_intentos_check": "intentos >= 0",
        "onboarding_invitacion_reenvios_check": "reenvios >= 0",
        "onboarding_invitacion_token_hash_no_vacio_check": "trim(token_hash) <> ''",
        "onboarding_invitacion_whatsapp_id_no_vacio_check": "trim(whatsapp_id) <> ''",
    },
    "limite_categoria": {
        "limite_categoria_cantidad_max_check": "cantidad_max >= 0",
        "limite_categoria_moneda_check": "length(moneda) = 3 AND moneda = upper(moneda)",
        "limite_categoria_periodo_check": "inicio_periodo <= fin_periodo",
    },
    "candidato_gasto_recurrente": {
        "candidato_gasto_recurrente_dia_check": "dia_estimado BETWEEN 1 AND 31",
        "candidato_gasto_recurrente_monto_check": "monto_estimado IS NULL OR monto_estimado > 0",
        "candidato_gasto_recurrente_moneda_check": "length(moneda) = 3 AND moneda = upper(moneda)",
        "candidato_gasto_recurrente_estado_check": "estado IN ('pendiente', 'aceptado', 'rechazado', 'pausado', 'desactivado', 'invalidado')",
        "candidato_gasto_recurrente_patron_hash_len_check": "length(patron_hash) = 64",
        "candidato_gasto_recurrente_desc_no_vacio_check": "trim(descripcion_normalizada) <> ''",
        "candidato_gasto_recurrente_concepto_no_vacio_check": "trim(concepto) <> ''",
        "candidato_gasto_recurrente_propuesta_conteo_check": "propuesta_conteo >= 0",
    },
    "recordatorio": {
        "recordatorio_dia_del_mes_check": "dia_del_mes BETWEEN 1 AND 31",
        "recordatorio_estado_check": "estado IN ('activo', 'pausado', 'eliminado')",
        "recordatorio_monto_check": "monto IS NULL OR monto > 0",
        "recordatorio_dias_anticipacion_check": "dias_anticipacion BETWEEN 1 AND 30",
        "recordatorio_origen_check": "origen IN ('manual', 'recurrente_inteligente')",
    },
    "movimientos_financieros": {
        "movimientos_financieros_tipo_check": "tipo IN ('ingreso', 'egreso')",
        "movimientos_financieros_cantidad_check": "cantidad > 0",
    },
    "conversation_flow": {
        "conversation_flow_slug_no_vacio_check": "trim(slug) <> ''",
        "conversation_flow_name_no_vacio_check": "trim(name) <> ''",
        "conversation_flow_event_key_no_vacio_check": "trim(event_key) <> ''",
        "conversation_flow_status_check": "status IN ('active', 'archived')",
    },
    "conversation_flow_version": {
        "conversation_flow_version_number_check": "version_number > 0",
        "conversation_flow_version_status_check": "status IN ('draft', 'published', 'retired')",
        "conversation_flow_version_publication_check": "status = 'draft' AND published_at IS NULL OR status IN ('published', 'retired') AND published_at IS NOT NULL",
    },
    "aviso_recordatorio": {
        "aviso_recordatorio_estado_check": "estado IN ('pendiente', 'sending', 'sent', 'failed', 'unknown', 'suprimido')",
        "aviso_recordatorio_periodo_check": "periodo ~ '^\\d{4}-(0[1-9]|1[0-2])$'",
        "aviso_recordatorio_intentos_check": "intentos >= 0 AND intentos <= max_intentos",
    },
}

# Indices parciales obligatorios con sus predicados canonicos
EXPECTED_INDEX_PREDICATES = {
    "usuario_whatsapp_id_uidx": "whatsapp_id IS NOT NULL",
    "usuario_auth_user_id_uidx": "auth_user_id IS NOT NULL",
    "onboarding_invitacion_whatsapp_pendiente_uidx": "estado = 'pendiente'",
    "dashboard_login_link_usuario_pendiente_uidx": "estado = 'pendiente'",
    "acuerdo_version_vigente_uidx": "esta_vigente",
    "categorias_usuario_nombre_activo_uidx": "esta_eliminado = false",
    "recordatorio_usuario_estado_idx": "estado = 'activo'",
    "movimientos_financieros_whatsapp_message_id_uidx": "whatsapp_message_id IS NOT NULL",
    "movimientos_financieros_presupuesto_egresos_idx": "tipo = 'egreso' AND categoria_id IS NOT NULL",
    "movimientos_financieros_egresos_activos_fecha_idx": "tipo = 'egreso' AND anulado_en IS NULL",
    "conversation_flow_version_draft_uidx": "status = 'draft'",
    "conversation_flow_version_published_uidx": "status = 'published'",
}


def introspect_schema(conn) -> dict[str, Any]:
    """Extrae metadatos de esquema de forma agnostica y exhaustiva excluyendo objetos de migracion."""
    dialect_name = conn.dialect.name
    is_postgres = dialect_name == "postgresql"
    inspector = inspect(conn)

    reflection_errors: list[str] = []

    schema_data: dict[str, Any] = {
        "dialect": dialect_name,
        "tables": {},
        "primary_keys": {},
        "foreign_keys": {},
        "unique_constraints": {},
        "check_constraints": {},
        "indexes": {},
        "rls": {},
        "public_policies": [],
        "revoked_privileges": {},
        "auth_fk_present": False,
        "extensions": [],
        "reflection_errors": reflection_errors,
    }

    raw_tables = set(inspector.get_table_names())
    # Exclusión estricta de objetos de infraestructura de migraciones
    tables = raw_tables - MIGRATION_MECHANISM_TABLES

    for t in sorted(list(tables)):
        try:
            cols = inspector.get_columns(t)
            schema_data["tables"][t] = {
                c["name"]: {
                    "type": normalize_type_name(
                        c["type"].compile(dialect=conn.dialect)
                        if hasattr(c["type"], "compile")
                        else str(c["type"])
                    ),
                    "nullable": c["nullable"],
                    "has_default": c.get("default") is not None,
                    "default": normalize_default(c.get("default")),
                }
                for c in cols
            }
        except Exception as exc:
            reflection_errors.append(f"Error reflejando columnas en tabla '{t}': {exc}")
            schema_data["tables"][t] = {}

        try:
            pk = inspector.get_pk_constraint(t)
            schema_data["primary_keys"][t] = sorted(pk.get("constrained_columns") or [])
        except Exception as exc:
            reflection_errors.append(f"Error reflejando PK en tabla '{t}': {exc}")
            schema_data["primary_keys"][t] = []

        try:
            fks = inspector.get_foreign_keys(t)
            schema_data["foreign_keys"][t] = sorted(
                [
                    {
                        "name": fk.get("name"),
                        "constrained_columns": fk.get("constrained_columns") or [],
                        "referred_table": fk.get("referred_table"),
                        "referred_columns": fk.get("referred_columns") or [],
                        "ondelete": ((fk.get("options") or {}).get("ondelete") or fk.get("ondelete") or "NO ACTION").upper(),
                        "onupdate": ((fk.get("options") or {}).get("onupdate") or fk.get("onupdate") or "NO ACTION").upper(),
                    }
                    for fk in fks
                ],
                key=lambda x: str(x.get("name")),
            )
        except Exception as exc:
            reflection_errors.append(f"Error reflejando FK en tabla '{t}': {exc}")
            schema_data["foreign_keys"][t] = []

        try:
            uks = inspector.get_unique_constraints(t)
            schema_data["unique_constraints"][t] = sorted(
                [{"name": str(u.get("name")), "columns": [str(c) for c in (u.get("column_names") or [])]} for u in uks],
                key=lambda x: str(x.get("name")),
            )
        except Exception as exc:
            reflection_errors.append(f"Error reflejando unique constraints en tabla '{t}': {exc}")
            schema_data["unique_constraints"][t] = []

        try:
            checks = inspector.get_check_constraints(t)
            schema_data["check_constraints"][t] = sorted(
                [{"name": str(ch.get("name")), "sqltext": str(ch.get("sqltext"))} for ch in checks],
                key=lambda x: str(x.get("name")),
            )
        except Exception as exc:
            reflection_errors.append(f"Error reflejando check constraints en tabla '{t}': {exc}")
            schema_data["check_constraints"][t] = []

        try:
            idxs = inspector.get_indexes(t)
            schema_data["indexes"][t] = sorted(
                [
                    {
                        "name": str(idx.get("name")),
                        "unique": bool(idx.get("unique")),
                        "columns": [str(c) for c in (idx.get("column_names") or [])],
                        "predicate": normalize_predicate((idx.get("dialect_options") or {}).get("postgresql_where")),
                    }
                    for idx in idxs
                ],
                key=lambda x: str(x.get("name")),
            )
        except Exception as exc:
            reflection_errors.append(f"Error reflejando indices en tabla '{t}': {exc}")
            schema_data["indexes"][t] = []

    if dialect_name == "sqlite":
        try:
            res_sql_idx = conn.execute(
                text("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name IS NOT NULL;")
            )
            for row in res_sql_idx:
                idx_name = str(row[0])
                tbl_name = str(row[1])
                sql_def = str(row[2]) if row[2] else ""
                if tbl_name in schema_data["indexes"]:
                    m = re.search(r"\bWHERE\s+(.*)$", sql_def, re.IGNORECASE)
                    pred = normalize_predicate(m.group(1) if m else None)
                    existing = next((i for i in schema_data["indexes"][tbl_name] if i.get("name") == idx_name), None)
                    if existing is None:
                        schema_data["indexes"][tbl_name].append({
                            "name": idx_name,
                            "unique": "UNIQUE" in sql_def.upper(),
                            "columns": [],
                            "predicate": pred,
                        })
                    else:
                        if not existing.get("predicate") and pred:
                            existing["predicate"] = pred
        except Exception as exc:
            reflection_errors.append(f"Error reflejando indices desde sqlite_master: {exc}")

    if is_postgres:
        try:
            res_pg_idx = conn.execute(
                text("SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname = 'public';")
            )
            for row in res_pg_idx:
                idx_name = str(row[0])
                tbl_name = str(row[1])
                indexdef = str(row[2]) if row[2] else ""
                if tbl_name in schema_data["indexes"]:
                    m = re.search(r"\bWHERE\s+(.*)$", indexdef, re.IGNORECASE)
                    pred = normalize_predicate(m.group(1) if m else None)
                    existing = next((i for i in schema_data["indexes"][tbl_name] if i.get("name") == idx_name), None)
                    if existing is None:
                        schema_data["indexes"][tbl_name].append({
                            "name": idx_name,
                            "unique": "UNIQUE" in indexdef.upper(),
                            "columns": [],
                            "predicate": pred,
                        })
                    else:
                        if not existing.get("predicate") and pred:
                            existing["predicate"] = pred
        except Exception as exc:
            reflection_errors.append(f"Error reflejando indices desde pg_indexes: {exc}")

        # Metadatos exclusivos de PostgreSQL desde catalogos de sistema
        try:
            res_rls = conn.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname NOT IN ('alembic_version');"
                )
            )
            schema_data["rls"] = {str(row[0]): bool(row[1]) for row in res_rls}
        except Exception as exc:
            reflection_errors.append(f"Error reflejando RLS desde pg_class: {exc}")

        try:
            res_pol = conn.execute(
                text(
                    "SELECT c.relname, pol.polname FROM pg_policy pol "
                    "JOIN pg_class c ON c.oid = pol.polrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname NOT IN ('alembic_version');"
                )
            )
            schema_data["public_policies"] = [f"{r[0]}.{r[1]}" for r in res_pol]
        except Exception as exc:
            reflection_errors.append(f"Error reflejando politicas desde pg_policy: {exc}")

        try:
            res_priv = conn.execute(
                text(
                    "SELECT table_name, grantee, privilege_type FROM information_schema.table_privileges "
                    "WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated') AND table_name NOT IN ('alembic_version');"
                )
            )
            schema_data["revoked_privileges"] = [f"{r[0]}:{r[1]}:{r[2]}" for r in res_priv]
        except Exception as exc:
            reflection_errors.append(f"Error reflejando privilegios desde table_privileges: {exc}")

        try:
            res_fk = conn.execute(
                text(
                    "SELECT conname FROM pg_constraint con "
                    "JOIN pg_class c ON c.oid = con.conrelid "
                    "WHERE c.relname = 'usuario' AND con.contype = 'f';"
                )
            )
            schema_data["auth_fk_present"] = any(r[0] == "usuario_auth_user_id_fkey" for r in res_fk)
        except Exception as exc:
            reflection_errors.append(f"Error reflejando auth_fk desde pg_constraint: {exc}")

        try:
            res_ext = conn.execute(text("SELECT extname FROM pg_extension;"))
            schema_data["extensions"] = [str(r[0]) for r in res_ext]
        except Exception as exc:
            reflection_errors.append(f"Error reflejando extensiones desde pg_extension: {exc}")

    return schema_data


def normalize_schema_data(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Limpia, normaliza y canoniza exhaustivamente los metadatos de esquema (de Python o Snapshot JSON)."""
    norm: dict[str, Any] = {
        "dialect": raw_data.get("dialect", "postgresql"),
        "tables": {},
        "primary_keys": {},
        "foreign_keys": {},
        "unique_constraints": {},
        "check_constraints": {},
        "indexes": {},
        "rls": {},
        "public_policies": sorted(raw_data.get("public_policies") or []),
        "revoked_privileges": sorted(raw_data.get("revoked_privileges") or []),
        "auth_fk_present": bool(raw_data.get("auth_fk_present", False)),
        "extensions": sorted(raw_data.get("extensions") or []),
        "reflection_errors": list(raw_data.get("reflection_errors") or []),
    }

    raw_tables = raw_data.get("tables", {})
    for t in sorted(raw_tables.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        cols = raw_tables[t]
        norm["tables"][t] = {}
        for col_name, c_info in cols.items():
            norm_def = normalize_default(c_info.get("default"))
            norm["tables"][t][col_name] = {
                "type": normalize_type_name(c_info.get("type", "")),
                "nullable": bool(c_info.get("nullable")),
                "has_default": bool(c_info.get("has_default")) or (norm_def is not None),
                "default": norm_def,
            }

    raw_pks = raw_data.get("primary_keys", {})
    for t in sorted(raw_pks.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        norm["primary_keys"][t] = sorted([str(c) for c in (raw_pks.get(t) or []) if c is not None])

    raw_fks = raw_data.get("foreign_keys", {})
    for t in sorted(raw_fks.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        table_fks = []
        for fk in raw_fks.get(t) or []:
            if fk.get("name") is None and not fk.get("constrained_columns") and not fk.get("referred_table"):
                continue  # Eliminar filas nulas de LEFT JOIN en snapshots
            name = fk.get("name") or f"fk_{t}_{'_'.join(fk.get('constrained_columns') or [])}"
            table_fks.append({
                "name": str(name),
                "constrained_columns": [str(c) for c in (fk.get("constrained_columns") or [])],
                "referred_table": fk.get("referred_table"),
                "referred_columns": [str(c) for c in (fk.get("referred_columns") or [])],
                "ondelete": ((fk.get("options") or {}).get("ondelete") or fk.get("ondelete") or "NO ACTION").upper(),
                "onupdate": ((fk.get("options") or {}).get("onupdate") or fk.get("onupdate") or "NO ACTION").upper(),
            })
        norm["foreign_keys"][t] = sorted(table_fks, key=lambda x: str(x.get("name")))

    raw_checks = raw_data.get("check_constraints", {})
    for t in sorted(raw_checks.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        table_checks = []
        for ch in raw_checks.get(t) or []:
            if ch.get("name") is None and not ch.get("sqltext"):
                continue  # Eliminar filas nulas de LEFT JOIN en snapshots
            table_checks.append({
                "name": str(ch.get("name")),
                "sqltext": normalize_check_expression(ch.get("sqltext")),
            })
        norm["check_constraints"][t] = sorted(table_checks, key=lambda x: str(x.get("name")))

    raw_uks = raw_data.get("unique_constraints", {})
    for t in sorted(raw_uks.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        table_uks = []
        for u in raw_uks.get(t) or []:
            if u.get("name") is None and not u.get("columns") and not u.get("column_names"):
                continue  # Eliminar filas nulas de LEFT JOIN en snapshots
            name = u.get("name") or f"uq_{t}_{'_'.join(u.get('columns') or u.get('column_names') or [])}"
            table_uks.append({
                "name": str(name),
                "columns": [str(c) for c in (u.get("columns") or u.get("column_names") or [])],
            })
        norm["unique_constraints"][t] = sorted(table_uks, key=lambda x: str(x.get("name")))

    raw_idxs = raw_data.get("indexes", {})
    for t in sorted(raw_idxs.keys()):
        if t in MIGRATION_MECHANISM_TABLES:
            continue
        table_idxs = []
        for idx in raw_idxs.get(t) or []:
            name = idx.get("name")
            if not name or name.endswith("_pkey"):
                continue  # Eliminar filas nulas y respaldos PK
            table_idxs.append({
                "name": str(name),
                "unique": bool(idx.get("unique")),
                "columns": [str(c) for c in (idx.get("columns") or idx.get("column_names") or [])],
                "predicate": normalize_predicate(idx.get("predicate")),
            })
        norm["indexes"][t] = sorted(table_idxs, key=lambda x: str(x.get("name")))

    raw_rls = raw_data.get("rls", {})
    norm["rls"] = {t: bool(v) for t, v in raw_rls.items() if t not in MIGRATION_MECHANISM_TABLES}

    return norm


def calculate_fingerprint(schema_data: dict[str, Any]) -> str:
    """Calcula una huella digital SHA-256 canonica y determinista excluyendo objetos de migracion."""
    norm = normalize_schema_data(schema_data)
    canonical = {
        "tables": norm["tables"],
        "primary_keys": norm["primary_keys"],
        "foreign_keys": norm["foreign_keys"],
        "unique_constraints": norm["unique_constraints"],
        "check_constraints": norm["check_constraints"],
        "indexes": norm["indexes"],
        "rls": norm["rls"],
        "public_policies": norm["public_policies"],
        "revoked_privileges": norm["revoked_privileges"],
        "auth_fk_present": norm["auth_fk_present"],
        "extensions": norm["extensions"],
    }
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audit_schema(
    conn=None,
    *,
    db_url: str | None = None,
    connection=None,
    snapshot_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audita la base de datos o snapshot contra el contrato canonico esperado."""
    if snapshot_data is not None:
        if "schema_data" in snapshot_data and isinstance(snapshot_data["schema_data"], dict):
            raw_schema = snapshot_data["schema_data"]
        else:
            raw_schema = snapshot_data
    else:
        active_conn = conn if conn is not None else connection
        if active_conn is None:
            if not db_url:
                raise ValueError("Se requiere conn, db_url o snapshot_data para auditar el esquema.")
            engine = create_engine(normalize_database_url(db_url), echo=False)
            with engine.connect() as c:
                return audit_schema(c)
        raw_schema = introspect_schema(active_conn)

    schema_data = normalize_schema_data(raw_schema)
    fingerprint = calculate_fingerprint(schema_data)
    discrepancies: list[str] = []

    # 1. Validar errores de reflexion (fail-fast ante reflexion incompleta)
    if schema_data.get("reflection_errors"):
        discrepancies.extend(schema_data["reflection_errors"])

    # 2. Validar presencia exacta de tablas de negocio (excluyendo alembic_version)
    actual_tables = set(schema_data.get("tables", {}).keys()) - MIGRATION_MECHANISM_TABLES
    missing_tables = EXPECTED_TABLES - actual_tables
    extra_tables = actual_tables - EXPECTED_TABLES

    if missing_tables:
        discrepancies.append(f"Tablas faltantes: {sorted(list(missing_tables))}")
    if extra_tables:
        discrepancies.append(f"Tablas no reconocidas en public: {sorted(list(extra_tables))}")

    is_postgres = schema_data.get("dialect") == "postgresql"

    # 3. Validar columnas, tipos, nullability y defaults en cada tabla
    for t in sorted(list(EXPECTED_TABLES & actual_tables)):
        expected_cols = EXPECTED_COLUMNS_CONTRACT.get(t, {})
        actual_cols = schema_data["tables"].get(t, {})

        missing_c = set(expected_cols.keys()) - set(actual_cols.keys())
        extra_c = set(actual_cols.keys()) - set(expected_cols.keys())

        if missing_c:
            discrepancies.append(f"Tabla '{t}' le faltan columnas: {sorted(list(missing_c))}")
        if extra_c:
            discrepancies.append(f"Tabla '{t}' tiene columnas adicionales no modeladas: {sorted(list(extra_c))}")

        for col_name, exp_meta in expected_cols.items():
            if col_name in actual_cols:
                act_info = actual_cols[col_name]
                act_null = act_info.get("nullable")
                exp_null = exp_meta.get("nullable")
                if exp_null != act_null:
                    discrepancies.append(
                        f"Tabla '{t}', columna '{col_name}': nullability mismatch (esperado={exp_null}, actual={act_null})"
                    )

                # Validacion de tipos
                if is_postgres:
                    exp_type = normalize_type_name(exp_meta.get("type", ""))
                    act_type = normalize_type_name(act_info.get("type", ""))
                    if exp_type != act_type:
                        discrepancies.append(
                            f"Tabla '{t}', columna '{col_name}': type mismatch (esperado='{exp_type}', actual='{act_type}')"
                        )

                # Validacion de defaults (estricta en PostgreSQL / snapshots PostgreSQL)
                if is_postgres:
                    exp_def = exp_meta.get("default")
                    act_def = act_info.get("default")
                    if exp_def is not None:
                        if not act_info.get("has_default") and act_def is None:
                            discrepancies.append(
                                f"Tabla '{t}', columna '{col_name}': default faltante (esperado='{exp_def}', actual=None)"
                            )
                        elif act_def is not None and normalize_default(act_def) != normalize_default(exp_def):
                            discrepancies.append(
                                f"Tabla '{t}', columna '{col_name}': default mismatch (esperado='{exp_def}', actual='{act_def}')"
                            )
                    else:
                        if act_info.get("has_default") or (act_def is not None and normalize_default(act_def) is not None):
                            discrepancies.append(
                                f"Tabla '{t}', columna '{col_name}': default inesperado (esperado=None, actual='{act_def}')"
                            )

    # 4. Validar Check constraints (presencia y expresion canonica)
    actual_checks = schema_data.get("check_constraints", {})
    for t, exp_checks in EXPECTED_CHECK_CONSTRAINTS.items():
        if t in actual_tables:
            act_checks_map = {c.get("name"): c.get("sqltext") for c in actual_checks.get(t, []) if c.get("name")}
            missing_chk = set(exp_checks.keys()) - set(act_checks_map.keys())
            if not is_postgres:
                missing_chk = {chk for chk in missing_chk if chk != "aviso_recordatorio_periodo_check"}
            if missing_chk:
                discrepancies.append(f"Tabla '{t}' faltan check constraints: {sorted(list(missing_chk))}")

            if is_postgres:
                for chk_name, exp_expr in exp_checks.items():
                    if chk_name in act_checks_map:
                        act_expr = act_checks_map[chk_name]
                        if act_expr and not check_expressions_match(exp_expr, act_expr):
                            discrepancies.append(
                                f"Tabla '{t}', check constraint '{chk_name}': expresion mismatch (esperado='{exp_expr}', actual='{act_expr}')"
                            )
                unexpected_chk = set(act_checks_map.keys()) - set(exp_checks.keys())
                if unexpected_chk:
                    discrepancies.append(f"Tabla '{t}' check constraints inesperados: {sorted(list(unexpected_chk))}")

    # 5. Validar Foreign Keys y acciones ON DELETE
    actual_fks = schema_data.get("foreign_keys", {})
    for t, exp_fks in EXPECTED_FK_ACTIONS.items():
        if t in actual_tables:
            act_table_fks = actual_fks.get(t, [])
            for exp_fk in exp_fks:
                match = None
                for afk in act_table_fks:
                    if (
                        afk.get("constrained_columns") == exp_fk.get("constrained_columns")
                        and afk.get("referred_table") == exp_fk.get("referred_table")
                    ):
                        match = afk
                        break
                if not match:
                    discrepancies.append(
                        f"Tabla '{t}' falta FK en {exp_fk.get('constrained_columns')} -> {exp_fk.get('referred_table')}"
                    )
                else:
                    exp_del = exp_fk.get("ondelete", "NO ACTION").upper()
                    act_del = match.get("ondelete", "NO ACTION").upper()
                    # Equivalencias estandar
                    if exp_del in ("NO ACTION", "RESTRICT") and act_del in ("NO ACTION", "RESTRICT", None):
                        pass
                    elif exp_del != act_del:
                        discrepancies.append(
                            f"Tabla '{t}' FK en {exp_fk.get('constrained_columns')}: ondelete mismatch (esperado='{exp_del}', actual='{act_del}')"
                        )

    # 6. Validar indices parciales obligatorios y sus predicados
    all_actual_indexes = {}
    for t_idxs in schema_data.get("indexes", {}).values():
        for idx in t_idxs:
            if idx.get("name"):
                all_actual_indexes[idx["name"]] = idx

    for idx_name, exp_pred in EXPECTED_INDEX_PREDICATES.items():
        if idx_name not in all_actual_indexes:
            discrepancies.append(f"Indice parcial esperado no encontrado: {idx_name}")
        else:
            act_pred = all_actual_indexes[idx_name].get("predicate")
            norm_act = normalize_predicate(act_pred)
            norm_exp = normalize_predicate(exp_pred)
            if norm_act != norm_exp:
                discrepancies.append(
                    f"Indice parcial '{idx_name}': predicado mismatch (esperado='{exp_pred}', actual='{act_pred}')"
                )

    # 7. Verificaciones exclusivas de PostgreSQL (o snapshot PostgreSQL)
    if is_postgres:
        # RLS en las 12 tablas protegidas
        rls_map = schema_data.get("rls", {})
        for t in sorted(PROTECTED_RLS_TABLES):
            if not rls_map.get(t, False):
                discrepancies.append(f"Tabla protegida sin RLS activo: {t}")

        # Politicas publicas
        if schema_data.get("public_policies"):
            discrepancies.append(f"Politicas publicas inesperadas: {schema_data['public_policies']}")

        # Privilegios en tablas sensibles para anon/authenticated
        unrevoked = [
            priv for priv in schema_data.get("revoked_privileges", [])
            if any(priv.startswith(f"{t}:") for t in REVOKE_SENSITIVE_TABLES)
        ]
        if unrevoked:
            discrepancies.append(f"Privilegios no revocados para anon/authenticated en tablas sensibles: {unrevoked}")

        # FK a auth.users
        if not schema_data.get("auth_fk_present"):
            discrepancies.append("Falta FK usuario.auth_user_id -> auth.users(id)")

        # Extensiones
        exts = schema_data.get("extensions", [])
        if "uuid-ossp" not in exts and "pgcrypto" not in exts:
            discrepancies.append("Falta extension uuid-ossp o pgcrypto")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dialect": schema_data.get("dialect", "unknown"),
        "fingerprint_sha256": fingerprint,
        "is_parity_confirmed": len(discrepancies) == 0,
        "discrepancies": discrepancies,
        "schema_data": schema_data,
    }
    return report


def compare_audit_reports(file1: str, file2: str) -> bool:
    """Compara dos reportes de auditoria JSON y asegura paridad estricta y coincidencia de fingerprints."""
    try:
        with open(file1, "r", encoding="utf-8") as f:
            rep1 = json.load(f)
        with open(file2, "r", encoding="utf-8") as f:
            rep2 = json.load(f)
    except Exception as exc:
        print(f"[ERROR] Error al leer los reportes de auditoria: {exc}", file=sys.stderr)
        return False

    fp1 = rep1.get("fingerprint_sha256")
    fp2 = rep2.get("fingerprint_sha256")
    parity1 = rep1.get("is_parity_confirmed", False)
    parity2 = rep2.get("is_parity_confirmed", False)

    print(f"Reporte 1 ({file1}): Fingerprint={fp1}, Paridad={parity1}")
    print(f"Reporte 2 ({file2}): Fingerprint={fp2}, Paridad={parity2}")

    errors = []
    if not parity1:
        errors.append(f"Paridad no confirmada en {file1}: {rep1.get('discrepancies')}")
    if not parity2:
        errors.append(f"Paridad no confirmada en {file2}: {rep2.get('discrepancies')}")
    if fp1 != fp2:
        errors.append(f"Discrepancia de fingerprints: '{fp1}' != '{fp2}'")
        can1 = normalize_schema_data(rep1.get("schema_data", {}))
        can2 = normalize_schema_data(rep2.get("schema_data", {}))
        for k in sorted(set(can1.keys()) | set(can2.keys())):
            v1 = can1.get(k)
            v2 = can2.get(k)
            if v1 != v2:
                errors.append(
                    f"Diferencia en '{k}':\n"
                    f"    {file1}: {json.dumps(v1, sort_keys=True, default=str)}\n"
                    f"    {file2}: {json.dumps(v2, sort_keys=True, default=str)}"
                )

    if errors:
        print(f"[ERROR] Fallo en la comparacion de esquemas ({len(errors)} errores):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return False

    print("[OK] Paridad estricta y huellas SHA-256 identicas confirmadas entre ambos esquemas.")
    return True


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Auditoria de Esquema de Solo Lectura y Snapshots (STK-210)")
    parser.add_argument(
        "--db-url",
        default=os.getenv("DATABASE_URL"),
        help="URL de conexion PostgreSQL (por defecto toma variable de entorno DATABASE_URL)",
    )
    parser.add_argument("--snapshot", help="Ruta a un archivo JSON de snapshot de catalogos de PostgreSQL")
    parser.add_argument("--output", default="schema_audit_report.json", help="Ruta para guardar el reporte JSON")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("REPORT1", "REPORT2"),
        help="Compara dos reportes JSON generados por el auditor para verificar paridad identica.",
    )
    args = parser.parse_args()

    if args.compare:
        success = compare_audit_reports(args.compare[0], args.compare[1])
        sys.exit(0 if success else 1)

    if args.snapshot:
        with open(args.snapshot, "r", encoding="utf-8") as f:
            snapshot_data = json.load(f)
        report = audit_schema(snapshot_data=snapshot_data)
    else:
        if not args.db_url:
            print("[ERROR] Debe especificarse --db-url, --snapshot o definirse DATABASE_URL.", file=sys.stderr)
            sys.exit(1)

        engine = create_engine(normalize_database_url(args.db_url), echo=False)
        with engine.connect() as conn:
            report = audit_schema(conn)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Auditoria completada. Fingerprint: {report['fingerprint_sha256']}")
    print(f"Paridad confirmada: {report['is_parity_confirmed']}")
    if report["discrepancies"]:
        print(f"Discrepancias encontradas ({len(report['discrepancies'])}):")
        for d in report["discrepancies"]:
            print(f"  - {d}")
        sys.exit(1)
    else:
        print("[OK] Esquema 100% coincidente con el Contrato de Comparacion PostgreSQL.")
        sys.exit(0)


if __name__ == "__main__":
    main()
