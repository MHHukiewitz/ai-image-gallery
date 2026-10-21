-- Duplicates by image content, not only by file (#113). Since #85 the API stores each original
-- without its metadata, while content_sha256 stays the hash of the uploaded file: the web client
-- hashes the file it drops to find duplicates, and a browser only has that file. Two files that
-- differ only in metadata therefore had different hashes and were both stored, and so was an
-- original downloaded from the gallery (already stripped) and uploaded again.
--
-- stored_sha256 is the hash of the stored, stripped file. Stripping copies the image data byte for
-- byte, so the same picture with other metadata strips to the same bytes, and stripping a stripped
-- file changes nothing. The API treats an upload as a duplicate when either hash matches, and the
-- lookup checks both columns.
--
-- Additive and safe while the current API runs: a nullable column, an index and one more column at
-- the end of image_details. Rows from before, and rows the old API writes until the new one is
-- deployed, have no stored_sha256 until scripts/backfill_stored_sha256.py fills it in. They still
-- match by content_sha256. SQL can't hash Storage objects, so the backfill isn't done here.

alter table public.images
  add column stored_sha256 char(64)
    check (stored_sha256 is null or stored_sha256 ~ '^[0-9a-f]{64}$');

-- The lookup's second half: one user's images by stored hash, as images_user_id_content_sha256_idx
-- serves the first.
create index images_user_id_stored_sha256_idx
  on public.images (user_id, stored_sha256);

-- The API finds duplicates through image_details, so the view carries the new column. CREATE OR
-- REPLACE may only add columns at the end, keeps the view's grants, and needs security_invoker
-- restated so RLS keeps applying to the caller.
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
  i.last_uploaded_at,
  i.stored_sha256
from public.images i
join public.image_metadata m on m.image_id = i.id;
