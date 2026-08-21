-- satei.html cloud sync schema
--
-- Setup (one time):
--   1. Create a free project at https://supabase.com
--   2. Open the SQL Editor and run this whole file once.
--   3. Before running, replace 'CHANGE-ME' below with the shared
--      passphrase your office will use, then run this file.
--   4. In satei.html, click "共有設定" and enter:
--        - Project URL      : Settings -> API -> Project URL
--        - anon public key  : Settings -> API -> Project API keys -> anon public
--        - 共有パスフレーズ : the same passphrase you set below
--
-- Security model: the "cases" table has Row Level Security enabled with
-- NO policies, so the public anon key alone cannot read or write it.
-- The only way in is through the functions below, which are
-- SECURITY DEFINER (they run as the table owner and therefore bypass
-- RLS) and each independently re-checks the passphrase against a
-- bcrypt hash before doing anything. Losing the anon key alone does not
-- expose the data; the passphrase is still required.

create extension if not exists pgcrypto;

create table if not exists app_config (
  key text primary key,
  value text not null
);

insert into app_config (key, value)
values ('passphrase_hash', crypt('CHANGE-ME', gen_salt('bf')))
on conflict (key) do update set value = excluded.value;

create table if not exists cases (
  id text primary key,
  data jsonb not null,
  updated_at timestamptz not null default now()
);

alter table app_config enable row level security;
alter table cases enable row level security;
-- Intentionally no policies: this blocks all direct table access via
-- the REST API for anon/authenticated roles. All access goes through
-- the functions below.

create or replace function check_passphrase(p_passphrase text)
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (
    select 1 from app_config
    where key = 'passphrase_hash'
      and value = crypt(p_passphrase, value)
  );
$$;

create or replace function list_cases(p_passphrase text)
returns setof cases
language plpgsql
security definer
set search_path = public
as $$
begin
  if not check_passphrase(p_passphrase) then
    raise exception 'invalid passphrase';
  end if;
  return query select * from cases order by updated_at desc;
end;
$$;

create or replace function upsert_case(p_passphrase text, p_id text, p_data jsonb)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not check_passphrase(p_passphrase) then
    raise exception 'invalid passphrase';
  end if;
  insert into cases (id, data, updated_at)
  values (p_id, p_data, now())
  on conflict (id) do update set data = excluded.data, updated_at = now();
end;
$$;

create or replace function delete_case(p_passphrase text, p_id text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not check_passphrase(p_passphrase) then
    raise exception 'invalid passphrase';
  end if;
  delete from cases where id = p_id;
end;
$$;

grant execute on function list_cases(text) to anon;
grant execute on function upsert_case(text, text, jsonb) to anon;
grant execute on function delete_case(text, text) to anon;

-- To change the shared passphrase later, just re-run:
--   insert into app_config (key, value)
--   values ('passphrase_hash', crypt('NEW-PASSPHRASE', gen_salt('bf')))
--   on conflict (key) do update set value = excluded.value;
