-- Permite que un límite quede en 0 tras una compensación total.
ALTER TABLE public.limite_categoria
    DROP CONSTRAINT limite_categoria_cantidad_max_check;

ALTER TABLE public.limite_categoria
    ADD CONSTRAINT limite_categoria_cantidad_max_check CHECK (cantidad_max >= 0);
