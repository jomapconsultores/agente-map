-- =============================================================================
-- agente_map — migración 010: roles por módulo (multi-rol por usuario)
--
-- Un usuario puede tener acceso a varios módulos (captación, clientes,
-- investigación, proyectos, oficios) a la vez. Solo el administrador otorga
-- o revoca estos accesos (vía /users/{id}/roles). El rol admin/user de
-- public.users se mantiene aparte: un admin tiene acceso implícito a todo.
--
-- Backfill: para no romper el acceso de usuarios ya aprobados que hoy operan
-- sin esta tabla (usaban todos los módulos libremente), se les concede acceso
-- a los 5 módulos al aplicar esta migración. Los usuarios NUEVOS a partir de
-- ahora empiezan sin ningún módulo asignado (denegar por defecto) hasta que
-- el administrador se los otorgue explícitamente.
--
-- El backfill corre UNA SOLA VEZ, marcado en schema_backfills: sin esto, cada
-- vez que se re-aplican las migraciones (ej. /admin/migrate, que concatena y
-- corre TODOS los db/*.sql) el INSERT se repite y deshace silenciosamente
-- cualquier revocación de módulo que un admin haya hecho desde entonces.
--
-- Aditiva e idempotente.
-- =============================================================================

create table if not exists public.user_module_roles (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references public.users(id) on delete cascade,
    module       text not null check (module in
                    ('captacion','clientes','investigacion','proyectos','oficios')),
    granted_by   uuid references public.users(id) on delete set null,
    created_at   timestamptz not null default now(),
    unique (user_id, module)
);

create index if not exists umr_user_idx on public.user_module_roles (user_id);

alter table public.user_module_roles enable row level security;
-- Sin políticas: solo service_role (el backend) puede leer/escribir.

create table if not exists public.schema_backfills (
    name        text primary key,
    applied_at  timestamptz not null default now()
);

insert into public.user_module_roles (user_id, module, granted_by)
select u.id, m.module, null
from public.users u
cross join (values ('captacion'),('clientes'),('investigacion'),('proyectos'),('oficios')) as m(module)
where u.role <> 'admin'
  and u.status = 'approved'
  and not exists (select 1 from public.schema_backfills where name = '010_module_roles_backfill')
on conflict (user_id, module) do nothing;

insert into public.schema_backfills (name) values ('010_module_roles_backfill')
on conflict (name) do nothing;
