-- Broaden only the finite set of commands supported by the existing private receipt.
-- Preserve every receipt, owner constraint, transaction and RLS/grant setting.
ALTER TABLE public.assistant_write_receipts
    DROP CONSTRAINT IF EXISTS assistant_write_receipts_command_kind_check;
ALTER TABLE public.assistant_write_receipts
    DROP CONSTRAINT IF EXISTS assistant_receipt_kind_check;
ALTER TABLE public.assistant_write_receipts
    ADD CONSTRAINT assistant_receipt_kind_check
    CHECK (command_kind IN ('register', 'correct_date', 'register_batch', 'update', 'annul'));
