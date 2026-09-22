-- ============================================================================
-- Luka — Export de Snapshot de Metadatos de Solo Lectura (STK-210)
-- ============================================================================
-- Este script realiza introspección EXCLUSIVA de catálogos del sistema
-- (pg_catalog e information_schema). NO accede a datos de negocio ni lee PII.
--
-- Ejecución:
-- 1. Ejecutar en Supabase SQL Editor o con un cliente psql con rol de solo lectura.
-- 2. Copiar el resultado JSON y guardarlo en 'supabase_schema_snapshot.json'.
-- 3. Auditar localmente sin credenciales:
--    python scripts/audit_schema_adoption.py --snapshot supabase_schema_snapshot.json
-- ============================================================================

WITH
-- Tablas del esquema public (excluyendo alembic_version)
target_tables AS (
    SELECT c.oid, c.relname AS table_name, c.relrowsecurity AS rls_enabled
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND c.relname NOT IN ('alembic_version')
),

-- Columnas con tipos, nullability y defaults
table_columns AS (
    SELECT
        t.table_name,
        jsonb_object_agg(
            a.attname,
            jsonb_build_object(
                'type', format_type(a.atttypid, a.atttypmod),
                'nullable', NOT a.attnotnull,
                'has_default', (d.adbin IS NOT NULL),
                'default', pg_get_expr(d.adbin, d.adrelid)
            )
            ORDER BY a.attnum
        ) AS columns_json
    FROM target_tables t
    JOIN pg_attribute a ON a.attrelid = t.oid
    LEFT JOIN pg_attrdef d ON d.adrelid = t.oid AND d.adnum = a.attnum
    WHERE a.attnum > 0 AND NOT a.attisdropped
    GROUP BY t.table_name
),

-- Primary keys
table_pks AS (
    SELECT
        t.table_name,
        coalesce(
            jsonb_agg(a.attname ORDER BY array_position(con.conkey, a.attnum)) FILTER (WHERE a.attname IS NOT NULL),
            '[]'::jsonb
        ) AS pk_cols
    FROM target_tables t
    LEFT JOIN pg_constraint con ON con.conrelid = t.oid AND con.contype = 'p'
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(con.conkey)
    GROUP BY t.table_name
),

-- Foreign keys con acciones ON DELETE / ON UPDATE
table_fks AS (
    SELECT
        t.table_name,
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'name', con.conname,
                    'constrained_columns', (
                        SELECT jsonb_agg(ca.attname ORDER BY pos.idx)
                        FROM unnest(con.conkey) WITH ORDINALITY AS pos(attnum, idx)
                        JOIN pg_attribute ca ON ca.attrelid = t.oid AND ca.attnum = pos.attnum
                    ),
                    'referred_table', f_class.relname,
                    'referred_columns', (
                        SELECT jsonb_agg(fa.attname ORDER BY fpos.idx)
                        FROM unnest(con.confkey) WITH ORDINALITY AS fpos(attnum, idx)
                        JOIN pg_attribute fa ON fa.attrelid = f_class.oid AND fa.attnum = fpos.attnum
                    ),
                    'ondelete', CASE con.confdeltype
                        WHEN 'a' THEN 'NO ACTION'
                        WHEN 'r' THEN 'RESTRICT'
                        WHEN 'c' THEN 'CASCADE'
                        WHEN 'n' THEN 'SET NULL'
                        WHEN 'd' THEN 'SET DEFAULT'
                        ELSE 'NO ACTION'
                    END,
                    'onupdate', CASE con.confupdtype
                        WHEN 'a' THEN 'NO ACTION'
                        WHEN 'r' THEN 'RESTRICT'
                        WHEN 'c' THEN 'CASCADE'
                        WHEN 'n' THEN 'SET NULL'
                        WHEN 'd' THEN 'SET DEFAULT'
                        ELSE 'NO ACTION'
                    END
                )
                ORDER BY con.conname
            ) FILTER (WHERE con.conname IS NOT NULL),
            '[]'::jsonb
        ) AS fks_json
    FROM target_tables t
    LEFT JOIN pg_constraint con ON con.conrelid = t.oid AND con.contype = 'f'
    LEFT JOIN pg_class f_class ON f_class.oid = con.confrelid
    GROUP BY t.table_name
),

-- Check constraints
table_checks AS (
    SELECT
        t.table_name,
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'name', con.conname,
                    'sqltext', pg_get_constraintdef(con.oid)
                )
                ORDER BY con.conname
            ) FILTER (WHERE con.conname IS NOT NULL),
            '[]'::jsonb
        ) AS checks_json
    FROM target_tables t
    LEFT JOIN pg_constraint con ON con.conrelid = t.oid AND con.contype = 'c'
    GROUP BY t.table_name
),

