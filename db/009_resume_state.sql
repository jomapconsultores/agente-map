-- =============================================================================
-- agente_map — migración 009: estado real para reanudar tras una pausa
--
-- Cómo aplicarlo:
--   1. Supabase → SQL Editor → New query
--   2. Pega este archivo y "Run".
--
-- Aditiva e idempotente (add column if not exists).
--
-- Contexto: hasta ahora, pausar un pipeline en curso no persistía el trabajo ya
-- hecho (investigación aprobada, redacción, presupuesto) — "Continuar" volvía a
-- ejecutar todo desde cero. Esta columna registra si Gate 1 (investigación) ya
-- había sido aprobado en el intento persistido, para que al reanudar el pipeline
-- sepa que puede reutilizar `analysis`/`brief` en vez de repetir la búsqueda web.
-- =============================================================================

alter table public.sessions
    add column if not exists research_approved boolean not null default false;
