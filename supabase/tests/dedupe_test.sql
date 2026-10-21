-- Duplicate detection and bump-to-top: the hash index, last_uploaded_at, the image_details view
-- and the bump_image RPC.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000007a1 owns a-old, a-mid and a-new.
-- User B = 00000000-0000-4000-8000-0000000007b1 owns b-1.
-- now() is frozen inside a transaction, so every fixture gets an explicit last_uploaded_at in the
-- past, and a bump (which sets it to now()) is then newer than all of them.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.

begin;

select plan(34);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000007a1', 'dedupe-a@example.test'),
  ('00000000-0000-4000-8000-0000000007b1', 'dedupe-b@example.test');

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256, uploaded_at, last_uploaded_at
)
select
  fixture.user_id,
  fixture.slug || '.jpg',
  format('%s/%s/original.jpg', fixture.user_id, fixture.slug),
  format('%s/%s/thumb.webp', fixture.user_id, fixture.slug),
  'image/jpeg', 1024, 640, 480, repeat(fixture.hash_digit, 64),
  now() - fixture.age, now() - fixture.age
from (
  values
    ('a-old', '00000000-0000-4000-8000-0000000007a1'::uuid, 'a', interval '3 days'),
    ('a-mid', '00000000-0000-4000-8000-0000000007a1'::uuid, 'b', interval '2 days'),
    ('a-new', '00000000-0000-4000-8000-0000000007a1'::uuid, 'c', interval '1 day'),
    ('b-1',   '00000000-0000-4000-8000-0000000007b1'::uuid, 'a', interval '5 days')
) as fixture (slug, user_id, hash_digit, age);

-- image_details hides an image without metadata.
insert into public.image_metadata (image_id, user_id)
select id, user_id from public.images;

create function pg_temp.image_id(slug text)
returns bigint
language sql
as $$ select id from public.images where filename = slug || '.jpg' $$;

-- A can't read B's rows, and anon can't read any, so stash the ids for the statements that run
-- under those roles.
select set_config('tests.a_old_id', pg_temp.image_id('a-old')::text, true);
select set_config('tests.b_1_id', pg_temp.image_id('b-1')::text, true);
select set_config('tests.b_1_last_uploaded_at',
  (select last_uploaded_at::text from public.images where filename = 'b-1.jpg'), true);

-- Structure -------------------------------------------------------------------------------------

select has_index(
  'public', 'images', 'images_user_id_content_sha256_idx', array['user_id', 'content_sha256'],
  'duplicates are looked up through an index on (user_id, content_sha256)'
);
select is(
  (select indisunique from pg_index where indexrelid = 'public.images_user_id_content_sha256_idx'::regclass),
  false,
  'the hash index is not unique: production already holds duplicates'
);
select is(
  (select indexdef from pg_indexes where indexname = 'images_user_id_last_uploaded_at_id_idx'),
  'CREATE INDEX images_user_id_last_uploaded_at_id_idx ON public.images USING btree (user_id, last_uploaded_at DESC, id DESC)',
  'the gallery order has an index on (user_id, last_uploaded_at desc, id desc)'
);
select lives_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256
    )
    select user_id, 'a-old-copy.jpg',
      format('%s/a-old-copy/original.jpg', user_id), format('%s/a-old-copy/thumb.webp', user_id),
      mime_type, size_bytes, width, height, content_sha256
    from public.images where filename = 'a-old.jpg'$$,
  'the same user can still hold two images with one hash'
);
select col_type_is('public', 'images', 'last_uploaded_at', 'timestamp with time zone', 'last_uploaded_at is a timestamptz');
select col_not_null('public', 'images', 'last_uploaded_at', 'last_uploaded_at is not null');
select col_default_is('public', 'images', 'last_uploaded_at', 'now()', 'last_uploaded_at defaults to now()');
select is(
  (select last_uploaded_at = now() from public.images where filename = 'a-old-copy.jpg'),
  true,
  'an insert that does not name last_uploaded_at (the running app) gets the current time'
);
-- Its timestamp equals what a bump sets in this transaction, which would make the order below a tie.
delete from public.images where filename = 'a-old-copy.jpg';

