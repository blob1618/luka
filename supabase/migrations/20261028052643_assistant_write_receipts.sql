-- STK-260 pilot: mutation and result receipt are committed in one backend transaction.
CREATE TABLE public.assistant_write_receipts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid NOT NULL REFERENCES public.usuario(id),
  operation_id varchar(64) NOT NULL,
  command_hash varchar(64) NOT NULL,
  source_message_id text NOT NULL,
  confirmation_message_id text NOT NULL,
  movement_id uuid NOT NULL REFERENCES public.movimientos_financieros(id),
  command_kind varchar(32) NOT NULL CHECK (command_kind IN ('register','correct_date')),
  result jsonb NOT NULL,
  committed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT assistant_receipt_operation_key UNIQUE(owner_id, operation_id),
  CONSTRAINT assistant_receipt_confirmation_key UNIQUE(owner_id, confirmation_message_id)
);
ALTER TABLE public.assistant_write_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.assistant_write_receipts FROM PUBLIC, anon, authenticated;
-- Deployment requires an owner/BYPASSRLS backend role or a reviewed server-only policy.
-- The effective production database role was not inspected by this migration.
GRANT SELECT, INSERT ON public.assistant_write_receipts TO service_role;
-- No anon/authenticated policies: neither role may read or mutate financial receipts.
