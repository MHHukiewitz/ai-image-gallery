-- Semantic text search: search_images_semantic and its three constants
-- (20261020000010_semantic_text_search.sql). Run with `supabase test db`. Everything happens in one
-- transaction that rolls back.
--
-- Embeddings here are synthetic: unit(c) is the 384-dimensional unit vector whose cosine with the query
-- vector (1, 0, 0, ...) is exactly c. The floor is 0.65 and the minimum 0.20, so an image is a
-- candidate by meaning from a cosine of 0.72 up, and its similarity is (c - 0.65) / 0.35. The weights
-- are 1 for the word match and 0.5 for the meaning, so similarity 0.4286 adds a score of 0.2143.
-- User A owns (the query is "puppy"):
--   both         tags {puppy, dog}, c = 0.8: matches the words and the meaning.
--   plain        tags {puppy}, no embedding yet: found by words alone.
--   exact        tags {puppy}, c = 0 (unrelated meaning): the exact-tag boost, nothing from meaning.
--   word         tag {sofa}, "A puppy asleep." as the description, c = 0: a weaker word match.
--   strong       tags {dog}, c = 0.9: a strong match by meaning only.
--   semantic     tags {dog}, c = 0.8: a match by meaning only.
--   semantic-twin the same as semantic, uploaded later: a score tie to page through.
--   near-in      tags {hound}, c = 0.74: just above the minimum.
--   near-out     tags {tractor}, c = 0.70: just below it, so left out.
--   unrelated    tags {tractor}, c = 0: left out.
--   opposite     tags {truck}, c = -1: its similarity clamps at 0, so left out.
--   pending      tags {pending}, no embedding and no word match: left out.
-- User B owns b-both and b-semantic, which would match "puppy" for B but must never show up for A.

begin;

select plan(48);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

-- The 384-dimensional vector (x, y, 0, 0, ...).
create function pg_temp.vec(x float8, y float8) returns extensions.vector
language sql immutable as $$
  select (array[x, y] || array_fill(0::float8, array[382]))::extensions.vector(384)
$$;

-- The unit vector whose cosine with the query vector (1, 0) is c.
create function pg_temp.unit(c float8) returns extensions.vector
language sql immutable as $$
  select pg_temp.vec(c, sqrt(1 - c * c))
$$;

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000000f1', 'semantic-a@example.test'),
  ('00000000-0000-4000-8000-0000000000f2', 'semantic-b@example.test');

create temp table fixture (
  slug text,
  user_id uuid,
  hours_after_start int,
  description text,
  tags text[],
  cosine float8
) on commit drop;

insert into fixture
values
  ('both', '00000000-0000-4000-8000-0000000000f1', 0, null, '{puppy,dog}', 0.8),
  ('plain', '00000000-0000-4000-8000-0000000000f1', 1, null, '{puppy}', null),
  ('exact', '00000000-0000-4000-8000-0000000000f1', 0, null, '{puppy}', 0),
  ('word', '00000000-0000-4000-8000-0000000000f1', 2, 'A puppy asleep.', '{sofa}', 0),
  ('strong', '00000000-0000-4000-8000-0000000000f1', 3, null, '{dog}', 0.9),
  ('semantic', '00000000-0000-4000-8000-0000000000f1', 4, null, '{dog}', 0.8),
  ('semantic-twin', '00000000-0000-4000-8000-0000000000f1', 5, null, '{dog}', 0.8),
  ('near-in', '00000000-0000-4000-8000-0000000000f1', 6, null, '{hound}', 0.74),
  ('near-out', '00000000-0000-4000-8000-0000000000f1', 7, null, '{tractor}', 0.7),
  ('unrelated', '00000000-0000-4000-8000-0000000000f1', 8, null, '{tractor}', 0),
  ('opposite', '00000000-0000-4000-8000-0000000000f1', 9, null, '{truck}', -1),
  ('pending', '00000000-0000-4000-8000-0000000000f1', 10, null, '{pending}', null),
  ('b-both', '00000000-0000-4000-8000-0000000000f2', 0, null, '{puppy,dog}', 0.8),
  ('b-semantic', '00000000-0000-4000-8000-0000000000f2', 1, null, '{dog}', 0.8);

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256, uploaded_at
)
select
  f.user_id,
  f.slug || '.jpg',
  format('%s/%s/original.jpg', f.user_id, f.slug),
  format('%s/%s/thumb.webp', f.user_id, f.slug),
  'image/jpeg', 1024, 640, 480, repeat('f', 64),
  timestamptz '2026-10-01 12:00:00+00' + f.hours_after_start * interval '1 hour'
from fixture f
order by f.user_id, f.hours_after_start, f.slug;

