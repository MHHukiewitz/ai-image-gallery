-- images.stored_sha256, the hash of the stored, metadata-stripped original (#113): the column, its
-- check, its index, image_details carrying it, and RLS keeping one user's hashes from another.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-000000000181 owns a-1.
-- User B = 00000000-0000-4000-8000-000000000182 owns nothing.

begin;

select plan(15);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-000000000181', 'stored-a@example.test'),
  ('00000000-0000-4000-8000-000000000182', 'stored-b@example.test');

-- Inserts one of A's images as postgres, with the given stored hash.
create function pg_temp.insert_as_postgres(slug text, stored text)
returns void
language sql
as $$
  insert into public.images (
    user_id, filename, original_path, thumbnail_path,
    mime_type, size_bytes, width, height, content_sha256, stored_sha256
  )
  values (
    '00000000-0000-4000-8000-000000000181', slug || '.jpg',
    '00000000-0000-4000-8000-000000000181/' || slug || '/original.jpg',
    '00000000-0000-4000-8000-000000000181/' || slug || '/thumb.webp',
    'image/jpeg', 1024, 640, 480, repeat('a', 64), stored
  )
$$;

-- Structure -------------------------------------------------------------------------------------

select has_column('public', 'images', 'stored_sha256', 'images has a stored_sha256 column');
select col_type_is('public', 'images', 'stored_sha256', 'character(64)', 'stored_sha256 is char(64), like content_sha256');
select col_is_null(
  'public', 'images', 'stored_sha256',
  'stored_sha256 is nullable: rows from before it existed have none until the backfill'
);
select has_index(
  'public', 'images', 'images_user_id_stored_sha256_idx', array['user_id', 'stored_sha256'],
  'duplicates by stored hash are looked up through an index on (user_id, stored_sha256)'
);

select throws_ok(
  $$select pg_temp.insert_as_postgres('bad-hex', repeat('z', 64))$$,
  '23514', null,
  'a stored hash that is not hex is refused'
);
select throws_ok(
  $$select pg_temp.insert_as_postgres('upper-hex', repeat('A', 64))$$,
  '23514', null,
  'a stored hash in upper case is refused, as the API writes lower case'
);
select lives_ok(
  $$select pg_temp.insert_as_postgres('no-stored-hash', null)$$,
  'a row without a stored hash is allowed'
);
select lives_ok(
  $$select pg_temp.insert_as_postgres('with-stored-hash', repeat('d', 64))$$,
  'a row with a lower-case hex stored hash is allowed'
);

select has_column('public', 'image_details', 'stored_sha256', 'image_details carries stored_sha256');
select is(
  (select attname::text from pg_attribute
   where attrelid = 'public.image_details'::regclass and attnum > 0 and not attisdropped
   order by attnum desc limit 1),
  'stored_sha256',
  'stored_sha256 is the last column of image_details, so CREATE OR REPLACE could add it'
);
select ok(
  (select 'security_invoker=true' = any (reloptions) from pg_class
   where oid = 'public.image_details'::regclass),
  'image_details still runs as the caller, so RLS applies to it'
);

-- As A --------------------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000181", "role": "authenticated"}';

-- Two statements, as the API sends them: the metadata policy checks that the image exists.
select lives_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256, stored_sha256
    )
    values (
      (select auth.uid()), 'a-1.jpg',
      (select auth.uid())::text || '/a-1/original.jpg',
      (select auth.uid())::text || '/a-1/thumb.webp',
      'image/jpeg', 2048, 640, 480, repeat('b', 64), repeat('e', 64)
    )$$,
  'a user can insert their own image with a stored hash, as the API does'
);
select lives_ok(
  $$insert into public.image_metadata (image_id, user_id)
    select id, user_id from public.images where filename = 'a-1.jpg'$$,
  'and its pending metadata row'
);
select results_eq(
  $$select filename::text from public.image_details where stored_sha256 = repeat('e', 64)$$,
  $$values ('a-1.jpg')$$,
  'the owner finds the image by its stored hash through image_details'
);

reset role;

-- As B --------------------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000182", "role": "authenticated"}';

select is_empty(
  $$select id from public.image_details where stored_sha256 = repeat('e', 64)$$,
  'another user never matches A''s stored hash'
);

reset role;

select * from finish();
rollback;
