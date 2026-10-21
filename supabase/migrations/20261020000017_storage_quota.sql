-- A cap on the objects one user may hold in the images bucket (#103). The bucket policies
-- (20261020000003_storage.sql) let a signed-in user upload any number of objects into their own
-- folder, each up to the bucket's 10 MB file_size_limit, without ever creating an images row, so
-- the upload quota (20261020000015_upload_quota.sql) never sees them. One user could fill the
-- project's storage, and then every tenant's uploads fail.
--
-- Additive and safe while the current API runs: one new column with a default, and one trigger.
-- Every image is two objects (the original and its thumbnail), so the default of 2000 objects is
-- 1000 images, far above what anyone holds today.
--
-- Why a count of objects, not bytes: the new object's size isn't reliably known to a before-insert
-- trigger. Older storage-api versions insert the row first and fill metadata->>'size' in an update
-- once the upload completes, and storage-api's permission pre-check inserts the row with only the
-- client's declared length. With the bucket's file_size_limit, N objects bound a user's bytes to
-- N x 10 MB, which is the guarantee this needs.

-- The setting ---------------------------------------------------------------------------------------

-- quota_settings keeps its access from 20261020000015: RLS on, no policies, no grants, so only the
-- database owner can change it. 0 turns this quota off.
alter table public.quota_settings
  add column max_storage_objects integer not null default 2000
    check (max_storage_objects >= 0);

-- The trigger ---------------------------------------------------------------------------------------

-- Refuses a new object in the images bucket when its owner's folder already holds the limit. The
-- owner is the first folder of the key, which is what the bucket policies key on; storage.objects'
-- owner and owner_id columns differ between storage-api versions. No role is special-cased: the
-- worker (service_role) never uploads, and an object in someone's folder counts against them
-- whoever put it there.
--
-- A before-row trigger sees the rows earlier in the same statement, so a bulk insert is counted
-- whole. Two uploads of one user in separate transactions can both see the last free slot, so the
-- folder can go over by the uploads in flight at that moment. That is accepted, unlike for the
-- upload quota: this bounds capacity, a few objects over does no harm, and a lock here would be
-- taken inside storage-api's own transactions.
--
-- The count leaves out a row with the new row's name: storage-api writes an upload as an upsert,
-- and a row that replaces an existing one adds no object.
--
-- SQLSTATE PT429 is PostgREST's convention for a custom HTTP status. storage-api (v1.82) doesn't
-- follow it: it answers 500 with the body's message "database error, code: PT429" and drops this
-- message, so the API matches the code (adapters/supabase_storage.py) and words the refusal itself.
-- The message below is what a database client, such as the SQL editor, sees.
--
-- Security definer because the inserting user has no privilege on the quota tables, and RLS would
-- hide other rows of storage.objects from a caller who can't see them. Postgres doesn't check
-- EXECUTE when a trigger fires, so no role needs the right to call it.
create function public.enforce_storage_quota() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  object_limit integer;
  folder text;
  used bigint;
begin
  -- A key outside any folder has no owner. The policies refuse it for users anyway.
  folder := (storage.foldername(new.name))[1];
  if folder is null or folder = '' then
    return new;
  end if;

  -- A missing row can't happen (the table holds exactly one and nobody can delete it); the default
  -- keeps an empty table from switching the quota off.
  select coalesce(
    (select s.max_storage_objects from public.quota_settings s),
    2000
  ) into object_limit;

  if object_limit = 0 then
    return new;
  end if;

  if exists (select 1 from public.quota_exempt_users e where e.user_id::text = folder) then
    return new;
  end if;

  -- The range is "name starts with folder/", which is (storage.foldername(name))[1] = folder, in
  -- a form the (bucket_id, name collate "C") index serves. '0' is the character after '/'.
  select count(*) into used
  from storage.objects o
  where o.bucket_id = 'images'
    and o.name collate "C" >= folder || '/'
    and o.name collate "C" < folder || '0'
    and o.name <> new.name;

  if used >= object_limit then
    raise sqlstate 'PT429' using
      message = format(
        'You''ve reached your storage limit of %s %s. Delete some images to make room.',
        object_limit, case when object_limit = 1 then 'file' else 'files' end
      ),
      hint = 'Each image takes two files: the original and its thumbnail.';
  end if;

  return new;
end;
$$;

revoke all on function public.enforce_storage_quota() from public, anon, authenticated, service_role;

create trigger enforce_storage_quota
  before insert on storage.objects
  for each row
  when (new.bucket_id = 'images')
  execute function public.enforce_storage_quota();
