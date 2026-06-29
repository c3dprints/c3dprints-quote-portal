-- C3D Prints Quote Portal - Supabase schema
-- Run this in Supabase SQL Editor.

create table if not exists public.quote_requests (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  name text not null,
  email text not null,
  phone text,
  project_description text not null,
  quantity integer not null default 1,
  approx_size text,
  deadline text,
  material_preference text,
  color_preference text,
  use_case text,
  requirements jsonb default '[]'::jsonb,
  delivery_method text,
  shipping_location text,
  additional_notes text,
  uploaded_files jsonb default '[]'::jsonb,
  ai_summary text,
  ai_quote_assist text,
  ai_quote_structured jsonb default '{}'::jsonb,
  status text not null default 'New'
);

-- Additive migration for existing tables (safe to re-run).
alter table public.quote_requests add column if not exists ai_quote_assist text;
alter table public.quote_requests add column if not exists ai_quote_structured jsonb default '{}'::jsonb;

create index if not exists idx_quote_requests_created_at
on public.quote_requests (created_at desc);

create index if not exists idx_quote_requests_status
on public.quote_requests (status);

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'quote_requests_status_check'
  ) then
    alter table public.quote_requests
    add constraint quote_requests_status_check
    check (status in ('New','Need Info','Quoted','Approved','Printing','Completed','Archived'));
  end if;
end $$;
