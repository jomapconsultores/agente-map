-- =============================================================================
-- agente_map — migración 012: blindar el backfill de user_module_roles
--
-- Problema: el INSERT de backfill en db/010_module_roles.sql se re-ejecuta cada
-- vez que se re-aplica el conjunto de migraciones (ej. vía /admin/migrate, que
-- concatena y corre TODOS los db/*.sql). Como su WHERE solo mira el estado
-- ACTUAL de user_module_roles (on conflict do nothing), si un admin revoca un
-- módulo a un usuario y luego alguien re-corre las migraciones, ese módulo se
-- le vuelve a otorgar — deshaciendo silenciosamente la revocación.
--
-- Fix: una tabla marcadora de "este backfill ya corrió una vez" hace que el
-- INSERT de 010 (y cualquier futuro backfill equivalente) sea un no-op real en
-- reintentos, sin depender del estado mutable de los grants.
--
-- También corrige que el backfill original no filtraba por status: solo deben
-- quedar con acceso heredado los usuarios ya 'approved' en ese momento.
--
-- Aditiva e idempotente.
-- =============================================================================

create table if not exists public.schema_backfills (
    name        text primary key,
    applied_at  timestamptz not null default now()
);

-- Revoca el acceso que el backfill original le haya dado a cuentas que en ese
-- momento NO estaban aprobadas (pending/rejected) — solo debía alcanzar a
-- usuarios ya operativos.
delete from public.user_module_roles umr
using public.users u
where umr.user_id = u.id
  and u.status <> 'approved'
  and umr.granted_by is null;

insert into public.schema_backfills (name) values ('010_module_roles_backfill')
on conflict (name) do nothing;