-- Unique constraints
table_uniques AS (
    SELECT
        t.table_name,
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'name', con.conname,
                    'columns', (
                        SELECT jsonb_agg(ca.attname ORDER BY pos.idx)
                        FROM unnest(con.conkey) WITH ORDINALITY AS pos(attnum, idx)
                        JOIN pg_attribute ca ON ca.attrelid = t.oid AND ca.attnum = pos.attnum
                    )
                )
                ORDER BY con.conname
            ) FILTER (WHERE con.conname IS NOT NULL),
            '[]'::jsonb
        ) AS uniques_json
    FROM target_tables t
    LEFT JOIN pg_constraint con ON con.conrelid = t.oid AND con.contype = 'u'
    GROUP BY t.table_name
),

-- Índices y predicados parciales
table_indexes AS (
    SELECT
        t.table_name,
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'name', i_class.relname,
                    'unique', i.indisunique,
                    'columns', (
                        SELECT jsonb_agg(coalesce(ia.attname, pg_get_indexdef(i.indexrelid, pos.idx::int, false)) ORDER BY pos.idx)
                        FROM unnest(i.indkey) WITH ORDINALITY AS pos(attnum, idx)
                        LEFT JOIN pg_attribute ia ON ia.attrelid = t.oid AND ia.attnum = pos.attnum
                    ),
                    'predicate', pg_get_expr(i.indpred, i.indrelid)
                )
                ORDER BY i_class.relname
            ) FILTER (WHERE i_class.relname IS NOT NULL),
            '[]'::jsonb
        ) AS indexes_json
    FROM target_tables t
    LEFT JOIN pg_index i ON i.indrelid = t.oid AND NOT i.indisprimary
    LEFT JOIN pg_class i_class ON i_class.oid = i.indexrelid
    GROUP BY t.table_name
),

-- RLS por tabla
table_rls AS (
    SELECT jsonb_object_agg(table_name, rls_enabled ORDER BY table_name) AS rls_json
    FROM target_tables
),

-- Políticas públicas
public_policies AS (
    SELECT coalesce(jsonb_agg(c.relname || '.' || pol.polname ORDER BY c.relname, pol.polname), '[]'::jsonb) AS pol_json
    FROM pg_policy pol
    JOIN pg_class c ON c.oid = pol.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname NOT IN ('alembic_version')
),

-- Privilegios para anon/authenticated
sensitive_privileges AS (
    SELECT coalesce(jsonb_agg(table_name || ':' || grantee || ':' || privilege_type ORDER BY table_name, grantee, privilege_type), '[]'::jsonb) AS priv_json
    FROM information_schema.table_privileges
    WHERE table_schema = 'public'
      AND grantee IN ('anon', 'authenticated')
      AND table_name NOT IN ('alembic_version')
),

-- FK usuario.auth_user_id -> auth.users(id)
auth_fk AS (
    SELECT (count(*) > 0) AS auth_fk_present
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    WHERE c.relname = 'usuario' AND con.conname = 'usuario_auth_user_id_fkey'
),

-- Extensiones instaladas
installed_extensions AS (
    SELECT coalesce(jsonb_agg(extname ORDER BY extname), '[]'::jsonb) AS ext_json
    FROM pg_extension
)

-- Ensamblado final del documento de metadatos
SELECT jsonb_pretty(
    jsonb_build_object(
        'version', '1.0',
        'dialect', 'postgresql',
        'timestamp', now(),
        'tables', (SELECT jsonb_object_agg(table_name, columns_json ORDER BY table_name) FROM table_columns),
        'primary_keys', (SELECT jsonb_object_agg(table_name, pk_cols ORDER BY table_name) FROM table_pks),
        'foreign_keys', (SELECT jsonb_object_agg(table_name, fks_json ORDER BY table_name) FROM table_fks),
        'check_constraints', (SELECT jsonb_object_agg(table_name, checks_json ORDER BY table_name) FROM table_checks),
        'unique_constraints', (SELECT jsonb_object_agg(table_name, uniques_json ORDER BY table_name) FROM table_uniques),
        'indexes', (SELECT jsonb_object_agg(table_name, indexes_json ORDER BY table_name) FROM table_indexes),
        'rls', (SELECT rls_json FROM table_rls),
        'public_policies', (SELECT pol_json FROM public_policies),
        'revoked_privileges', (SELECT priv_json FROM sensitive_privileges),
        'auth_fk_present', (SELECT auth_fk_present FROM auth_fk),
        'extensions', (SELECT ext_json FROM installed_extensions),
        'reflection_errors', '[]'::jsonb
    )
) AS schema_snapshot_json;
