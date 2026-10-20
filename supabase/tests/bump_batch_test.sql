-- The bump_images RPC: several images moved to the top of the gallery in one call, in array order.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000076a1 owns a-1 .. a-4, uploaded a-1 first and a-4 last.
-- User B = 00000000-0000-4000-8000-0000000076b1 owns b-1 and b-2.
-- now() is frozen inside a transaction, so every fixture gets an explicit last_uploaded_at in the
-- past, and a bump (which sets it to now() plus a few microseconds) is then newer than all of them.
-- Two bumps in this one transaction would share that now(), so each scenario starts from the
-- fixture times again (pg_temp.rewind) and bumps once.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.

begin;

select plan(29);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000076a1', 'bump-batch-a@example.test'),
  ('00000000-0000-4000-8000-0000000076b1', 'bump-batch-b@example.test');

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
    ('a-1', '00000000-0000-4000-8000-0000000076a1'::uuid, '1', interval '4 days'),
    ('a-2', '00000000-0000-4000-8000-0000000076a1'::uuid, '2', interval '3 days'),
    ('a-3', '00000000-0000-4000-8000-0000000076a1'::uuid, '3', interval '2 days'),
    ('a-4', '00000000-0000-4000-8000-0000000076a1'::uuid, '4', interval '1 day'),
    ('b-1', '00000000-0000-4000-8000-0000000076b1'::uuid, '5', interval '6 days'),
    ('b-2', '00000000-0000-4000-8000-0000000076b1'::uuid, '6', interval '5 days')
) as fixture (slug, user_id, hash_digit, age);

create function pg_temp.image_id(slug text)
returns bigint
language sql
as $$ select id from public.images where filename = slug || '.jpg' $$;

-- The ids behind some slugs, in the order given, for passing to bump_images.
create function pg_temp.ids(variadic slugs text[])
returns bigint[]
language sql
as $$
  select array_agg(pg_temp.image_id(s.slug) order by s.ord)
  from unnest(slugs) with ordinality as s(slug, ord)
$$;

-- The caller's images as slugs, newest first, which is how the grid sorts them. Under a user's
-- role, RLS limits it to their own rows.
create function pg_temp.gallery_order()
returns text[]
language sql
as $$
  select array_agg(replace(filename, '.jpg', '') order by last_uploaded_at desc, id desc)
  from public.images
$$;

-- Puts every image back at its upload time, whichever role is active.
create function pg_temp.rewind()
returns void
language sql
security definer
as $$ update public.images set last_uploaded_at = uploaded_at $$;

-- A can't read B's rows, and anon can't read any, so stash the ids for statements under those roles.
select set_config('tests.a_1_id', pg_temp.image_id('a-1')::text, true);
select set_config('tests.b_1_id', pg_temp.image_id('b-1')::text, true);

-- Structure -------------------------------------------------------------------------------------

select function_returns(
  'public', 'bump_images', array['bigint[]'], 'setof bigint', 'bump_images returns the bumped ids'
);
select is_definer('public', 'bump_images', array['bigint[]'], 'bump_images is security definer');
select is(
  (select proconfig from pg_proc where oid = 'public.bump_images(bigint[])'::regprocedure),
  array['search_path=""'],
  'bump_images pins an empty search_path'
);
select results_eq(
  $$select r.role, has_function_privilege(r.role, 'public.bump_images(bigint[])', 'execute')
    from (values ('anon'), ('authenticated'), ('service_role')) as r(role) order by 1$$,
  $$values ('anon', false), ('authenticated', true), ('service_role', false)$$,
  'only authenticated can execute bump_images'
);
select is(
  (select count(*) from pg_proc p, aclexplode(p.proacl) a
   where p.oid = 'public.bump_images(bigint[])'::regprocedure and a.grantee = 0),
  0::bigint,
  'PUBLIC has no execute right on bump_images'
);

-- A batch keeps the array order ------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000076a1", "role": "authenticated"}';

select is(
  pg_temp.gallery_order(),
  array['a-4', 'a-3', 'a-2', 'a-1'],
  'before any bump the order is the upload order, newest first'
);
select is(
  array(select public.bump_images(pg_temp.ids('a-1', 'a-2', 'a-3'))),
  pg_temp.ids('a-1', 'a-2', 'a-3'),
  'it returns the bumped ids in the order they were given'
);
select is(
  pg_temp.gallery_order(),
  array['a-3', 'a-2', 'a-1', 'a-4'],
  'the last id of the batch ends up on top, then the one before it'
);
select is(
  (select array_agg(last_uploaded_at - now() order by filename)
   from public.images where filename in ('a-1.jpg', 'a-2.jpg', 'a-3.jpg')),
  array[interval '1 microsecond', interval '2 microseconds', interval '3 microseconds'],
  'each image is stamped now() plus its position in the array, in microseconds'
);
select is(
  (select array_agg(now() - uploaded_at order by filename) from public.images),
  array[interval '4 days', interval '3 days', interval '2 days', interval '1 day'],
  'uploaded_at stays at the first upload'
);

