-- Keep the original WhatsApp message ID for deduplication while excluding
-- mistaken movements from financial views and budget calculations.
alter table public.movimientos_financieros
    add column anulado_en timestamptz;