insert into public.image_metadata (
  image_id, user_id, description, tags, ai_processing_status, embedding, embedding_model
)
select
  i.id, f.user_id, f.description, f.tags, 'done',
  case when f.cosine is null then null else pg_temp.unit(f.cosine) end,
  case when f.cosine is null then null else 'test-model' end
from fixture f
join public.images i on i.filename = f.slug || '.jpg';

-- Statements run as a signed-in user can't see pg_temp, so the query vectors go through settings.
select set_config('tests.query', pg_temp.vec(1, 0)::text, true);
select set_config('tests.zero', pg_temp.vec(0, 0)::text, true);

-- Tunables --------------------------------------------------------------------------------------

select is(public.search_semantic_min(), 0.2::float8, 'the semantic minimum is 0.20');
select is(public.search_text_weight(), 1::float8, 'the text weight is 1');
select is(public.search_semantic_weight(), 0.5::float8, 'the semantic weight is 0.5');
select ok(
  (select count(*) = 3 and bool_and(p.provolatile = 'i' and p.proparallel = 's')
   from pg_proc p
   where p.oid in (
     'public.search_semantic_min()'::regprocedure,
     'public.search_text_weight()'::regprocedure,
     'public.search_semantic_weight()'::regprocedure
   )),
  'the constants are immutable and parallel safe, like the other constants'
);
select ok(
  (select bool_and(
     has_function_privilege('authenticated', f, 'execute')
       and has_function_privilege('service_role', f, 'execute')
       and not has_function_privilege('anon', f, 'execute'))
   from unnest(array[
     'public.search_semantic_min()', 'public.search_text_weight()', 'public.search_semantic_weight()'
   ]) as f),
  'the constants are executable by authenticated and service_role, not anon'
);
select is(
  (select count(*)::int
   from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('search_semantic_min', 'search_text_weight', 'search_semantic_weight',
                       'search_images_semantic')
     and acl.grantee = 0),
  0,
  'neither the constants nor the search function is executable by PUBLIC'
);

-- The function: structure and grants ------------------------------------------------------------

select ok(
  (select not p.prosecdef and p.provolatile = 's' and p.prolang = (select oid from pg_language where lanname = 'sql')
   from pg_proc p
   where p.oid = 'public.search_images_semantic(text, extensions.vector, int, int)'::regprocedure),
  'search_images_semantic is a stable SQL function that runs as the caller (security invoker)'
);
select is(
  (select array_agg(arg.name order by arg.ord)
   from pg_proc p, unnest(p.proargnames) with ordinality as arg(name, ord)
   where p.oid = 'public.search_images_semantic(text, extensions.vector, int, int)'::regprocedure
     and arg.ord <= 4),
  array['q', 'q_embedding', 'lim', 'off'],
  'the arguments are named q, q_embedding, lim and off'
);
select is(
  (select p.pronargdefaults::int
   from pg_proc p
   where p.oid = 'public.search_images_semantic(text, extensions.vector, int, int)'::regprocedure),
  2,
  'lim and off have defaults'
);
select is(
  pg_get_function_result('public.search_images_semantic(text, extensions.vector, int, int)'::regprocedure),
  pg_get_function_result('public.search_images(text, int, int)'::regprocedure),
  'it returns the same columns as search_images, in the same order'
);
select is(
  (select count(*)::int from pg_proc where pronamespace = 'public'::regnamespace and proname = 'search_images'),
  1,
  'search_images has no overload, so PostgREST can still pick it by name'
);
select ok(
  has_function_privilege('authenticated', 'public.search_images_semantic(text, extensions.vector, int, int)', 'execute')
    and not has_function_privilege('anon', 'public.search_images_semantic(text, extensions.vector, int, int)', 'execute')
    and not has_function_privilege('service_role', 'public.search_images_semantic(text, extensions.vector, int, int)', 'execute'),
  'only authenticated can execute it'
);

-- Search by meaning and by words (as A) ----------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000f1", "role": "authenticated"}';

