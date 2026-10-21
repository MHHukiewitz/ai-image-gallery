-- An append-only log of uploads, for the daily quota (#84). The quota used to count the caller's rows
-- in images, so deleting an image gave its slot back, and upload, delete, upload again had no limit.
-- A log row outlives its image. Additive: a new table and an insert trigger. The running app keeps
-- counting images until the new API counts this table instead.

-- upload_log ------------------------------------------------------------------------------------

create table public.upload_log (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  uploaded_at timestamptz not null default now()
);

-- The quota's count: one user's uploads since a moment.
create index upload_log_user_id_uploaded_at_idx on public.upload_log (user_id, uploaded_at);

alter table public.upload_log enable row level security;

-- Owners read their own uploads, which is all the quota count needs.
create policy "upload_log: owner can select"
  on public.upload_log for select to authenticated
  using ((select auth.uid()) = user_id);

-- No insert, update or delete policies: only the trigger below writes, and nothing rewrites or
-- removes a row, so deleting an image can't free its slot. Deleting the user removes their rows.

-- Supabase grants every Data API role full access to new public tables by default. Keep only reads:
-- the trigger function inserts as its owner, so no role needs an insert grant.
revoke all on table public.upload_log from anon, authenticated, service_role;
revoke all on sequence public.upload_log_id_seq from anon, authenticated, service_role;

grant select on table public.upload_log to authenticated, service_role;

-- Logging trigger -------------------------------------------------------------------------------

-- Logs one row per new image. It is security definer because the inserting user has no insert grant
-- on upload_log. The row takes the database's clock, not the image's uploaded_at, which a client
-- inserting through PostgREST could set to the past. Bumps (bump_image, bump_images) update
-- last_uploaded_at and insert nothing, so they stay free. Postgres doesn't check EXECUTE when a
-- trigger fires, so no role needs the right to call it.
create function public.log_upload() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.upload_log (user_id) values (new.user_id);
  return null;
end;
$$;

revoke all on function public.log_upload() from public, anon, authenticated, service_role;

-- Created before the backfill: creating it locks images against inserts until this migration's
-- transaction ends, so an upload that lands while it applies is logged once, by one or the other.
create trigger images_log_upload
  after insert on public.images
  for each row
  execute function public.log_upload();

-- Backfill --------------------------------------------------------------------------------------

-- One row per image that exists now, at its own upload time, so the quota's window is the same
-- before and after this applies. Images deleted earlier left no trace, so they can't be logged.
insert into public.upload_log (user_id, uploaded_at)
select user_id, uploaded_at
from public.images
order by uploaded_at, id;
