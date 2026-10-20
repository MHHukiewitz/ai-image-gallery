-- Per-user duplicate detection and "bump to top" on re-upload (docs/architecture.md section 5).
-- Additive: no column is dropped or retyped, no row is deleted, and the running app keeps working
-- before and after this applies (it inserts without last_uploaded_at, which then takes its default).

-- Finding duplicates ----------------------------------------------------------------------------

-- Answers "which of my images have these hashes". It is not unique on purpose: production already
-- holds duplicates (from re-uploads after the Storage incident), and a migration must not delete
-- user data. The upload service checks before it stores instead.
create index images_user_id_content_sha256_idx
  on public.images (user_id, content_sha256);

-- Sort time ------------------------------------------------------------------------------------

-- When the user last uploaded this image. Re-uploading an image they already have moves it to the
-- top of their gallery by updating this, not by storing a copy. uploaded_at stays the first upload.
alter table public.images add column last_uploaded_at timestamptz;

-- Backfill from uploaded_at, so the gallery's order does not change when this applies. Adding the
-- column with default now() would instead give every existing row the same timestamp.
update public.images set last_uploaded_at = uploaded_at;

alter table public.images alter column last_uploaded_at set default now();
alter table public.images alter column last_uploaded_at set not null;

-- Keyset-friendly ordering of a user's gallery, most recently uploaded first.
create index images_user_id_last_uploaded_at_id_idx
  on public.images (user_id, last_uploaded_at desc, id desc);

-- The same columns as before, with last_uploaded_at appended: CREATE OR REPLACE VIEW can only add
-- columns at the end. The privileges of the existing view are kept, but security_invoker is a
-- view option, so it is restated.
create or replace view public.image_details
with (security_invoker = true)
as
select
  i.id,
  i.user_id,
  i.filename,
  i.original_path,
  i.thumbnail_path,
  i.mime_type,
  i.size_bytes,
  i.width,
  i.height,
  i.content_sha256,
  i.uploaded_at,
  m.description,
  m.tags,
  m.colors,
  m.ai_processing_status,
  m.last_error,
  i.last_uploaded_at
from public.images i
join public.image_metadata m on m.image_id = i.id;

-- bump_image ------------------------------------------------------------------------------------

-- Moves the caller's own image to the top of their gallery. Images stay immutable for users (there
-- is still no UPDATE policy or grant), so this function is the only way to change one, and it
-- changes the sort time and nothing else. Returns whether a row changed, so the API can tell
-- "missing or not yours" from success.
create function public.bump_image(p_image_id bigint)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.images i
  set last_uploaded_at = now()
  where i.id = p_image_id
    and i.user_id = (select auth.uid());

  return found;
end;
$$;

-- Authenticated only: the function reads auth.uid(), which is null for the worker.
revoke execute on function public.bump_image(bigint) from public, anon, service_role;
grant execute on function public.bump_image(bigint) to authenticated;