select results_eq(
  $$select r.filename::text
    from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
      with ordinality as r
    order by r.ordinality$$,
  array[
    'both.jpg',          -- words + meaning + exact tag: 0.5 + 0.5 + 0.2143
    'plain.jpg',         -- words + exact tag: 0.5 + 0.5. Equal to exact, but uploaded later.
    'exact.jpg',         -- words + exact tag: 0.5 + 0.5
    'strong.jpg',        -- meaning only, c = 0.9: 0.5 * 0.7143 = 0.3571
    'word.jpg',          -- a description match: 0.2857
    'semantic-twin.jpg', -- meaning only, c = 0.8: 0.2143. Equal to semantic, but uploaded later.
    'semantic.jpg',
    'near-in.jpg'        -- meaning only, c = 0.74: 0.5 * 0.2571 = 0.1286
  ],
  'results are the word matches and the meaning matches, best first'
);
select ok(
  not exists (select 1 from public.search_images('puppy', 100, 0) where filename = 'strong.jpg'),
  'the strong match shares no word with the query, so search_images does not find it'
);
select ok(
  exists (
    select 1
    from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
    where filename = 'strong.jpg'
  ),
  'a match by meaning alone is returned'
);
select ok(
  (select filename from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 1, 0))
    = 'both.jpg',
  'an image with the exact tag and the closest meaning ranks first'
);
select ok(
  (select max(score) from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
   where filename in ('strong.jpg', 'semantic.jpg', 'near-in.jpg'))
    < (select min(score) from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
       where filename in ('exact.jpg', 'plain.jpg')),
  'an exact tag match outranks even a strong match by meaning'
);
select ok(
  not exists (
    select 1
    from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
    where filename in ('near-out.jpg', 'unrelated.jpg', 'opposite.jpg', 'pending.jpg')
  ),
  'an unrelated image is left out: below the minimum, opposite, or with nothing to compare'
);
select ok(
  exists (
    select 1 from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
    where filename = 'near-in.jpg'
  )
    and not exists (
      select 1 from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
      where filename = 'near-out.jpg'
    ),
  'the minimum falls between a cosine of 0.70 (out) and 0.74 (in)'
);
select ok(
  abs((select score from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
       where filename = 'semantic.jpg')
      - public.search_semantic_weight() * (0.8 - public.search_embedding_floor()) / (1 - public.search_embedding_floor()))
    < 1e-6,
  'a meaning-only score is the semantic weight times the calibrated similarity'
);
select ok(
  abs((select score from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
       where filename = 'both.jpg')
      - (select score from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
         where filename = 'exact.jpg')
      - public.search_semantic_weight() * (0.8 - public.search_embedding_floor()) / (1 - public.search_embedding_floor()))
    < 1e-6,
  'meaning adds to the score of a word match'
);
select ok(
  abs((select score from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
       where filename = 'exact.jpg')
      - (public.search_text_weight() * (select r / (r + 1) from (
           select score - public.search_exact_tag_boost() as r
           from public.search_images('puppy', 100, 0) where filename = 'exact.jpg') as lexical)
         + public.search_exact_tag_boost()))
    < 1e-6,
  'a word match scores the text weight times rank / (rank + 1), plus the exact-tag boost'
);
select results_eq(
  $$select r.filename::text
    from public.search_images_semantic('  PUPPY ', current_setting('tests.query')::extensions.vector, 100, 0)
      with ordinality as r
    order by r.ordinality$$,
  $$select r.filename::text
    from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
      with ordinality as r
    order by r.ordinality$$,
  'search ignores case and surrounding spaces'
);
select results_eq(
  $$select filename::text from public.search_images_semantic('puppy dog', current_setting('tests.query')::extensions.vector, 100, 0)
    order by filename collate "C"$$,
  array['both.jpg', 'near-in.jpg', 'semantic-twin.jpg', 'semantic.jpg', 'strong.jpg'],
  'every word of the query must match, but a meaning match needs no words'
);
select results_eq(
  $$select filename::text from public.search_images_semantic('the', current_setting('tests.query')::extensions.vector, 100, 0)
    order by filename collate "C"$$,
  array['both.jpg', 'near-in.jpg', 'semantic-twin.jpg', 'semantic.jpg', 'strong.jpg'],
  'a query of stop words only has no word match but still finds by meaning'
);

-- Without an embedding it is a search by words ----------------------------------------------------

select results_eq(
  $$select r.filename::text
    from public.search_images_semantic('puppy', null, 100, 0) with ordinality as r
    order by r.ordinality$$,
  array['plain.jpg', 'exact.jpg', 'both.jpg', 'word.jpg'],
  'with a NULL embedding, only the word matches are returned (both ties with plain and exact)'
);
select results_eq(
  $$select r.filename::text
    from public.search_images_semantic('puppy', current_setting('tests.zero')::extensions.vector, 100, 0) with ordinality as r
    order by r.ordinality$$,
  array['plain.jpg', 'exact.jpg', 'both.jpg', 'word.jpg'],
  'an all-zero embedding counts as none, because its cosine distance would be NaN'
);
select is(
  (select count(*)::int from public.search_images_semantic('puppy', current_setting('tests.zero')::extensions.vector, 100, 0)
   where score = 'NaN'),
  0,
  'no score is NaN'
);
select is_empty(
  $$select id from public.search_images_semantic('   ', current_setting('tests.query')::extensions.vector, 100, 0)$$,
  'blank input returns nothing'
);
select is_empty(
  $$select id from public.search_images_semantic('!?', current_setting('tests.query')::extensions.vector, 100, 0)$$,
  'input without a word returns nothing'
);
select is_empty(
  $$select id from public.search_images_semantic(null, current_setting('tests.query')::extensions.vector, 100, 0)$$,
  'a NULL query returns nothing'
);
select throws_ok(
  $$select id from public.search_images_semantic('puppy', '[1,0,0]'::extensions.vector, 100, 0)$$,
  '22000', null::text,
  'a vector of the wrong size is an error'
);

