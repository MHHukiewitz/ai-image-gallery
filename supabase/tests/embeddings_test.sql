-- Image embeddings: the pgvector extension and columns, store_image_embedding, the floor constant, the
-- stale-embedding trigger, and similar_images scoring on embeddings with a fallback to tag_lexemes
-- (20261020000008_image_embeddings.sql). Run with `supabase test db`. Everything happens in one
-- transaction that rolls back.
--
-- Embeddings here are synthetic: vec(x, y) is the 384-dimensional vector with x and y in the first two
-- dimensions, so the cosine of two of them is plain 2D geometry. No fixture has colors, so a score is
-- 0.7 · semantic (the color term is 0 for an empty palette). The floor is 0.65, so
-- semantic = (cosine - 0.65) / 0.35, clamped at 0.
-- Tags are chosen to disagree with the embeddings, which shows that embeddings win when both images
-- have one. User A owns:
--   target        {alpha}, vec(1, 0)
--   near          {omega}, vec(1, 0.05): cosine 0.99875 and no shared tag, so it is found by meaning alone.
--   mid           {sigma}, vec(0.8, 0.6): cosine 0.8, so semantic 0.4286 and score 0.3.
--   far           {alpha}, vec(0, 1): cosine 0, so it scores 0 and is left out despite the shared tag.
--   plain         {alpha}, no embedding: falls back to tag_cosine, 1, so it scores 0.7.
--   plain-other   {omega}, no embedding: falls back to tag_cosine, 0, so it is left out.
--   other-model   {alpha}, vec(0, 1) from another model: not comparable, so it falls back to tags and scores 0.7.
--   bare-target   {alpha, omega}, no embedding: scores 0.7 / sqrt(2) against the target's tags.
-- User B owns b-near {alpha} vec(1, 0), b-twin {omega} vec(1, 0) and b-plain {alpha}, none of which may show up for A.

begin;

select plan(65);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

-- The 384-dimensional vector (x, y, 0, 0, ...). Its length is x² + y², so vec(1, 0) is a unit vector.
create function pg_temp.vec(x float8, y float8) returns extensions.vector
language sql immutable as $$
  select (array[x, y] || array_fill(0::float8, array[382]))::extensions.vector(384)
$$;

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000000e1', 'embed-a@example.test'),
  ('00000000-0000-4000-8000-0000000000e2', 'embed-b@example.test');

create temp table fixture (
  slug text,
  user_id uuid,
  hours_after_start int,
  tags text[],
  x float8,
  y float8,
  model text
) on commit drop;

insert into fixture
values
  ('target', '00000000-0000-4000-8000-0000000000e1', 0, '{alpha}', 1, 0, 'test-model'),
  ('near', '00000000-0000-4000-8000-0000000000e1', 1, '{omega}', 1, 0.05, 'test-model'),
  ('mid', '00000000-0000-4000-8000-0000000000e1', 2, '{sigma}', 0.8, 0.6, 'test-model'),
  ('far', '00000000-0000-4000-8000-0000000000e1', 3, '{alpha}', 0, 1, 'test-model'),
  ('plain', '00000000-0000-4000-8000-0000000000e1', 4, '{alpha}', null, null, null),
  ('plain-other', '00000000-0000-4000-8000-0000000000e1', 5, '{omega}', null, null, null),
  ('other-model', '00000000-0000-4000-8000-0000000000e1', 6, '{alpha}', 0, 1, 'other-model'),
  ('bare-target', '00000000-0000-4000-8000-0000000000e1', 7, '{alpha,omega}', null, null, null),
  ('b-near', '00000000-0000-4000-8000-0000000000e2', 0, '{alpha}', 1, 0, 'test-model'),
  ('b-twin', '00000000-0000-4000-8000-0000000000e2', 1, '{omega}', 1, 0, 'test-model'),
  ('b-plain', '00000000-0000-4000-8000-0000000000e2', 2, '{alpha}', null, null, null);

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256, uploaded_at
)
select
  f.user_id,
  f.slug || '.jpg',
  format('%s/%s/original.jpg', f.user_id, f.slug),
  format('%s/%s/thumb.webp', f.user_id, f.slug),
  'image/jpeg', 1024, 640, 480, repeat('e', 64),
  timestamptz '2026-10-01 12:00:00+00' + f.hours_after_start * interval '1 hour'
from fixture f
order by f.user_id, f.hours_after_start;

insert into public.image_metadata (image_id, user_id, tags, ai_processing_status, embedding, embedding_model)
select
  i.id, f.user_id, f.tags, 'done',
  case when f.x is null then null else pg_temp.vec(f.x, f.y) end,
  f.model
from fixture f
join public.images i on i.filename = f.slug || '.jpg';

