-- The daily upload quota, enforced in the database (#95). The API checks the quota first (#77, #84),
-- but `authenticated` may insert into public.images through PostgREST with its own JWT, which skips
-- the API, and the worker then analyzes the image at AI cost. This refuses such an insert once the
-- caller has reached the limit in upload_log. The API check stays as the friendly first line.
--
-- Additive and safe while the current API runs: two new tables and one trigger. The API's own
-- check refuses at its UPLOAD_DAILY_QUOTA before this does, and counts the same table, so uploads
-- behave as before. Keep that setting at or below the limit here, and add the owner to
-- quota_exempt_users (below) as well as to the API's UPLOAD_QUOTA_EXEMPT_USER_IDS: an owner who is
-- exempt only in the API is refused here after 300 uploads in 24 hours.
--
-- Supabase's default privileges grant every Data API role full access to a new table and EXECUTE
-- on a new function, and PostgreSQL grants EXECUTE to PUBLIC. Everything below revokes what it must
-- not hand out instead of relying on a bare GRANT.

-- quota_settings --------------------------------------------------------------------------------

-- One row, holding the limit. It lives in the database, not in a function body, so changing it is
-- an UPDATE from the SQL editor, not a migration deployed through main.
create table public.quota_settings (
  -- The only allowed value is true, so a second row can't exist.
  singleton boolean primary key default true check (singleton),
  -- New images one user may add in a rolling 24 hours. 0 turns the quota off, as it does for the
  -- API's UPLOAD_DAILY_QUOTA.
  daily_upload_limit integer not null default 300 check (daily_upload_limit >= 0)
);

insert into public.quota_settings default values;

-- quota_exempt_users ----------------------------------------------------------------------------

-- Users the quota skips. An id, unlike an email, can't be claimed by signing up with it. Deleting
-- the user removes the row.
create table public.quota_exempt_users (
  user_id uuid primary key references auth.users on delete cascade
);

-- Access ------------------------------------------------------------------------------------------

-- RLS on with no policies and no grants: no user-facing role reads or writes either table, not even
-- service_role, which the worker uses. Only the database owner changes them: the SQL editor, the
-- CLI, a migration. The trigger function below reads them as its owner.
alter table public.quota_settings enable row level security;
alter table public.quota_exempt_users enable row level security;

revoke all on table public.quota_settings, public.quota_exempt_users
  from anon, authenticated, service_role;

-- The trigger ---------------------------------------------------------------------------------------

-- Refuses a statement that leaves a user above the limit. It runs after the statement, as a
-- statement-level trigger over a transition table, and not before each row:
--   * log_upload (#84) writes upload_log from an after-row trigger, which fires at the end of the
--     statement. A before-row trigger therefore can't see the earlier rows of the same statement
--     in upload_log, so one bulk insert of a thousand rows would pass every row's check. Here the
--     log rows exist already, so the count includes the whole batch.
--   * RLS WITH CHECK has run by now, so a user who inserts a row with someone else's user_id gets
--     the policy's error, never this one, and can't learn whether that user is at their limit.
-- Raising rolls back the whole statement, rows and log rows together.
--
-- Two transactions of one user can both count a free slot, so they queue on an advisory lock until
-- the first commits. The count that follows is a new statement, so it sees that commit. The lock
-- is not taken for a user the quota doesn't apply to.
--
-- SQLSTATE PT429 is PostgREST's convention for a custom HTTP status: it answers 429 with the
-- message below, which the API turns into its own quota_exceeded response (adapters/
-- supabase_images.py). The message is the text the API uses for the same refusal.
--
-- Security definer because the inserting user has no privilege on the tables it reads. Postgres
-- doesn't check EXECUTE when a trigger fires, so no role needs the right to call it.
create function public.enforce_upload_quota() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  daily_limit integer;
  candidate uuid;
  used bigint;
begin
  -- A missing row can't happen (the table holds exactly one and nobody can delete it); the default
  -- keeps an empty table from switching the quota off.
  select coalesce(
    (select s.daily_upload_limit from public.quota_settings s),
    300
  ) into daily_limit;

  if daily_limit = 0 then
    return null;
  end if;

  -- In id order, so two batches that share users take their locks in the same order.
  for candidate in
    select distinct n.user_id
    from new_rows n
    where not exists (select 1 from public.quota_exempt_users e where e.user_id = n.user_id)
    order by n.user_id
  loop
    perform pg_advisory_xact_lock(hashtext('upload_quota'), hashtext(candidate::text));

    select count(*) into used
    from public.upload_log l
    where l.user_id = candidate
      and l.uploaded_at >= now() - interval '24 hours';

    if used > daily_limit then
      raise sqlstate 'PT429' using
        message = format(
          'You''ve reached today''s upload limit of %s %s. Try again tomorrow.',
          daily_limit, case when daily_limit = 1 then 'image' else 'images' end
        ),
        hint = 'The limit counts uploads in the last 24 hours, including images deleted since.';
    end if;
  end loop;

  return null;
end;
$$;

revoke all on function public.enforce_upload_quota() from public, anon, authenticated, service_role;

-- Created after log_upload's trigger, though it doesn't depend on the order: statement-level
-- after triggers fire after every row-level one.
create trigger images_enforce_upload_quota
  after insert on public.images
  referencing new table as new_rows
  for each statement
  execute function public.enforce_upload_quota();
