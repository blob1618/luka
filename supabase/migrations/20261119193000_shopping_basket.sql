CREATE TABLE public.shopping_basket (
    usuario_id uuid PRIMARY KEY REFERENCES public.usuario(id) ON DELETE CASCADE,
    locality text NOT NULL,
    province text
);

CREATE TABLE public.shopping_basket_item (
    usuario_id uuid NOT NULL REFERENCES public.shopping_basket(usuario_id) ON DELETE CASCADE,
    ean varchar(13) NOT NULL,
    product_name text NOT NULL,
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (usuario_id, ean)
);

ALTER TABLE public.shopping_basket ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shopping_basket_item ENABLE ROW LEVEL SECURITY;