-- Ids are generated, and statements run as A can't look up B's rows, so remember the ones they name.
select set_config('tests.target_id', id::text, true) from public.images where filename = 'target.jpg';
select set_config('tests.bare_target_id', id::text, true) from public.images where filename = 'bare-target.jpg';
select set_config('tests.mid_id', id::text, true) from public.images where filename = 'mid.jpg';
select set_config('tests.b_near_id', id::text, true) from public.images where filename = 'b-near.jpg';

-- The extension and the columns -------------------------------------------------------------------

select has_extension('extensions', 'vector', 'pgvector is installed in the extensions schema');
select col_type_is(
  'public', 'image_metadata', 'embedding', 'extensions', 'vector(384)',
  'embedding is a 384-dimensional vector'
);
select col_is_null('public', 'image_metadata', 'embedding', 'embedding is nullable, because rows start without one');
select col_type_is('public', 'image_metadata', 'embedding_model', 'text', 'embedding_model is text');
select col_is_null('public', 'image_metadata', 'embedding_model', 'embedding_model is nullable');
select is(
  (select count(*) from public.image_metadata where embedding is not null)::int, 7,
  'the fixtures with an embedding have one, and the others have none'
);
select throws_ok(
  $$update public.image_metadata set embedding_model = null
    where image_id = (select id from public.images where filename = 'near.jpg')$$,
  '23514', null::text,
  'an embedding without a model id violates the check constraint'
);
select throws_ok(
  $$update public.image_metadata set embedding = pg_temp.vec(1, 0)
    where image_id = (select id from public.images where filename = 'plain.jpg')$$,
  '23514', null::text,
  'a model id without an embedding, and so a vector without a model, violates the check constraint'
);

-- The constant ------------------------------------------------------------------------------------

select is(public.search_embedding_floor(), 0.65::float8, 'the embedding floor is 0.65');
select ok(
  (select p.provolatile = 'i' and p.proparallel = 's' from pg_proc p
   where p.oid = 'public.search_embedding_floor()'::regprocedure),
  'search_embedding_floor is immutable, like the other constants'
);
select ok(
  has_function_privilege('authenticated', 'public.search_embedding_floor()', 'execute')
    and has_function_privilege('service_role', 'public.search_embedding_floor()', 'execute')
    and not has_function_privilege('anon', 'public.search_embedding_floor()', 'execute'),
  'search_embedding_floor is executable by authenticated and service_role, not anon'
);

-- store_image_embedding: who may call it ----------------------------------------------------------

select ok(
  has_function_privilege('service_role', 'public.store_image_embedding(bigint, extensions.vector, text)', 'execute'),
  'service_role can execute store_image_embedding'
);
select ok(
  not has_function_privilege('authenticated', 'public.store_image_embedding(bigint, extensions.vector, text)', 'execute')
    and not has_function_privilege('anon', 'public.store_image_embedding(bigint, extensions.vector, text)', 'execute'),
  'authenticated and anon cannot execute store_image_embedding'
);
select ok(
  (select p.prosecdef and p.proconfig = array['search_path=""']
   from pg_proc p where p.oid = 'public.store_image_embedding(bigint, extensions.vector, text)'::regprocedure),
  'store_image_embedding is security definer with an empty search_path'
);
select is(
  (select count(*)::int from information_schema.routine_privileges
   where routine_schema = 'public' and routine_name = 'store_image_embedding'
     and grantee not in ('postgres', 'service_role')),
  0,
  'no role besides service_role (and the owner) holds EXECUTE on store_image_embedding, PUBLIC included'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000e1", "role": "authenticated"}';

select throws_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'test-model')$$,
         current_setting('tests.target_id')),
  '42501', 'permission denied for function store_image_embedding',
  'a signed-in user cannot store an embedding, not even for their own image'
);

reset role;
set local role anon;

select throws_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'test-model')$$,
         current_setting('tests.target_id')),
  '42501', 'permission denied for function store_image_embedding',
  'anon cannot store an embedding'
);

reset role;

-- store_image_embedding: what it does (as service_role, the worker) ---------------------------------

set local role service_role;

select lives_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'bge-test')$$,
         (select id from public.images where filename = 'plain.jpg')),
  'service_role stores an embedding for an image that had none'
);

reset role;

select is(
  (select m.embedding_model from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'plain.jpg'),
  'bge-test',
  'the model id is stored with the vector'
);
select is(
  (select extensions.vector_dims(m.embedding) from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'plain.jpg'),
  384,
  'the vector is stored with its 384 dimensions'
);

set local role service_role;

select lives_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.25::float8, array[384])::extensions.vector(384), 'bge-test-2')$$,
         (select id from public.images where filename = 'plain.jpg')),
  'storing again replaces the embedding'
);

reset role;

select is(
  (select m.embedding_model from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'plain.jpg'),
  'bge-test-2',
  'the second call replaced the model id'
);

set local role service_role;

