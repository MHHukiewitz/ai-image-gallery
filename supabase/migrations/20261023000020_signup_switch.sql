-- The sign-up switch (docs/architecture.md section 4, "Closing sign-ups", and section 5). An admin
-- turns new sign-ups on or off from the web app. While they are off, no account can be created,
-- whoever asks: the sign-up page, a direct call to Supabase Auth with the public publishable key,
-- an invite from the dashboard. Existing users still sign in, because signing in creates no user.
--
-- Enforced by a trigger on auth.users rather than Supabase's "before user created" auth hook: the
-- hook must also be switched on in the production dashboard, and if that step were missed sign-ups
-- would stay open with nothing to show for it. The trigger ships with this migration. Its cost is
-- the error Auth answers with: a generic 500 "Database error saving new user", which is why the web
-- asks signups_allowed() first and says sign-ups are closed before anyone fills in the form.
--
-- Additive: sign-ups start allowed, so nothing changes until an admin turns them off.

-- admins -----------------------------------------------------------------------------------------

-- Who may change site settings, by account id. An id rather than an e-mail address, because e-mail
-- confirmation is off: whoever registers or changes to an address gets it unverified, so an address
-- alone must never grant anything.
create table public.admins (
  user_id uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now()
);

alter table public.admins enable row level security;

-- No policies and no grants: only is_admin() and the security definer functions below read it, and
-- only someone with database access adds a row.
revoke all on table public.admins from anon, authenticated;

-- The site owner's account, if it exists. Locally it doesn't, so add an admin by hand there:
--   insert into public.admins (user_id) select id from auth.users where email = '<address>';
insert into public.admins (user_id)
select u.id
from auth.users u
where u.email = 'mike@hukiewitz.com'
on conflict do nothing;

-- Whether the caller is an admin. False for anon and for the worker, which have no auth.uid().
create function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (select 1 from public.admins a where a.user_id = (select auth.uid()))
$$;

revoke execute on function public.is_admin() from public, anon, service_role;
grant execute on function public.is_admin() to authenticated;

-- site_settings ----------------------------------------------------------------------------------

-- One row, enforced by the primary key that can only be true.
create table public.site_settings (
  id boolean primary key default true check (id),
  allow_signups boolean not null default true,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users (id) on delete set null
);

insert into public.site_settings (id) values (true);

alter table public.site_settings enable row level security;

-- No policies and no grants: reads and writes go through the functions below, which check the
-- caller. The table is never read directly, so nobody but an admin sees who changed it and when.
revoke all on table public.site_settings from anon, authenticated;

-- Whether sign-ups are open. Anyone may ask, signed in or not: the sign-up page needs to know before
-- the visitor has an account, and trying to sign up tells anyone the same thing anyway.
create function public.signups_allowed()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  -- A missing row reads as closed, so losing it can't open sign-ups.
  select coalesce((select s.allow_signups from public.site_settings s where s.id), false)
$$;

revoke execute on function public.signups_allowed() from public, service_role;
grant execute on function public.signups_allowed() to anon, authenticated;

-- The settings with when they last changed. Admins only: anyone else gets PT403, which PostgREST
-- answers with HTTP 403 and keeps as the code for the API to map.
create function public.get_site_settings()
returns table (allow_signups boolean, updated_at timestamptz)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  if not public.is_admin() then
    raise exception 'Only an admin can see the site settings.' using errcode = 'PT403';
  end if;

  return query select s.allow_signups, s.updated_at from public.site_settings s where s.id;
end;
$$;

revoke execute on function public.get_site_settings() from public, anon, service_role;
grant execute on function public.get_site_settings() to authenticated;

-- Turns sign-ups on or off and returns the settings as get_site_settings does. Admins only (PT403).
create function public.set_allow_signups(p_allow boolean)
returns table (allow_signups boolean, updated_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not public.is_admin() then
    raise exception 'Only an admin can change the site settings.' using errcode = 'PT403';
  end if;
  if p_allow is null then
    raise exception 'p_allow must be true or false.' using errcode = '22004';
  end if;

  return query
  update public.site_settings s
  set allow_signups = p_allow,
      updated_at = now(),
      updated_by = (select auth.uid())
  where s.id
  returning s.allow_signups, s.updated_at;
end;
$$;

revoke execute on function public.set_allow_signups(boolean) from public, anon, service_role;
grant execute on function public.set_allow_signups(boolean) to authenticated;

-- The enforcement ------------------------------------------------------------------------------

-- Refuses every new auth.users row while sign-ups are off. Supabase Auth inserts as
-- supabase_auth_admin, which has no grant on site_settings, hence security definer. There is no
-- exemption for any role, so to add an account by hand, allow sign-ups for that moment.
create function public.refuse_closed_signups()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not public.signups_allowed() then
    raise exception 'Sign-ups are closed.' using errcode = 'P0001';
  end if;
  return new;
end;
$$;

revoke execute on function public.refuse_closed_signups() from public, anon, authenticated, service_role;

create trigger refuse_closed_signups
  before insert on auth.users
  for each row execute function public.refuse_closed_signups();
