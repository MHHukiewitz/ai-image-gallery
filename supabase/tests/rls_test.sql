-- Tenant isolation for images, image_metadata, image_details and the images bucket.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-00000000000a owns images a-1, a-2 and a-3.
-- User B = 00000000-0000-4000-8000-00000000000b owns image b-1.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.

begin;

select plan(55);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-00000000000a', 'a@example.test'),
  ('00000000-0000-4000-8000-00000000000b', 'b@example.test');

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256
)
select
  fixture.user_id,
  fixture.slug || '.jpg',
  format('%s/%s/original.jpg', fixture.user_id, fixture.slug),
  format('%s/%s/thumb.webp', fixture.user_id, fixture.slug),
  'image/jpeg', 1024, 640, 480, repeat('a', 64)
from (
  values
    ('a-1', '00000000-0000-4000-8000-00000000000a'::uuid),
    ('a-2', '00000000-0000-4000-8000-00000000000a'::uuid),
    ('a-3', '00000000-0000-4000-8000-00000000000a'::uuid),
    ('b-1', '00000000-0000-4000-8000-00000000000b'::uuid)
) as fixture (slug, user_id);

-- a-3 deliberately has no metadata yet, so A can queue it below.
insert into public.image_metadata (image_id, user_id, ai_processing_status, tags)
select i.id, i.user_id, fixture.status, fixture.tags
from (
  values
    ('a-1.jpg', 'pending', '{}'::text[]),
    ('a-2.jpg', 'done', '{beach,sunset}'::text[]),
    ('b-1.jpg', 'done', '{dog}'::text[])
) as fixture (filename, status, tags)
join public.images i on i.filename = fixture.filename;

-- Ids are generated (sequences don't roll back), and A can't look up B's rows, so remember the
-- ids that statements run as A need to name.
select set_config('tests.b1_id', id::text, true) from public.images where filename = 'b-1.jpg';
select set_config('tests.a1_id', id::text, true) from public.images where filename = 'a-1.jpg';

insert into storage.buckets (id, name) values ('other-bucket', 'other-bucket');

insert into storage.objects (bucket_id, name)
values
  ('images', '00000000-0000-4000-8000-00000000000a/a-1/original.jpg'),
  ('images', '00000000-0000-4000-8000-00000000000b/b-1/original.jpg'),
  ('other-bucket', '00000000-0000-4000-8000-00000000000a/a-1/original.jpg');

-- Structure -------------------------------------------------------------------------------------

select ok(
  (select relrowsecurity from pg_class where oid = 'public.images'::regclass),
  'RLS is enabled on images'
);
select ok(
  (select relrowsecurity from pg_class where oid = 'public.image_metadata'::regclass),
  'RLS is enabled on image_metadata'
);
select policies_are(
  'public', 'images',
  array['images: owner can select', 'images: owner can insert', 'images: owner can delete'],
  'images has select, insert and delete policies and no update policy'
);
select policies_are(
  'public', 'image_metadata',
  array['image_metadata: owner can select', 'image_metadata: owner can queue analysis'],
  'image_metadata has select and insert policies only'
);
select ok(
  (select 'security_invoker=true' = any (reloptions)
   from pg_class where oid = 'public.image_details'::regclass),
  'image_details runs with the caller''s privileges, so RLS applies through it'
);
select table_privs_are('public', 'images', 'anon', array[]::text[], 'anon has no privileges on images');
select table_privs_are('public', 'image_metadata', 'anon', array[]::text[], 'anon has no privileges on image_metadata');
select table_privs_are('public', 'image_details', 'anon', array[]::text[], 'anon has no privileges on image_details');
select table_privs_are(
  'public', 'images', 'authenticated', array['SELECT', 'INSERT', 'DELETE'],
  'authenticated can select, insert and delete images, but not update or truncate'
);
select table_privs_are(
  'public', 'image_metadata', 'authenticated', array['SELECT', 'INSERT'],
  'authenticated can select and insert image_metadata only'
);
select table_privs_are(
  'public', 'image_details', 'authenticated', array['SELECT'],
  'authenticated can only select image_details'
);
select results_eq(
  $$select public, file_size_limit, allowed_mime_types from storage.buckets where id = 'images'$$,
  $$values (false, 10485760::bigint, array['image/jpeg', 'image/png', 'image/webp'])$$,
  'the images bucket is private, capped at 10 MiB and limited to JPEG, PNG and WebP'
);

-- Reads: each user sees only their own rows ------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select results_eq(
  'select filename::text from public.images order by filename',
  array['a-1.jpg', 'a-2.jpg', 'a-3.jpg'],
  'A sees only A''s images'
);
select results_eq(
  'select user_id from public.image_metadata',
  array['00000000-0000-4000-8000-00000000000a', '00000000-0000-4000-8000-00000000000a']::uuid[],
  'A sees only A''s image_metadata'
);
select results_eq(
  'select filename::text from public.image_details order by filename',
  array['a-1.jpg', 'a-2.jpg'],
  'A sees only A''s image_details'
);
select is_empty(
  $$select id from public.images where id = current_setting('tests.b1_id')::bigint$$,
  'A can''t read B''s image even by its id'
);

set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000b", "role": "authenticated"}';

select results_eq(
  'select filename::text from public.image_details order by filename',
  array['b-1.jpg'],
  'B sees only B''s image_details'
);

-- Inserts into images ----------------------------------------------------------------------------

set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select lives_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'a-new.png',
            '00000000-0000-4000-8000-00000000000a/a-new/original.png',
            '00000000-0000-4000-8000-00000000000a/a-new/thumb.webp',
            'image/png', 1, 1, 1, repeat('b', 64))$$,
  'A can insert an image of A''s own'
);
select throws_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000b', 'forged.jpg',
            '00000000-0000-4000-8000-00000000000b/forged/original.jpg',
            '00000000-0000-4000-8000-00000000000b/forged/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('b', 64))$$,
  '42501', 'new row violates row-level security policy for table "images"',
  'A can''t insert an image with B''s user_id'
);
select throws_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'steal.jpg',
            '00000000-0000-4000-8000-00000000000b/b-1/original.jpg',
            '00000000-0000-4000-8000-00000000000a/steal/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('b', 64))$$,
  '23514',
  'new row for relation "images" violates check constraint "images_original_path_in_owner_folder"',
  'A can''t point an image at an object in B''s folder'
);
select throws_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'steal.jpg',
            '00000000-0000-4000-8000-00000000000a/../00000000-0000-4000-8000-00000000000b/b-1/original.jpg',
            '00000000-0000-4000-8000-00000000000a/steal/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('b', 64))$$,
  '23514',
  'new row for relation "images" violates check constraint "images_original_path_in_owner_folder"',
  'A can''t escape A''s folder with ".." in an object key'
);
select throws_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'steal.jpg',
            '00000000-0000-4000-8000-00000000000a/steal/original.jpg',
            '00000000-0000-4000-8000-00000000000b/b-1/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('b', 64))$$,
  '23514',
  'new row for relation "images" violates check constraint "images_thumbnail_path_in_owner_folder"',
  'A can''t point a thumbnail at an object in B''s folder'
);

