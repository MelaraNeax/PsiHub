-- PsiHub · esquema base para Supabase (Postgres + Auth + RLS)
-- Ejecutar en Supabase → SQL Editor → New query. Es idempotente.
--
-- La app funciona hoy sin cuenta (localStorage). Este esquema deja lista la
-- version multiusuario: Google OAuth crea la fila en auth.users y el trigger
-- arma el perfil y la carpeta "Guardados". Cada tabla queda aislada por RLS:
-- un usuario solo puede leer y escribir sus propias filas.

create extension if not exists pgcrypto;

-- ─── Perfiles ───────────────────────────────────────────────────────────────
create table if not exists public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text,
  name        text,
  avatar_url  text,
  plan        text not null default 'basic' check (plan in ('basic', 'pro')),
  role        text not null default 'student'
              check (role in ('student', 'researcher', 'professional', 'admin')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.profiles enable row level security;

drop policy if exists "profiles: leer el propio" on public.profiles;
create policy "profiles: leer el propio" on public.profiles
  for select using (auth.uid() = id);

drop policy if exists "profiles: editar el propio" on public.profiles;
create policy "profiles: editar el propio" on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);

drop policy if exists "profiles: crear el propio" on public.profiles;
create policy "profiles: crear el propio" on public.profiles
  for insert with check (auth.uid() = id);

-- ─── Carpetas de guardados ──────────────────────────────────────────────────
create table if not exists public.folders (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  name        text not null check (char_length(trim(name)) between 1 and 60),
  position    int not null default 0,
  created_at  timestamptz not null default now(),
  unique (user_id, name)
);

create index if not exists folders_user_idx on public.folders(user_id, position);

alter table public.folders enable row level security;

drop policy if exists "folders: propias" on public.folders;
create policy "folders: propias" on public.folders
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─── Papers guardados ──────────────────────────────────────────────────────
create table if not exists public.saved_papers (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  folder_id   uuid references public.folders(id) on delete set null,
  paper_id    text not null,
  doi         text,
  pmcid       text,
  title       text,
  authors     jsonb not null default '[]'::jsonb,
  year        int,
  journal     text,
  abstract    text,
  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, paper_id)
);

create index if not exists saved_user_idx on public.saved_papers(user_id, created_at desc);
create index if not exists saved_folder_idx on public.saved_papers(folder_id);

alter table public.saved_papers enable row level security;

drop policy if exists "saved: propios" on public.saved_papers;
create policy "saved: propios" on public.saved_papers
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─── Historial de chat ─────────────────────────────────────────────────────
create table if not exists public.chats (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  title       text not null default 'Nueva investigación',
  depth       text not null default 'medium',
  plan        text not null default 'basic',
  messages    jsonb not null default '[]'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists chats_user_idx on public.chats(user_id, updated_at desc);

alter table public.chats enable row level security;

drop policy if exists "chats: propios" on public.chats;
create policy "chats: propios" on public.chats
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─── Resumenes y traducciones (estado visible en Mis archivos) ─────────────
create table if not exists public.summaries (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  paper_id    text not null,
  mode        text not null default 'basic' check (mode in ('basic', 'advanced')),
  content     text not null,
  cost_usd    numeric(12, 6) not null default 0,
  created_at  timestamptz not null default now(),
  unique (user_id, paper_id, mode)
);

alter table public.summaries enable row level security;

drop policy if exists "summaries: propios" on public.summaries;
create policy "summaries: propios" on public.summaries
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create table if not exists public.translations (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  paper_id    text not null,
  document_id text,
  created_at  timestamptz not null default now(),
  unique (user_id, paper_id)
);

alter table public.translations enable row level security;

drop policy if exists "translations: propias" on public.translations;
create policy "translations: propias" on public.translations
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─── Uso y costo (espejo consultable del ledger de Cloud Logging) ──────────
create table if not exists public.usage_events (
  id                  bigserial primary key,
  user_id             uuid references auth.users(id) on delete set null,
  kind                text not null,
  model               text,
  prompt_tokens       int not null default 0,
  cache_hit_tokens    int not null default 0,
  completion_tokens   int not null default 0,
  total_tokens        int not null default 0,
  cost_usd            numeric(12, 6) not null default 0,
  elapsed_sec         numeric(10, 2),
  metadata            jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now()
);

create index if not exists usage_user_idx on public.usage_events(user_id, created_at desc);
create index if not exists usage_kind_idx on public.usage_events(kind, created_at desc);

alter table public.usage_events enable row level security;
-- Solo lectura del propio uso; las inserciones las hace el backend con la
-- service_role key (que ignora RLS), nunca el navegador.
drop policy if exists "usage: leer el propio" on public.usage_events;
create policy "usage: leer el propio" on public.usage_events
  for select using (auth.uid() = user_id);

-- ─── Alta de usuario + carpeta inicial ─────────────────────────────────────
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (id, email, name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)),
    new.raw_user_meta_data->>'avatar_url'
  )
  on conflict (id) do nothing;

  insert into public.folders (user_id, name, position)
  values (new.id, 'Guardados', 0)
  on conflict (user_id, name) do nothing;

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ─── updated_at automatico ─────────────────────────────────────────────────
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists touch_profiles on public.profiles;
create trigger touch_profiles before update on public.profiles
  for each row execute function public.touch_updated_at();

drop trigger if exists touch_saved on public.saved_papers;
create trigger touch_saved before update on public.saved_papers
  for each row execute function public.touch_updated_at();

drop trigger if exists touch_chats on public.chats;
create trigger touch_chats before update on public.chats
  for each row execute function public.touch_updated_at();
