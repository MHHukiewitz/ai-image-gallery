-- Word-level "find similar": the tag_lexemes column, the trigger that derives it, and similar_images
-- scoring on it (20261020000006_word_level_similarity.sql). Run with `supabase test db`. Everything
-- happens in one transaction that rolls back.
--
-- Tags are the only thing that differs between the fixture images, and none of them has colors, so a
-- score is 0.7 · tag cosine on the word sets (the color term is 0 for an empty palette).
-- User A owns:
--   target      {house}: the image "find similar" starts from. Its word set is {hous}.
--   houses      {houses}: the same word after stemming, so cosine 1 (tie with the-house).
--   the-house   {The House}: the stop word drops out, so cosine 1 (tie with houses).
--   red-house   {red house}: {hous, red}, so cosine 1/sqrt(2). Whole-string cosine was 0.
--   big-red     {big red house}: {big, hous, red}, so cosine 1/sqrt(3).
--   garden      {garden}: shares no word, so it scores 0 and is left out.
--   untagged    no tags at all.
-- User B owns b-house {house} and b-red-house {red house}, which must never show up for A.

begin;

select plan(39);

-- These cases check the scoring formula, so some of their matches score at or below the floor that
-- "Find similar" applies (search_similar_min_score, 20261020000016). Lower it for this transaction,
-- which rolls back, so every case stays visible. search_test.sql checks the floor itself.
create or replace function public.search_similar_min_score() returns float8
language sql immutable parallel safe
return 0.1;

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-00000000000a', 'a@example.test'),
  ('00000000-0000-4000-8000-00000000000b', 'b@example.test');

create temp table fixture (
  slug text,
  user_id uuid,
  hours_after_start int,
  tags text[]
) on commit drop;

insert into fixture
values
  ('target', '00000000-0000-4000-8000-00000000000a', 0, '{house}'),
  ('houses', '00000000-0000-4000-8000-00000000000a', 1, '{houses}'),
  ('the-house', '00000000-0000-4000-8000-00000000000a', 2, '{The House}'),
  ('red-house', '00000000-0000-4000-8000-00000000000a', 3, '{red house}'),
  ('big-red', '00000000-0000-4000-8000-00000000000a', 4, '{big red house}'),
  ('garden', '00000000-0000-4000-8000-00000000000a', 5, '{garden}'),
  ('untagged', '00000000-0000-4000-8000-00000000000a', 6, '{}'),
  ('b-house', '00000000-0000-4000-8000-00000000000b', 0, '{house}'),
  ('b-red-house', '00000000-0000-4000-8000-00000000000b', 1, '{red house}');

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256, uploaded_at
)
select
  f.user_id,
  f.slug || '.jpg',
  format('%s/%s/original.jpg', f.user_id, f.slug),
  format('%s/%s/thumb.webp', f.user_id, f.slug),
  'image/jpeg', 1024, 640, 480, repeat('a', 64),
  timestamptz '2026-10-01 12:00:00+00' + f.hours_after_start * interval '1 hour'
from fixture f
order by f.hours_after_start, f.slug;

-- The trigger fills tag_lexemes, so the fixture never sets it.
insert into public.image_metadata (image_id, user_id, tags, ai_processing_status)
select i.id, f.user_id, f.tags, 'done'
from fixture f
join public.images i on i.filename = f.slug || '.jpg';

-- Ids are generated, and statements run as A can't look up B's rows, so remember the ones they name.
select set_config('tests.target_id', id::text, true) from public.images where filename = 'target.jpg';
select set_config('tests.b_house_id', id::text, true) from public.images where filename = 'b-house.jpg';

-- The lexemes of one fixture image, found by filename.
create function pg_temp.lexemes(slug text) returns text[]
language sql stable as $$
  select m.tag_lexemes
  from public.image_metadata m join public.images i on i.id = m.image_id
  where i.filename = slug || '.jpg'