-- image_details gained the column at the end and keeps its options and grants. Only
-- stored_sha256 (#113) has been added after it since.
select is(
  (select array_agg(attname::text order by attnum) from (
     select attname, attnum from pg_attribute
     where attrelid = 'public.image_details'::regclass and attnum > 0 and not attisdropped
     order by attnum desc limit 2) as last_two),
  array['last_uploaded_at', 'stored_sha256'],
  'last_uploaded_at comes right after the original columns, with only stored_sha256 after it'
);
select is(
  (select reloptions from pg_class where oid = 'public.image_details'::regclass),
  array['security_invoker=true'],
  'image_details still runs with the caller''s privileges'
);
select is(has_table_privilege('authenticated', 'public.image_details', 'select'), true,
  'authenticated can still read image_details');
select is(has_table_privilege('anon', 'public.image_details', 'select'), false,
  'anon still can''t read image_details');

-- Backfill --------------------------------------------------------------------------------------

-- The migration backfills with this statement, so run it against rows that look like the ones
-- production has: last_uploaded_at empty, uploaded_at all different. The rollback undoes it.
alter table public.images alter column last_uploaded_at drop default;
alter table public.images alter column last_uploaded_at drop not null;
update public.images set last_uploaded_at = null;
update public.images set last_uploaded_at = uploaded_at;

select is(
  (select count(*) from public.images where last_uploaded_at is distinct from uploaded_at),
  0::bigint,
  'the backfill copies uploaded_at into last_uploaded_at for every existing row'
);
select is(
  (select array_agg(id order by last_uploaded_at desc, id desc) from public.images where user_id = '00000000-0000-4000-8000-0000000007a1'),
  (select array_agg(id order by uploaded_at desc, id desc) from public.images where user_id = '00000000-0000-4000-8000-0000000007a1'),
  'the backfill keeps the gallery order'
);

alter table public.images alter column last_uploaded_at set default now();
alter table public.images alter column last_uploaded_at set not null;

-- Grants on the function ------------------------------------------------------------------------

select function_returns('public', 'bump_image', array['bigint'], 'boolean', 'bump_image returns boolean');
select is_definer('public', 'bump_image', array['bigint'], 'bump_image is security definer');
select is(
  (select proconfig from pg_proc where oid = 'public.bump_image(bigint)'::regprocedure),
  array['search_path=""'],
  'bump_image pins an empty search_path'
);
select results_eq(
  $$values
    ('anon', has_function_privilege('anon', 'public.bump_image(bigint)', 'execute')),
    ('authenticated', has_function_privilege('authenticated', 'public.bump_image(bigint)', 'execute')),
    ('service_role', has_function_privilege('service_role', 'public.bump_image(bigint)', 'execute'))$$,
  $$values ('anon', false), ('authenticated', true), ('service_role', false)$$,
  'only authenticated can execute bump_image'
);
select is(
  (select count(*) from pg_proc p, aclexplode(p.proacl) a
   where p.oid = 'public.bump_image(bigint)'::regprocedure and a.grantee = 0),
  0::bigint,
  'PUBLIC has no execute right on bump_image'
);

-- Bumping as A ----------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000007a1", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.image_details order by last_uploaded_at desc, id desc$$,
  $$values ('a-new.jpg'), ('a-mid.jpg'), ('a-old.jpg')$$,
  'before the bump the order is the upload order, newest first'
);
select is(
  public.bump_image(current_setting('tests.a_old_id')::bigint),
  true,
  'A bumps their own image'
);
select results_eq(
  $$select filename::text from public.image_details order by last_uploaded_at desc, id desc$$,
  $$values ('a-old.jpg'), ('a-new.jpg'), ('a-mid.jpg')$$,
  'the bumped image moves to the top of A''s image_details order'
);
select is(
  (select last_uploaded_at = now() and uploaded_at = now() - interval '3 days'
   from public.image_details where filename = 'a-old.jpg'),
  true,
  'the bump sets last_uploaded_at to now and leaves uploaded_at at the first upload'
);
select is(
  public.bump_image(current_setting('tests.a_old_id')::bigint),
  true,
  'bumping the same image again is allowed'
);
select is(
  public.bump_image(current_setting('tests.b_1_id')::bigint),
  false,
  'A can''t bump B''s image'
);
select is(
  public.bump_image(-1),
  false,
  'bumping an image that does not exist returns false'
);
select is(
  public.bump_image(null),
  false,
  'bumping null returns false'
);
select throws_ok(
  $$update public.images set last_uploaded_at = now()$$,
  '42501', 'permission denied for table images',
  'images stay immutable: authenticated still has no UPDATE'
);

reset role;

select is(
  (select last_uploaded_at::text from public.images where filename = 'b-1.jpg'),
  current_setting('tests.b_1_last_uploaded_at'),
  'B''s image was not changed by A''s attempt'
);

-- Bumping as B ----------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000007b1", "role": "authenticated"}';

select is(
  public.bump_image(current_setting('tests.b_1_id')::bigint),
  true,
  'B bumps their own image'
);
select is(
  public.bump_image(current_setting('tests.a_old_id')::bigint),
  false,
  'B can''t bump A''s image'
);

reset role;

select is(
  (select last_uploaded_at = now() from public.images where filename = 'b-1.jpg'),
  true,
  'B''s bump set last_uploaded_at to the current time'
);

-- Other roles -----------------------------------------------------------------------------------

set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  $$select public.bump_image(current_setting('tests.a_old_id')::bigint)$$,
  '42501', 'permission denied for function bump_image',
  'anon can''t execute bump_image'
);

reset role;
set local role service_role;

select throws_ok(
  $$select public.bump_image(current_setting('tests.a_old_id')::bigint)$$,
  '42501', 'permission denied for function bump_image',
  'service_role can''t execute bump_image'
);

reset role;

select * from finish();

rollback;