select lives_ok(
  $$select public.store_image_embedding(-1, array_fill(0.5::float8, array[384])::extensions.vector(384), 'bge-test')$$,
  'an image that does not exist (a user deleted it mid-analysis) is not an error'
);
select throws_ok(
  $$select public.store_image_embedding(1, '[1,2,3]'::extensions.vector, 'bge-test')$$,
  '22000', 'expected 384 dimensions, not 3',
  'a vector of the wrong size is rejected'
);
select throws_ok(
  $$select public.store_image_embedding(1, array_fill(0::float8, array[384])::extensions.vector(384), 'bge-test')$$,
  '22023', 'an embedding must not be the zero vector',
  'the zero vector is rejected, because its cosine distance is NaN'
);
select throws_ok(
  $$select public.store_image_embedding(1, array_fill(0.5::float8, array[384])::extensions.vector(384), '')$$,
  '22023', 'store_image_embedding needs an embedding and a model id',
  'an empty model id is rejected'
);
select throws_ok(
  $$select public.store_image_embedding(1, array_fill(0.5::float8, array[384])::extensions.vector(384), null)$$,
  '22023', 'store_image_embedding needs an embedding and a model id',
  'a missing model id is rejected'
);
select throws_ok(
  $$select public.store_image_embedding(1, null, 'bge-test')$$,
  '22023', 'store_image_embedding needs an embedding and a model id',
  'a missing embedding is rejected'
);

reset role;

-- Put plain back as the fixture defined it (no embedding), for the similarity tests below.
update public.image_metadata set embedding = null, embedding_model = null
where image_id = (select id from public.images where filename = 'plain.jpg');

-- Similar images (as A) ---------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000e1", "role": "authenticated"}';

select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'near.jpg')
      - 0.7 * ((1 / sqrt(1.0025)) - 0.65) / 0.35) < 1e-5,
  'near scores 0.7 · (cosine - floor) / (1 - floor), where the cosine of vec(1, 0) and vec(1, 0.05) is 1/sqrt(1.0025)'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'mid.jpg')
      - 0.3) < 1e-5,
  'mid, at cosine 0.8, scores 0.7 · 0.15 / 0.35 = 0.3'
);
select ok(
  (select near.score > mid.score
   from public.similar_images(current_setting('tests.target_id')::bigint) near,
        public.similar_images(current_setting('tests.target_id')::bigint) mid
   where near.filename = 'near.jpg' and mid.filename = 'mid.jpg'),
  'a near-identical embedding outranks a distant one'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'far.jpg'$$,
  'an orthogonal embedding scores 0 and is left out, even though far shares the target''s tag'
);
select ok(
  exists (select 1 from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'near.jpg'),
  'near is found by meaning alone, with no tag in common with the target'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'plain.jpg')
      - 0.7) < 1e-9,
  'an image with no embedding falls back to tag_cosine on tag_lexemes: the shared tag scores 0.7'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'plain-other.jpg'$$,
  'an image with no embedding and no shared word is left out'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'bare-target.jpg')
      - 0.7 / sqrt(2)) < 1e-9,
  'the fallback is word-level: {alpha} against {alpha, omega} scores 0.7 / sqrt(2)'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'other-model.jpg')
      - 0.7) < 1e-9,
  'embeddings from different models are not compared: other-model falls back to tags and scores 0.7, not 0'
);
select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.target_id')::bigint)$$,
  array['other-model.jpg', 'plain.jpg', 'near.jpg', 'bare-target.jpg', 'mid.jpg'],
  'results run best first, with equal scores falling back to the highest id'
);
select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.target_id')::bigint, 2, 1)$$,
  array['plain.jpg', 'near.jpg'],
  'limit and offset page through the same ordering'
);
select ok(
  not exists (
    select 1 from public.similar_images(current_setting('tests.target_id')::bigint) where id = current_setting('tests.target_id')::bigint
  ),
  'the target is never returned'
);
select ok(
  (select bool_and(score > 0.1) from public.similar_images(current_setting('tests.target_id')::bigint)),
  'every result still scores above the minimum'
);
select is(
  (select tags from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'near.jpg'),
  '{omega}'::text[],
  'the results show the image''s tags, not its embedding'
);

-- A target without an embedding falls back to tags for every candidate, embedded or not.
select set_eq(
  $$select filename::text from public.similar_images(current_setting('tests.bare_target_id')::bigint)$$,
  array['target.jpg', 'near.jpg', 'far.jpg', 'plain.jpg', 'plain-other.jpg', 'other-model.jpg'],
  'with no embedding on the target, every candidate is scored on tags, and mid (no shared word) is left out'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.bare_target_id')::bigint) where filename = 'near.jpg')
      - 0.7 / sqrt(2)) < 1e-9,
  'near, which has an embedding, is scored on tags too when the target has none'
);

-- Isolation --------------------------------------------------------------------------------------