-- Inserts into image_metadata --------------------------------------------------------------------

select throws_ok(
  $$insert into public.image_metadata (image_id, user_id)
    values (current_setting('tests.b1_id')::bigint, '00000000-0000-4000-8000-00000000000a')$$,
  '42501', 'new row violates row-level security policy for table "image_metadata"',
  'A can''t insert metadata for B''s image'
);
select throws_ok(
  $$insert into public.image_metadata (image_id, user_id)
    values (current_setting('tests.b1_id')::bigint, '00000000-0000-4000-8000-00000000000b')$$,
  '42501', 'new row violates row-level security policy for table "image_metadata"',
  'A can''t insert metadata for B''s image in B''s name'
);
select throws_ok(
  $$insert into public.image_metadata (image_id, user_id)
    select id, '00000000-0000-4000-8000-00000000000b' from public.images where filename = 'a-3.jpg'$$,
  '42501', 'new row violates row-level security policy for table "image_metadata"',
  'A can''t insert metadata for A''s image in B''s name'
);
select throws_ok(
  $$insert into public.image_metadata (image_id, user_id, ai_processing_status, tags)
    select id, user_id, 'done', '{fake}' from public.images where filename = 'a-3.jpg'$$,
  '42501', 'new row violates row-level security policy for table "image_metadata"',
  'A can''t insert metadata with status done'
);
select lives_ok(
  $$insert into public.image_metadata (image_id, user_id)
    select id, user_id from public.images where filename = 'a-3.jpg'$$,
  'A can queue pending metadata for A''s own image'
);

-- Updates ----------------------------------------------------------------------------------------

select throws_ok(
  $$update public.image_metadata set ai_processing_status = 'done'$$,
  '42501', 'permission denied for table image_metadata',
  'A can''t update A''s own metadata'
);
select throws_ok(
  $$update public.images set filename = 'renamed.jpg'$$,
  '42501', 'permission denied for table images',
  'A can''t update A''s own images'
);

-- Second layer: even if UPDATE were granted again, there is no update policy, so nothing matches.
reset role;
grant update on public.images, public.image_metadata to authenticated;
set local role authenticated;

select is_empty(
  $$update public.image_metadata set ai_processing_status = 'done' returning id$$,
  'with UPDATE granted, A''s metadata update still affects zero rows'
);
select is_empty(
  $$update public.images set filename = 'renamed.jpg' returning id$$,
  'with UPDATE granted, A''s image update still affects zero rows'
);

reset role;
revoke update on public.images, public.image_metadata from authenticated;
set local role authenticated;

-- Deletes ----------------------------------------------------------------------------------------

