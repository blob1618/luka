-- ADR 0002: el dashboard se sirve por BFF; anon/authenticated no acceden a public.
-- El Worker usa postgres (BYPASSRLS), por lo que el REVOKE no lo afecta.

ALTER TABLE public.categorias ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evento ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recordatorio ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON FUNCTIONS FROM anon, authenticated;
