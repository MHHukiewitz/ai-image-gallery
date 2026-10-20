-- Private bucket for originals and thumbnails (docs/architecture.md section 5).
-- Object keys are {user_id}/{uuid}/original.jpg|png and {user_id}/{uuid}/thumb.webp, so the
-- first folder of a key names its owner. The API reads and writes as the user; the worker uses
-- the secret key, which bypasses these policies.

-- Upsert so a bucket created by hand in the dashboard ends up with these settings too.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('images', 'images', false, 10485760, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Owners read (and sign URLs for) only objects in their own folder of the images bucket.
create policy "images bucket: owner can select own folder"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- Owners upload only into their own folder of the images bucket.
create policy "images bucket: owner can insert into own folder"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- Owners delete only objects in their own folder of the images bucket.
create policy "images bucket: owner can delete own folder"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- No update policy: objects are write-once, so uploads can't upsert and nothing can be moved
-- or renamed into another user's folder.