$$;

-- The column ------------------------------------------------------------------------------------

select col_type_is('public', 'image_metadata', 'tag_lexemes', 'text[]', 'tag_lexemes is a text array');
select col_not_null('public', 'image_metadata', 'tag_lexemes', 'tag_lexemes is not null');
select col_default_is('public', 'image_metadata', 'tag_lexemes', '{}'::text[], 'tag_lexemes defaults to an empty array');

-- What the trigger derives on insert -------------------------------------------------------------

select is(
  pg_temp.lexemes('target'), '{hous}'::text[],
  'tag_lexemes holds the stemmed words of the tags'
);

-- Insert a row whose tags are the issue's example: two multi-word tags, one with capitals and a plural.
insert into public.images (user_id, filename, original_path, thumbnail_path,
                           mime_type, size_bytes, width, height, content_sha256)
values ('00000000-0000-4000-8000-00000000000a', 'example.jpg',
        '00000000-0000-4000-8000-00000000000a/example/original.jpg',
        '00000000-0000-4000-8000-00000000000a/example/thumb.webp',
        'image/jpeg', 1, 1, 1, repeat('b', 64));
insert into public.image_metadata (image_id, user_id, tags)
select id, user_id, '{red house,Palm Trees}' from public.images where filename = 'example.jpg';

select is(
  pg_temp.lexemes('example'), '{hous,palm,red,tree}'::text[],
  '{red house, Palm Trees} splits into the stemmed, lowercased, sorted words {hous, palm, red, tree}'
);
select is(
  (select m.tags from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'example.jpg'),
  '{"red house","Palm Trees"}'::text[],
  'tags itself keeps the tags exactly as written, with spaces and capitals'
);
select is(pg_temp.lexemes('the-house'), '{hous}'::text[], 'a stop word is dropped, so {The House} gives {hous}');
select is(pg_temp.lexemes('houses'), '{hous}'::text[], 'a plural is stemmed to the same word as the singular');
select is(pg_temp.lexemes('big-red'), '{big,hous,red}'::text[], 'three words in one tag give three lexemes');
select is(pg_temp.lexemes('untagged'), '{}'::text[], 'an image without tags has no lexemes');

-- The trigger keeps working under search_path = '' with the roles that write tags.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select lives_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'queued.jpg',
            '00000000-0000-4000-8000-00000000000a/queued/original.jpg',
            '00000000-0000-4000-8000-00000000000a/queued/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('c', 64))$$,
  'A can insert an image'
);
select lives_ok(
  $$insert into public.image_metadata (image_id, user_id, tags)
    select id, user_id, '{Hot Dogs,the}' from public.images where filename = 'queued.jpg'$$,
  'A can queue its metadata, which fires the trigger as A'
);

reset role;

select is(
  pg_temp.lexemes('queued'), '{dog,hot}'::text[],
  'the trigger filled tag_lexemes for a user''s insert, with the stop word dropped'
);

-- What the trigger refreshes on update -----------------------------------------------------------

-- The worker writes analysis results as service_role.
set local role service_role;

select lives_ok(
  $$update public.image_metadata set tags = '{Palm Tree,red houses,tree}'
    where image_id = (select id from public.images where filename = 'queued.jpg')$$,
  'service_role can write tags, which fires the trigger as service_role'
);

reset role;

select is(
  pg_temp.lexemes('queued'), '{hous,palm,red,tree}'::text[],
  'updating tags refreshes tag_lexemes, and a word repeated across tags counts once'
);
select is(
  (select m.tags from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'queued.jpg'),
  '{"Palm Tree","red houses",tree}'::text[],
  'the updated tags are stored as written'
);

update public.image_metadata
set description = 'A tiny pond creature'
where image_id = (select id from public.images where filename = 'queued.jpg');

select is(
  pg_temp.lexemes('queued'), '{hous,palm,red,tree}'::text[],
  'updating the description leaves tag_lexemes as it was'
);

