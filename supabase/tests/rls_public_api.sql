-- Diagnóstico: lista TODAS las tablas malas antes de abortar, para no perder
-- evidencia con el primer RAISE.
SELECT c.relname, c.relrowsecurity,
       has_table_privilege('anon', c.oid, 'SELECT') AS anon_select,
       has_table_privilege('authenticated', c.oid, 'SELECT') AS auth_select
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  AND (
    NOT c.relrowsecurity
    OR has_table_privilege('anon', c.oid, 'SELECT')
    OR has_table_privilege('authenticated', c.oid, 'SELECT')
  )
ORDER BY c.relname;

-- Falla si una tabla de public quedó expuesta a anon/authenticated o sin RLS.
DO $$
DECLARE
  bad record;
BEGIN
  FOR bad IN
    SELECT c.relname,
           c.relrowsecurity,
           has_table_privilege('anon', c.oid, 'SELECT') AS anon_select,
           has_table_privilege('anon', c.oid, 'INSERT') AS anon_insert,
           has_table_privilege('anon', c.oid, 'UPDATE') AS anon_update,
           has_table_privilege('anon', c.oid, 'DELETE') AS anon_delete,
           has_table_privilege('authenticated', c.oid, 'SELECT') AS auth_select,
           has_table_privilege('authenticated', c.oid, 'INSERT') AS auth_insert,
           has_table_privilege('authenticated', c.oid, 'UPDATE') AS auth_update,
           has_table_privilege('authenticated', c.oid, 'DELETE') AS auth_delete
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  LOOP
    IF NOT bad.relrowsecurity
       OR bad.anon_select OR bad.anon_insert OR bad.anon_update OR bad.anon_delete
       OR bad.auth_select OR bad.auth_insert OR bad.auth_update OR bad.auth_delete THEN
      RAISE EXCEPTION
        'public table % is exposed (rls=%, anon select/insert/update/delete=%, auth select/insert/update/delete=%)',
        bad.relname, bad.relrowsecurity,
        bad.anon_select OR bad.anon_insert OR bad.anon_update OR bad.anon_delete,
        bad.auth_select OR bad.auth_insert OR bad.auth_update OR bad.auth_delete;
    END IF;
  END LOOP;
END $$;