-- Pagination: pages must add up to the full ordered list, including score ties ---------------------

select is(
  (select array_agg(r.id order by r.ordinality)
   from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0) with ordinality as r),
  (select array_agg(r.id order by r.page * 100 + r.ordinality)
   from (
     select 1 as page, s.id, s.ordinality
       from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, 0) with ordinality as s
     union all select 2, s.id, s.ordinality
       from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, 3) with ordinality as s
     union all select 3, s.id, s.ordinality
       from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, 6) with ordinality as s
   ) as r),
  'pages of the semantic search concatenate to the full list'
);
select results_eq(
  $$select filename::text from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, 5)$$,
  array['semantic-twin.jpg', 'semantic.jpg', 'near-in.jpg'],
  'a page across a score tie is the same every time: newest first among equals'
);
select is(
  (select count(*)::int from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 2, 100)),
  0,
  'a page past the end is empty'
);
select is(
  (select count(*)::int from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 0, 0)),
  0,
  'lim 0 returns nothing'
);
select is(
  (select count(*)::int from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, -3, 0)),
  0,
  'a negative lim returns nothing'
);
select is(
  (select array_agg(r.id order by r.ordinality)
   from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, -5) with ordinality as r),
  (select array_agg(r.id order by r.ordinality)
   from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 3, 0) with ordinality as r),
  'a negative off counts as 0'
);
select is(
  (select count(*)::int from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, null, 0)),
  0,
  'a NULL lim returns nothing'
);

-- The columns --------------------------------------------------------------------------------------

select results_eq(
  $$select id, user_id, filename::text, tags, ai_processing_status::text
    from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 1, 0)$$,
  $$select i.id, i.user_id, i.filename::text, m.tags, m.ai_processing_status::text
    from public.images i join public.image_metadata m on m.image_id = i.id
    where i.filename = 'both.jpg'$$,
  'a result row carries the image and its metadata'
);

-- Isolation: A never sees B's images ---------------------------------------------------------------

select is(
  (select count(*)::int
   from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
   where user_id <> '00000000-0000-4000-8000-0000000000f1'),
  0,
  'every result belongs to A'
);
select ok(
  not exists (
    select 1 from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
    where filename in ('b-both.jpg', 'b-semantic.jpg')
  ),
  'A can''t find B''s images by their words or by their meaning'
);

reset role;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000f2", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.search_images_semantic('puppy', current_setting('tests.query')::extensions.vector, 100, 0)
    order by filename collate "C"$$,
  array['b-both.jpg', 'b-semantic.jpg'],
  'B finds only B''s images, by words and by meaning'
);
select is_empty(
  $$select id from public.search_images_semantic('hound', current_setting('tests.query')::extensions.vector, 100, 0)
    where filename = 'near-in.jpg'$$,
  'B can''t find A''s image by its tag'
);

reset role;

-- Who may call it -------------------------------------------------------------------------------------

set local role anon;

select throws_ok(
  format($$select id from public.search_images_semantic('puppy', %L::extensions.vector, 20, 0)$$, current_setting('tests.query')),
  '42501', 'permission denied for function search_images_semantic',
  'anon cannot call it'
);

reset role;
set local role service_role;

select throws_ok(
  format($$select id from public.search_images_semantic('puppy', %L::extensions.vector, 20, 0)$$, current_setting('tests.query')),
  '42501', 'permission denied for function search_images_semantic',
  'service_role cannot call it, because it would read every tenant''s images'
);

reset role;

-- search_images itself is untouched ------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000f1", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.search_images('puppy', 100, 0) order by filename collate "C"$$,
  array['both.jpg', 'exact.jpg', 'plain.jpg', 'word.jpg'],
  'search_images still finds by words only'
);
select is(
  (select pronargs::int from pg_proc where oid = 'public.search_images(text, int, int)'::regprocedure),
  3,
  'search_images still takes q, lim and off'
);

reset role;

select * from finish();

rollback;