update public.image_metadata
set tags = '{}'
where image_id = (select id from public.images where filename = 'queued.jpg');

select is(pg_temp.lexemes('queued'), '{}'::text[], 'clearing tags clears tag_lexemes');

-- Backfill ---------------------------------------------------------------------------------------
-- Rows from before the migration have the column default. Updating tag_lexemes alone doesn't fire the
-- trigger, so that's how to put a row back in the state the migration found it in.

update public.image_metadata
set tag_lexemes = '{}'
where image_id = (select id from public.images where filename = 'example.jpg');

select is(
  pg_temp.lexemes('example'), '{}'::text[],
  'setting tag_lexemes directly doesn''t fire the derive trigger, which gives a row as the migration found it'
);

-- The same statement as the migration's backfill.
update public.image_metadata
set tag_lexemes = pg_catalog.tsvector_to_array(
  pg_catalog.to_tsvector(
    'english'::pg_catalog.regconfig,
    pg_catalog.array_to_string(coalesce(tags, '{}'), ' ')
  )
);

select is(
  pg_temp.lexemes('example'), '{hous,palm,red,tree}'::text[],
  'the backfill fills the lexemes of an existing row'
);
select is_empty(
  $$select m.image_id
    from public.image_metadata m
    where m.tag_lexemes is distinct from
      pg_catalog.tsvector_to_array(
        pg_catalog.to_tsvector('english'::pg_catalog.regconfig,
                               pg_catalog.array_to_string(m.tags, ' ')))$$,
  'after the backfill every row''s tag_lexemes matches its tags'
);

-- Similar images (as A) --------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select is(
  public.tag_cosine('{house}', '{red house}'), 0::float8,
  'tag_cosine on whole tags still scores house against red house 0, which is what similar_images used to see'
);
select ok(
  (select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'red-house.jpg') > 0,
  'house now scores above 0 against red house'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'red-house.jpg')
      - 0.7 / sqrt(2)) < 1e-9,
  'house against red house scores 0.7 · 1/sqrt(2) on the words {hous} and {hous, red}'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'big-red.jpg')
      - 0.7 / sqrt(3)) < 1e-9,
  'house against big red house scores 0.7 · 1/sqrt(3)'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'houses.jpg')
      - 0.7) < 1e-9,
  'houses matches house completely: the plural stems to the same word'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'the-house.jpg')
      - 0.7) < 1e-9,
  'The House matches house completely: the stop word doesn''t count'
);
select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.target_id')::bigint)$$,
  array['the-house.jpg', 'houses.jpg', 'red-house.jpg', 'big-red.jpg', 'example.jpg'],
  'results run best first, with equal scores falling back to the highest id'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint)
    where filename in ('garden.jpg', 'untagged.jpg', 'queued.jpg')$$,
  'images that share no word with the target are left out'
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
select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.target_id')::bigint, 2, 1)$$,
  array['houses.jpg', 'red-house.jpg'],
  'limit and offset page through the same ordering'
);
select is(
  (select tags from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'the-house.jpg'),
  '{"The House"}'::text[],
  'the results show the tags as written, not the lexemes'
);

-- Isolation --------------------------------------------------------------------------------------

select ok(
  (select count(*) = 0 from public.similar_images(current_setting('tests.target_id')::bigint, 100)
   where user_id <> '00000000-0000-4000-8000-00000000000a'),
  'B''s images never appear for A, even b-house, which has the same tags as the target'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.b_house_id')::bigint)$$,
  'with B''s image as the target, A gets nothing, because RLS hides the target'
);

set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000b", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.b_house_id')::bigint)$$,
  array['b-red-house.jpg'],
  'B''s similar search finds B''s red house and none of A''s images'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint)$$,
  'with A''s image as the target, B gets nothing, because RLS hides the target'
);

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

select * from finish();

rollback;
