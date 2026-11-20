CREATE TABLE public.sepa_snapshot (
    id uuid PRIMARY KEY,
    data_date date NOT NULL UNIQUE,
    source_url text NOT NULL,
    status text NOT NULL DEFAULT 'loading' CHECK (status IN ('loading', 'ready', 'failed')),
    row_count integer NOT NULL DEFAULT 0,
    rejected_count integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    loaded_at timestamptz
);
CREATE INDEX sepa_snapshot_ready_date_idx ON public.sepa_snapshot (status, data_date);

CREATE TABLE public.sepa_price (
    snapshot_id uuid NOT NULL REFERENCES public.sepa_snapshot(id) ON DELETE CASCADE,
    commerce_id text NOT NULL,
    flag_id text NOT NULL,
    branch_id text NOT NULL,
    product_id text NOT NULL,
    report_date date NOT NULL,
    is_ean boolean NOT NULL,
    description text NOT NULL,
    brand text,
    quantity numeric(18,4),
    unit text,
    chain text NOT NULL,
    branch text NOT NULL,
    province text,
    locality text,
    latitude numeric(10,6),
    longitude numeric(10,6),
    list_price numeric(18,2) NOT NULL CHECK (list_price > 0),
    promo1_price numeric(18,2),
    promo1_text text,
    promo2_price numeric(18,2),
    promo2_text text,
    PRIMARY KEY (snapshot_id, commerce_id, flag_id, branch_id, product_id)
);
CREATE INDEX sepa_price_ean_idx ON public.sepa_price (snapshot_id, product_id, is_ean);
CREATE INDEX sepa_price_description_idx ON public.sepa_price (snapshot_id, description);
CREATE INDEX sepa_price_description_fts_idx ON public.sepa_price USING gin (to_tsvector('spanish', description));
CREATE INDEX sepa_price_zone_idx ON public.sepa_price (snapshot_id, province, locality);

ALTER TABLE public.sepa_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sepa_price ENABLE ROW LEVEL SECURITY;