select ok(
  (select count(*) = 0 from public.similar_images(current_setting('tests.target_id')::bigint, 100)
   where user_id <> '00000000-0000-4000-8000-0000000000e1'),
  'B''s images never appear for A, even b-twin, whose embedding is identical to the target''s'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.b_near_id')::bigint)$$,
  'with B''s image as the target, A gets nothing, because RLS hides the target'
);

set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000000e2", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.b_near_id')::bigint)$$,
  array['b-plain.jpg', 'b-twin.jpg'],
  'B''s similar search finds B''s own images and none of A''s: b-plain by tag, b-twin by meaning'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint)$$,
  'with A''s image as the target, B gets nothing, because RLS hides the target'
);

reset role;

-- The function is still the one that was merged ---------------------------------------------------

select ok(
  has_function_privilege('authenticated', 'public.similar_images(bigint, int, int)', 'execute')
    and not has_function_privilege('anon', 'public.similar_images(bigint, int, int)', 'execute')
    and not has_function_privilege('service_role', 'public.similar_images(bigint, int, int)', 'execute'),
  'only authenticated can still execute similar_images'
);
select ok(
  (select not p.prosecdef and p.provolatile = 's'
   from pg_proc p where p.oid = 'public.similar_images(bigint, int, int)'::regprocedure),
  'similar_images is still stable and security invoker'
);
select is(
  (select pg_get_function_result('public.similar_images(bigint, int, int)'::regprocedure)),
  'TABLE(id bigint, user_id uuid, filename text, original_path text, thumbnail_path text, mime_type text, size_bytes integer, width integer, height integer, content_sha256 text, uploaded_at timestamp with time zone, description text, tags text[], colors text[], ai_processing_status text, last_error text, score double precision)',
  'similar_images still returns the same columns'
);

-- A re-analysis drops an embedding that no longer matches ------------------------------------------

select is(
  (select embedding_model from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'test-model',
  'mid starts with an embedding'
);

update public.image_metadata set colors = '{#ff0000}'
where image_id = current_setting('tests.mid_id')::bigint;

select is(
  (select embedding_model from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'test-model',
  'updating the colors keeps the embedding'
);

update public.image_metadata set tags = tags
where image_id = current_setting('tests.mid_id')::bigint;

select is(
  (select embedding_model from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'test-model',
  'writing the same tags again keeps the embedding'
);

update public.image_metadata set tags = '{sigma,tau}', embedding = pg_temp.vec(0.6, 0.8)
where image_id = current_setting('tests.mid_id')::bigint;

select ok(
  (select embedding = pg_temp.vec(0.6, 0.8) and embedding_model = 'test-model'
   from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'changing the tags and setting a new embedding in one statement keeps the new embedding'
);

update public.image_metadata set tags = '{sigma,tau,upsilon}'
where image_id = current_setting('tests.mid_id')::bigint;

select ok(
  (select embedding is null and embedding_model is null
   from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'changing the tags clears the embedding and its model id'
);

select ok(
  (select tags = '{sigma,tau,upsilon}' from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'the new tags are stored as written'
);

select lives_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'test-model')$$,
         current_setting('tests.mid_id')),
  'the worker can store a new embedding after the re-analysis (as the table owner here)'
);

update public.image_metadata set description = 'A different description'
where image_id = current_setting('tests.mid_id')::bigint;

select ok(
  (select embedding is null and embedding_model is null
   from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'changing the description clears the embedding too'
);

-- complete_analysis_job, as the worker runs it, is what a re-analysis calls.
select lives_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'test-model')$$,
         current_setting('tests.mid_id')),
  'store one more embedding on mid'
);

set local role service_role;

select lives_ok(
  format($$select public.complete_analysis_job(%s, '{sigma,new}', 'A new description', '{}', 'test:analyzer:v1')$$,
         current_setting('tests.mid_id')),
  'complete_analysis_job (the worker''s call) updates tags and description'
);

reset role;

select ok(
  (select embedding is null and embedding_model is null
   from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'completing an analysis with new tags clears the old embedding'
);

-- Completing with the same tags and description keeps it.
select lives_ok(
  format($$select public.store_image_embedding(%s, array_fill(0.5::float8, array[384])::extensions.vector(384), 'test-model')$$,
         current_setting('tests.mid_id')),
  'store the embedding again'
);

set local role service_role;

select lives_ok(
  format($$select public.complete_analysis_job(%s, '{sigma,new}', 'A new description', '{}', 'test:analyzer:v1')$$,
         current_setting('tests.mid_id')),
  'complete_analysis_job runs again with the same text'
);

reset role;

select is(
  (select embedding_model from public.image_metadata where image_id = current_setting('tests.mid_id')::bigint),
  'test-model',
  'a re-analysis that produces the same tags and description keeps the embedding'
);

select * from finish();

rollback;
