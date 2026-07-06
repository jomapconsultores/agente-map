-- =============================================================================
-- agente_map — ESQUEMA COMPLETO (proyecto Supabase nuevo / vacío)
--
-- Cómo aplicarlo:
--   1. Supabase → tu proyecto → SQL Editor → New query
--   2. Pega TODO este archivo y "Run".
--
-- Incluye: sessions (+status, url, owner), proposal_versions, reviews, users,
-- user_module_roles (roles por módulo, incl. 'trabajador').
--
-- IMPORTANTE: este snapshot NO incluye las migraciones de webauthn (006),
-- oficios (007), captación (008) ni resume_state (009) — para un proyecto
-- nuevo, después de correr este archivo aplica también esas, o usa
-- POST /admin/migrate (que concatena y corre TODAS las db/NNN_*.sql reales).
--
-- RLS activado sin políticas → solo la secret key (service_role) accede.
-- Idempotente: se puede re-ejecutar sin romper nada.
-- =============================================================================

create extension if not exists "pgcrypto";

-- ── users (auth propia: hash PBKDF2 + aprobación) ───────────────────────────
create table if not exists public.users (
    id             uuid primary key default gen_random_uuid(),
    email          text not null unique,
    name           text not null default '',
    password_hash  text not null,
    password_salt  text not null,
    role           text not null default 'user'   check (role in ('admin','user','trabajador')),
    status         text not null default 'pending' check (status in ('pending','approved','rejected')),
    created_at     timestamptz not null default now(),
    last_login_at  timestamptz
);
create index if not exists users_email_idx  on public.users (email);
create index if not exists users_status_idx on public.users (status);

-- ── user_module_roles (multi-rol por módulo, otorgado solo por el admin) ────
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

-- ── sessions ────────────────────────────────────────────────────────────────
create table if not exists public.sessions (
    id             uuid primary key default gen_random_uuid(),
    session_id     text not null unique,
    user_input     text not null,
    input_mode     text not null check (input_mode in ('search','text','file','url')),
    doc_type_key   text not null,
    language       text not null default 'es',

    status         text not null default 'pending'
                     check (status in ('pending','running','approved','failed')),
    approved       boolean not null default false,
    current_cycle  int not null default 0,
    error_message  text,

    owner_user_id  uuid references public.users(id) on delete set null,

    output_path    text,
    word_path      text,
    excel_path     text,

    template_text  text,
    support_docs   jsonb not null default '[]'::jsonb,

    analysis       jsonb,
    brief          jsonb,
    financial      jsonb,

    started_at     timestamptz,
    completed_at   timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index if not exists sessions_created_at_idx on public.sessions (created_at desc);
create index if not exists sessions_doc_type_idx   on public.sessions (doc_type_key);
create index if not exists sessions_status_idx     on public.sessions (status);
create index if not exists sessions_owner_idx      on public.sessions (owner_user_id);

-- ── proposal_versions (borrador por ciclo) ──────────────────────────────────
create table if not exists public.proposal_versions (
    id          uuid primary key default gen_random_uuid(),
    session_id  uuid not null references public.sessions(id) on delete cascade,
    cycle       int  not null,
    content     text not null,
    char_count  int  generated always as (length(content)) stored,
    created_at  timestamptz not null default now(),
    unique (session_id, cycle)
);
create index if not exists proposal_versions_session_idx on public.proposal_versions (session_id);

-- ── reviews (verificación 90/90 por ciclo) ──────────────────────────────────
create table if not exists public.reviews (
    id                    uuid primary key default gen_random_uuid(),
    session_id            uuid not null references public.sessions(id) on delete cascade,
    cycle                 int  not null,
    approved              boolean not null,
    overall_score         numeric(5,2) not null,
    criterion_scores      jsonb not null default '{}'::jsonb,
    format_check          jsonb not null default '{}'::jsonb,
    strengths             jsonb not null default '[]'::jsonb,
    corrections           jsonb not null default '[]'::jsonb,
    critical_issues       jsonb not null default '[]'::jsonb,
    compliance_checklist  jsonb not null default '[]'::jsonb,
    failing_elements      jsonb not null default '[]'::jsonb,
    recommendation        text,
    created_at            timestamptz not null default now(),
    unique (session_id, cycle)
);
create index if not exists reviews_session_idx on public.reviews (session_id);

-- ── trigger: updated_at automático ──────────────────────────────────────────
create or replace function public._touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists sessions_touch_updated_at on public.sessions;
create trigger sessions_touch_updated_at
    before update on public.sessions
    for each row execute function public._touch_updated_at();

-- ── RLS: solo service_role (la secret key del backend) ──────────────────────
alter table public.users             enable row level security;
alter table public.sessions          enable row level security;
alter table public.proposal_versions enable row level security;
alter table public.reviews           enable row level security;
-- Sin políticas: el backend usa la secret key (service_role), que salta RLS.
