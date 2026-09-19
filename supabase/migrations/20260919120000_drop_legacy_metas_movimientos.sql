-- Retira tablas heredadas que no forman parte del contrato vigente de Luka.
-- Ambas fueron verificadas vacias en el entorno remoto antes de versionar este cambio.

DROP TABLE IF EXISTS public.metas;
DROP TABLE IF EXISTS public.movimientos;
