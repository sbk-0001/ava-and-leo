# Optional shared mock diary for Vercel + LiveKit Cloud (two users).
-- Table for DIARY_STORE=supabase / MOCK_DIARY_TABLE=mock_diary
create table if not exists public.mock_diary (
  id text primary key,
  payload jsonb not null,
  updated_at timestamptz not null default now()
);

alter table public.mock_diary enable row level security;
