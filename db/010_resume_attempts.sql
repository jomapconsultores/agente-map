-- =============================================================================
-- agente_map — migración 010: auto-resume tras redeploy + checkpoints de gate
--
-- Cómo aplicarlo:
--   1. Supabase → SQL Editor → New query
--   2. Pega este archivo y "Run".
--
-- Aditiva e idempotente (add column if not exists).
--
-- Contexto:
--  · resume_attempts — cuenta cuántas veces el servidor ha auto-reanudado esta
--    sesión al arrancar (ver api.main lifespan / db.repository.list_recently_
--    interrupted). Corta el bucle redeploy→resume→redeploy: pasado un tope, la
--    sesión deja de reanimarse sola.
--  · gate2_passed / gate3_passed — checkpoints de grano fino. Cuando una sesión
--    muere DESPUÉS de aprobar Gate 2 (redacción) o Gate 3 (paquete) y se reanuda,
--    estos flags evitan repetir la redacción multipasada y los gates ya superados.
--    Se invalidan si Fase 4 rechaza y fuerza reinicio (el borrador deja de valer).
-- =============================================================================

alter table public.sessions
    add column if not exists resume_attempts integer not null default 0;

alter table public.sessions
    add column if not exists gate2_passed boolean not null default false;

alter table public.sessions
    add column if not exists gate3_passed boolean not null default false;