select throws_ok(
  $$delete from public.image_metadata$$,
  '42501', 'permission denied for table image_metadata',
  'A can''t delete metadata directly'
);
select is_empty(
  $$delete from public.images where id = current_setting('tests.b1_id')::bigint returning id$$,
  'A''s delete of B''s image affects zero rows'
);
select results_eq(
  $$delete from public.images where filename = 'a-1.jpg' returning filename::text$$,
  array['a-1.jpg'],
  'A can delete A''s own image'
);

reset role;

select is_empty(
  $$select id from public.image_metadata where image_id = current_setting('tests.a1_id')::bigint$$,
  'deleting A''s image cascades to its metadata'
);

-- With a WHERE clause the select policy hides B's rows anyway. An unfiltered delete is checked
-- against the delete policy alone.
set local role authenticated;

select lives_ok('delete from public.images', 'A can run an unfiltered delete on images');

reset role;

select results_eq(
  'select filename::text from public.images',
  array['b-1.jpg'],
  'an unfiltered delete by A removes only A''s images'
);
select results_eq(
  'select user_id from public.image_metadata',
  array['00000000-0000-4000-8000-00000000000b']::uuid[],
  'and only A''s metadata goes with them'
);

-- anon -------------------------------------------------------------------------------------------

set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  'select id from public.images',
  '42501', 'permission denied for table images',
  'anon can''t read images'
);
select throws_ok(
  'select id from public.image_metadata',
  '42501', 'permission denied for table image_metadata',
  'anon can''t read image_metadata'
);
select throws_ok(
  'select id from public.image_details',
  '42501', 'permission denied for view image_details',
  'anon can''t read image_details'
);
select throws_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'anon.jpg',
            '00000000-0000-4000-8000-00000000000a/anon/original.jpg',
            '00000000-0000-4000-8000-00000000000a/anon/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('c', 64))$$,
  '42501', 'permission denied for table images',
  'anon can''t insert images'
);
select is_empty(
  'select name from storage.objects',
  'anon sees no storage objects'
);

-- Storage ----------------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select results_eq(
  'select bucket_id, name from storage.objects',
  $$values ('images', '00000000-0000-4000-8000-00000000000a/a-1/original.jpg')$$,
  'A sees only objects in A''s folder of the images bucket'
);
select is_empty(
  $$select name from storage.objects
    where name = '00000000-0000-4000-8000-00000000000b/b-1/original.jpg'$$,
  'A can''t select an object under B''s folder'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-00000000000b/planted/original.jpg')$$,
  '42501', 'new row violates row-level security policy for table "objects"',
  'A can''t upload into B''s folder'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name) values ('images', 'loose.jpg')$$,
  '42501', 'new row violates row-level security policy for table "objects"',
  'A can''t upload outside any user folder'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('other-bucket', '00000000-0000-4000-8000-00000000000a/new/original.jpg')$$,
  '42501', 'new row violates row-level security policy for table "objects"',
  'A''s folder policies don''t reach other buckets'
);
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-00000000000a/new/original.jpg')$$,
  'A can upload into A''s own folder'
);

-- No RETURNING here: a moved row would then fail the select policy and abort the whole run,
-- instead of failing the check below.
select lives_ok(
  $$update storage.objects
    set name = '00000000-0000-4000-8000-00000000000b/moved/original.jpg'
    where name = '00000000-0000-4000-8000-00000000000a/a-1/original.jpg'$$,
  'A can issue an update that would move an object into B''s folder'
);
select results_eq(
  'select name from storage.objects order by name',
  array[
    '00000000-0000-4000-8000-00000000000a/a-1/original.jpg',
    '00000000-0000-4000-8000-00000000000a/new/original.jpg'
  ],
  'but nothing moves, because objects have no update policy'
);

-- Storage blocks direct deletes unless this is set; the Storage API sets it for its own deletes.
set local storage.allow_delete_query = 'true';

select is_empty(
  $$delete from storage.objects
    where name = '00000000-0000-4000-8000-00000000000b/b-1/original.jpg'
    returning name$$,
  'A''s delete of B''s object affects zero rows'
);
select results_eq(
  $$delete from storage.objects
    where bucket_id = 'images' and name = '00000000-0000-4000-8000-00000000000a/a-1/original.jpg'
    returning name$$,
  array['00000000-0000-4000-8000-00000000000a/a-1/original.jpg'],
  'A can delete an object in A''s own folder'
);
select lives_ok('delete from storage.objects', 'A can run an unfiltered delete on storage objects');

reset role;

select results_eq(
  'select bucket_id, name from storage.objects order by bucket_id, name',
  $$values
    ('images', '00000000-0000-4000-8000-00000000000b/b-1/original.jpg'),
    ('other-bucket', '00000000-0000-4000-8000-00000000000a/a-1/original.jpg')$$,
  'an unfiltered delete by A removes only A''s objects in the images bucket'
);

select * from finish();

rollback;
