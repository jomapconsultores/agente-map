-- =============================================================================
-- agente_map — migración 011: rol "trabajador"
--
-- Tercer valor de public.users.role, junto a 'admin' y 'user'. Un trabajador
-- es una cuenta creada DIRECTAMENTE por el administrador (no por auto-registro
-- público) y aprobada de inmediato; sus permisos por módulo los asigna el
-- administrador vía user_module_roles (mismo mecanismo que ya usa 'user' —
-- ver db/010_module_roles.sql). El administrador mantiene control total:
-- crea la cuenta, y otorga/revoca los módulos que considere pertinentes.
--
-- Aditiva e idempotente.
-- =============================================================================

alter table public.users drop constraint if exists users_role_check;
alter table public.users add constraint users_role_check
    check (role in ('admin', 'user', 'trabajador'));
