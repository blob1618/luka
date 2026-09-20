-- Recordatorio proactivo diario: opt-out por usuario y fecha del ultimo
-- aviso enviado (maximo uno por dia).
alter table public.usuario
    add column proactivo_habilitado boolean not null default true,
    add column proactivo_ultimo_envio date;