-- The order follows the array, not the ids.
select pg_temp.rewind();
select is(
  array(select public.bump_images(pg_temp.ids('a-4', 'a-1'))),
  pg_temp.ids('a-4', 'a-1'),
  'another batch returns its ids in the order given'
);
select is(
  pg_temp.gallery_order(),
  array['a-1', 'a-4', 'a-3', 'a-2'],
  'a-1 is on top though a-4 has the higher id, because it came last'
);

-- An id given twice is bumped once, at its last position.
select pg_temp.rewind();
select is(
  array(select public.bump_images(pg_temp.ids('a-2', 'a-3', 'a-2'))),
  pg_temp.ids('a-3', 'a-2'),
  'an id repeated in the array comes back once, at its last position'
);
select is(
  pg_temp.gallery_order(),
  array['a-2', 'a-3', 'a-4', 'a-1'],
  'the repeated id ends up on top'
);

-- Ids that are not the caller's, or not there, are skipped ---------------------------------------

select pg_temp.rewind();
select is(
  array(select public.bump_images(array[
    pg_temp.image_id('a-4'), pg_temp.image_id('b-1'), -1, null, pg_temp.image_id('a-1')
  ])),
  pg_temp.ids('a-4', 'a-1'),
  'B''s image, a missing id and a null are skipped, and only the bumped ids come back'
);
select is(
  pg_temp.gallery_order(),
  array['a-1', 'a-4', 'a-3', 'a-2'],
  'the skipped ids did not disturb the rest of the order'
);

select pg_temp.rewind();
select is(
  array(select public.bump_images(pg_temp.ids('b-1', 'b-2'))),
  array[]::bigint[],
  'a batch of only B''s images returns nothing'
);
select is(
  pg_temp.gallery_order(),
  array['a-4', 'a-3', 'a-2', 'a-1'],
  'and changes nothing for A'
);
select is(
  array(select public.bump_images(array[]::bigint[])),
  array[]::bigint[],
  'an empty array returns nothing'
);
select is(
  array(select public.bump_images(null)),
  array[]::bigint[],
  'a null array returns nothing'
);
select throws_ok(
  $$update public.images set last_uploaded_at = now()$$,
  '42501', 'permission denied for table images',
  'images stay immutable: authenticated still has no UPDATE'
);

reset role;

select is(
  (select array_agg(last_uploaded_at = uploaded_at order by filename)
   from public.images where filename like 'b-%'),
  array[true, true],
  'B''s images were not changed by A''s batches'
);

-- Bumping as B ----------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000076b1", "role": "authenticated"}';

select is(
  array(select public.bump_images(array[
    current_setting('tests.a_1_id')::bigint, pg_temp.image_id('b-1'), pg_temp.image_id('b-2')
  ])),
  pg_temp.ids('b-1', 'b-2'),
  'B bumps their own images, and A''s id in the same batch is skipped'
);
select is(
  pg_temp.gallery_order(),
  array['b-2', 'b-1'],
  'B''s last id is on top'
);

reset role;

select is(
  (select array_agg(last_uploaded_at = uploaded_at order by filename)
   from public.images where filename like 'a-%'),
  array[true, true, true, true],
  'A''s images were not changed by B''s batch'
);

-- Other roles -----------------------------------------------------------------------------------

select pg_temp.rewind();
set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  $$select public.bump_images(array[current_setting('tests.a_1_id')::bigint])$$,
  '42501', 'permission denied for function bump_images',
  'anon can''t execute bump_images'
);

reset role;
set local role service_role;

select throws_ok(
  $$select public.bump_images(array[current_setting('tests.a_1_id')::bigint])$$,
  '42501', 'permission denied for function bump_images',
  'service_role can''t execute bump_images'
);

reset role;

select is(
  (select count(*) from public.images where last_uploaded_at <> uploaded_at),
  0::bigint,
  'the refused calls changed nothing'
);

-- Without a signed-in user, auth.uid() is null and nothing matches, whoever the caller is.
select is(
  (select count(*) from public.bump_images(array[current_setting('tests.a_1_id')::bigint])),
  0::bigint,
  'with no auth.uid() nothing is bumped'
);

select * from finish();

rollback;
