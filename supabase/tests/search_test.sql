-- Search, similarity and color functions, the helpers behind them and the trigger that derives
-- search_vector and colors_lab. Run with `supabase test db`. Everything happens in one transaction
-- that rolls back.
--
-- User A owns most of the fixture, so each case has something to find and something to skip:
--   target     beach, sunset, ocean, sky; orange and blue. The image "find similar" starts from.
--   close      shares 3 of 4 target tags, with colors near the target's: the best match.
--   tie-1..4   copies of close, uploaded at different times, to pin the tie-breaks.
--   distant    shares only the tag "beach", with dark colors.
--   twin       no shared tags, but the target's exact palette (a color-only match).
--   desc-only  mentions "beach" and "sunset" in its description but not in its tags.
--   unrelated  nothing in common with the target.
--   pending    no analysis yet: no tags, no colors, no description.
--   red-*      red palettes at different distances from #ff0000, and blue-1 for contrast.
-- User B owns b-beach (an exact copy of A's target), b-near and b-red, which must never show up for A.

begin;

select plan(115);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-00000000000a', 'a@example.test'),
  ('00000000-0000-4000-8000-00000000000b', 'b@example.test');

create temp table fixture (
  slug text,
  user_id uuid,
  hours_after_start int,
  description text,
  tags text[],
  colors text[],
  status text
) on commit drop;

insert into fixture
values
  ('target', '00000000-0000-4000-8000-00000000000a', 0, 'A quiet beach at sunset with an orange sky',
    '{beach,sunset,ocean,sky}', '{#ff8800,#1e90ff}', 'done'),
  ('close', '00000000-0000-4000-8000-00000000000a', 1, 'Tropical shore at dusk',
    '{beach,sunset,ocean,palm}', '{#ff9a1a,#2a95f0}', 'done'),
  ('tie-1', '00000000-0000-4000-8000-00000000000a', 2, 'Tropical shore at dusk',
    '{beach,sunset,ocean,palm}', '{#ff9a1a,#2a95f0}', 'done'),
  ('tie-2', '00000000-0000-4000-8000-00000000000a', 3, 'Tropical shore at dusk',
    '{beach,sunset,ocean,palm}', '{#ff9a1a,#2a95f0}', 'done'),
  ('tie-3', '00000000-0000-4000-8000-00000000000a', 3, 'Tropical shore at dusk',
    '{beach,sunset,ocean,palm}', '{#ff9a1a,#2a95f0}', 'done'),
  ('tie-4', '00000000-0000-4000-8000-00000000000a', 4, 'Tropical shore at dusk',
    '{beach,sunset,ocean,palm}', '{#ff9a1a,#2a95f0}', 'done'),
  ('distant', '00000000-0000-4000-8000-00000000000a', 5, 'A dog next to a parked car',
    '{beach,dog,car,tree}', '{#222222}', 'done'),
  ('unrelated', '00000000-0000-4000-8000-00000000000a', 6, 'Neon lights over a busy street at night',
    '{city,night,traffic}', '{#222222}', 'done'),
  ('twin', '00000000-0000-4000-8000-00000000000a', 7, 'Snowy peak',
    '{mountain,snow}', '{#ff8800,#1e90ff}', 'done'),
  ('desc-only', '00000000-0000-4000-8000-00000000000a', 8, 'People relaxing on the beach at sunset',
    '{holiday,people}', '{#222222}', 'done'),
  ('pending', '00000000-0000-4000-8000-00000000000a', 9, null, '{}', '{}', 'pending'),
  ('red-1', '00000000-0000-4000-8000-00000000000a', 10, 'A ripe tomato',
    '{tomato,fruit}', '{#ff0000,#ffffff}', 'done'),
  ('red-1b', '00000000-0000-4000-8000-00000000000a', 11, 'A red ball',
    '{ball,toy}', '{#ff0000}', 'done'),
  ('red-2', '00000000-0000-4000-8000-00000000000a', 12, 'A red scooter',
    '{scooter,vehicle}', '{#ff3b1d,#808080,#222222}', 'done'),
  ('red-3', '00000000-0000-4000-8000-00000000000a', 13, 'A red door',
    '{door,house}', '{#ee3030}', 'done'),
  ('red-edge', '00000000-0000-4000-8000-00000000000a', 14, 'A dull red barn',
    '{barn,farm}', '{#e63030}', 'done'),
  ('blue-1', '00000000-0000-4000-8000-00000000000a', 15, 'A deep blue gate',
    '{gate,blue}', '{#0000ff,#1e5aff}', 'done'),
  ('b-beach', '00000000-0000-4000-8000-00000000000b', 0, 'Beach photo',
    '{beach,sunset,ocean,sky}', '{#ff8800,#1e90ff}', 'done'),
  ('b-near', '00000000-0000-4000-8000-00000000000b', 1, 'A boat at sea',
    '{beach,sunset,ocean,boat}', '{#ff8800}', 'done'),
  ('b-red', '00000000-0000-4000-8000-00000000000b', 2, 'A red car',
    '{car}', '{#ff0000}', 'done');

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

-- The trigger fills search_vector and colors_lab, so the fixture never sets them.
insert into public.image_metadata (image_id, user_id, description, tags, colors, ai_processing_status)
select i.id, f.user_id, f.description, f.tags, f.colors, f.status
from fixture f
join public.images i on i.filename = f.slug || '.jpg';

-- Ids are generated, and statements run as A can't look up B's rows, so remember the ones they name.
select set_config('tests.target_id', id::text, true) from public.images where filename = 'target.jpg';
select set_config('tests.b_beach_id', id::text, true) from public.images where filename = 'b-beach.jpg';

-- Tells whether two flattened arrays hold the same number of values, each within tolerance.
create function pg_temp.arrays_close(actual float8[], expected float8[], tolerance float8)
returns boolean
language sql immutable as $$
  select cardinality(actual) = cardinality(expected)
    and coalesce(bool_and(abs(a.value - e.value) <= tolerance), true)
  from unnest(actual) with ordinality as a(value, ord)
  join unnest(expected) with ordinality as e(value, ord) using (ord)
$$;

-- Tunables --------------------------------------------------------------------------------------

select is(
  concat_ws(
    ', ',
    'tag weight ' || public.search_tag_weight(),
    'color weight ' || public.search_color_weight(),
    'similar min score ' || public.search_similar_min_score(),
    'color max delta E ' || public.search_color_max_delta_e(),
    'exact tag boost ' || public.search_exact_tag_boost()
  ),
  'tag weight 0.7, color weight 0.3, similar min score 0.1, color max delta E 25, exact tag boost 0.5',
  'the tunables hold the values architecture.md section 8 documents'
);

-- Color conversion ------------------------------------------------------------------------------

select ok(
  pg_temp.arrays_close(public.hex_to_lab('#ffffff'), '{100,0,0}', 0.5),
  'white is Lab {100, 0, 0}'
);
select ok(
  pg_temp.arrays_close(public.hex_to_lab('#000000'), '{0,0,0}', 0.5),
  'black is Lab {0, 0, 0}'
);
-- Reference values for sRGB primaries under D65 (for example from colorspacious or Colour).
select ok(
  pg_temp.arrays_close(public.hex_to_lab('#ff0000'), '{53.24,80.09,67.20}', 0.1),
  'red matches the reference Lab value'
);
select ok(
  pg_temp.arrays_close(public.hex_to_lab('#00ff00'), '{87.73,-86.18,83.18}', 0.1),
  'green matches the reference Lab value'
);
select ok(
  pg_temp.arrays_close(public.hex_to_lab('#0000ff'), '{32.30,79.19,-107.86}', 0.1),
  'blue matches the reference Lab value'
);
select ok(
  pg_temp.arrays_close(public.hex_to_lab('#808080'), '{53.59,0,0}', 0.1),
  'mid gray is neutral, at the lightness the sRGB curve predicts'
);
select is(
  public.hex_to_lab('FF8800'),
  public.hex_to_lab('#ff8800'),
  'hex_to_lab ignores case and accepts a missing #'
);
select is(public.hex_to_lab('#fff'), null, 'hex_to_lab returns NULL for a 3-digit shorthand');
select is(public.hex_to_lab('#gg0000'), null, 'hex_to_lab returns NULL for non-hex digits');
select is(public.hex_to_lab('red'), null, 'hex_to_lab returns NULL for a color name');
select is(public.hex_to_lab(null), null, 'hex_to_lab returns NULL for NULL');

select ok(
  pg_temp.arrays_close(public.colors_to_lab('{#ffffff,#000000,#ff0000}'),
                       '{100,0,0, 0,0,0, 53.24,80.09,67.20}', 0.5),
  'colors_to_lab flattens the palette in order, 3 values per color'
);
select is(
  cardinality(public.colors_to_lab('{#ffffff,not-a-color,#000000}')),
  6,
  'colors_to_lab skips entries that aren''t valid hex'
);
select is(public.colors_to_lab('{}'), '{}'::float8[], 'colors_to_lab of an empty palette is empty');
select is(public.colors_to_lab(null), '{}'::float8[], 'colors_to_lab of NULL is empty');

select is(public.delta_e76('{0,0,0}', '{3,4,0}'), 5::float8, 'delta_e76 is the straight-line distance in Lab');
select ok(
  abs(public.delta_e76(public.hex_to_lab('#ffffff'), public.hex_to_lab('#000000')) - 100) < 0.5,
  'black against white is about 100'
);
select is(
  public.min_delta_e76(public.hex_to_lab('#ff0000'), public.colors_to_lab('{#0000ff,#ff0000}')),
  0::float8,
  'min_delta_e76 picks the closest color of the palette'
);
select is(
  public.min_delta_e76(public.hex_to_lab('#ff0000'), '{}'),
  null,
  'min_delta_e76 against an empty palette is NULL'
);

-- Tag and palette similarity --------------------------------------------------------------------

select ok(
  abs(public.tag_cosine('{a,b,c}', '{a,b,d}') - 2.0 / 3) < 1e-12,
  'tag_cosine is |A ∩ B| / sqrt(|A|·|B|)'
);
select is(public.tag_cosine('{a,b}', '{b,a}'), 1::float8, 'identical tag sets score 1');
select is(public.tag_cosine('{a,b}', '{c,d}'), 0::float8, 'disjoint tag sets score 0');
select is(
  public.tag_cosine('{a,b,c,d}', '{a}'), 0.5::float8,
  'a one-tag subset of four tags scores 1 / sqrt(4·1)'
);
select is(public.tag_cosine('{a,a,b}', '{a,b}'), 1::float8, 'tag_cosine counts a repeated tag once');
select is(public.tag_cosine('{}', '{a}'), 0::float8, 'an empty tag set scores 0 against anything');
select is(public.tag_cosine('{a}', '{}'), 0::float8, 'anything scores 0 against an empty tag set');
select is(public.tag_cosine(null, '{a}'), 0::float8, 'a NULL tag set scores 0');

select is(
  public.color_similarity(public.colors_to_lab('{#ff8800,#1e90ff}'), public.colors_to_lab('{#1e90ff,#ff8800}')),
  1::float8,
  'identical palettes score 1, whatever the order'
);
select is(
  public.color_similarity(public.colors_to_lab('{#ff0000}'), public.colors_to_lab('{#0000ff}')),
  0::float8,
  'colors further apart than delta E 100 score 0'
);
select is(
  public.color_similarity(public.colors_to_lab('{#ff0000,#0000ff}'), public.colors_to_lab('{#ff0000}')),
  0.5::float8,
  'the mean runs over the target''s colors, so one unmatched color of two scores 0.5'
);
select is(
  public.color_similarity(public.colors_to_lab('{#ff0000}'), public.colors_to_lab('{#ff0000,#0000ff}')),
  1::float8,
  'extra candidate colors don''t lower the score'
);
select ok(
  abs(public.color_similarity(public.hex_to_lab('#ff0000'), public.hex_to_lab('#ee3030'))
      - (1 - public.delta_e76(public.hex_to_lab('#ff0000'), public.hex_to_lab('#ee3030')) / 100)) < 1e-9,
  'a single color pair scores 1 - delta E / 100'
);
select is(
  public.color_similarity(public.colors_to_lab('{#ff0000}'), '{}'), 0::float8,
  'a candidate with no colors scores 0'
);
select is(
  public.color_similarity('{}', public.colors_to_lab('{#ff0000}')), 0::float8,
  'a target with no colors scores 0'
);
select is(public.color_similarity(null, null), 0::float8, 'NULL palettes score 0');

-- Helper functions: volatility and grants -------------------------------------------------------

select ok(
  (select count(*) = 11 and bool_and(p.provolatile = 'i' and p.proparallel = 's')
   from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('search_tag_weight', 'search_color_weight', 'search_similar_min_score',
                       'search_color_max_delta_e', 'search_exact_tag_boost', 'hex_to_lab',
                       'colors_to_lab', 'delta_e76', 'min_delta_e76', 'color_similarity', 'tag_cosine')),
  'the helpers are immutable and parallel safe'
);
select ok(
  (select count(*) = 11 and bool_and(
     has_function_privilege('authenticated', p.oid, 'execute')
     and has_function_privilege('service_role', p.oid, 'execute')
     and not has_function_privilege('anon', p.oid, 'execute')
   )
   from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('search_tag_weight', 'search_color_weight', 'search_similar_min_score',
                       'search_color_max_delta_e', 'search_exact_tag_boost', 'hex_to_lab',
                       'colors_to_lab', 'delta_e76', 'min_delta_e76', 'color_similarity', 'tag_cosine')),
  'authenticated and service_role can execute the helpers, which their triggers and searches call, and anon can''t'
);
select ok(
  not exists (
    select 1
    from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) as acl
    where p.pronamespace = 'public'::regnamespace
      and p.proname in ('search_tag_weight', 'search_color_weight', 'search_similar_min_score',
                        'search_color_max_delta_e', 'search_exact_tag_boost', 'hex_to_lab',
                        'colors_to_lab', 'delta_e76', 'min_delta_e76', 'color_similarity', 'tag_cosine')
      and acl.grantee = 0
  ),
  'no helper is executable by PUBLIC'
);

-- Search functions: structure and grants --------------------------------------------------------

select ok(
  (select count(*) = 3 and bool_and(
     (select array_agg(arg.name order by arg.ord)
      from unnest(p.proargnames, p.proargmodes) with ordinality as arg(name, mode, ord)
      where arg.mode = 't')
     = (select array_agg(a.attname::text order by a.attnum) || 'score'::text
        from pg_attribute a
        -- last_uploaded_at (#60) only orders the unfiltered gallery. Search ranks by score.
        where a.attrelid = 'public.image_details'::regclass and a.attnum > 0 and not a.attisdropped
          and a.attname <> 'last_uploaded_at')
   )
   from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('search_images', 'similar_images', 'images_by_color')),
  'search_images, similar_images and images_by_color return the image_details columns (but last_uploaded_at) plus score, in that order'
);
select ok(
  (select count(*) = 3 and bool_and(not p.prosecdef and p.provolatile = 's' and p.prolang = (select oid from pg_language where lanname = 'sql'))
   from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('search_images', 'similar_images', 'images_by_color')),
  'the search functions are SQL, stable and security invoker, so RLS applies to them'
);
select ok(
  not exists (
    select 1
    from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) as acl
    where p.pronamespace = 'public'::regnamespace
      and p.proname in ('search_images', 'similar_images', 'images_by_color', 'derive_image_metadata')
      and acl.grantee = 0
  ),
  'neither the search functions nor the derive trigger function is executable by PUBLIC'
);
select ok(
  not has_function_privilege('anon', 'public.search_images(text, int, int)', 'execute')
    and not has_function_privilege('anon', 'public.similar_images(bigint, int, int)', 'execute')
    and not has_function_privilege('anon', 'public.images_by_color(text, int, int)', 'execute'),
  'anon can''t execute the search functions'
);
select ok(
  has_function_privilege('authenticated', 'public.search_images(text, int, int)', 'execute')
    and has_function_privilege('authenticated', 'public.similar_images(bigint, int, int)', 'execute')
    and has_function_privilege('authenticated', 'public.images_by_color(text, int, int)', 'execute'),
  'authenticated can execute the search functions'
);
select ok(
  not has_function_privilege('service_role', 'public.search_images(text, int, int)', 'execute')
    and not has_function_privilege('service_role', 'public.similar_images(bigint, int, int)', 'execute')
    and not has_function_privilege('service_role', 'public.images_by_color(text, int, int)', 'execute'),
  'service_role can''t execute the search functions, since it would search every tenant'
);
select trigger_is(
  'public', 'image_metadata', 'image_metadata_derive', 'public', 'derive_image_metadata',
  'image_metadata_derive runs derive_image_metadata'
);

-- Derived columns on insert ----------------------------------------------------------------------

select ok(
  (select search_vector @@ to_tsquery('english', 'sunset:A') and search_vector @@ to_tsquery('english', 'quiet:B')
   from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'target.jpg'),
  'search_vector weighs tags as A and the description as B'
);
select ok(
  (select not (search_vector @@ to_tsquery('english', 'quiet:A')) and not (search_vector @@ to_tsquery('english', 'ocean:B'))
   from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'target.jpg'),
  'description words aren''t weighted A, and tags aren''t weighted B'
);
select is(
  (select colors_lab from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'target.jpg'),
  public.colors_to_lab('{#ff8800,#1e90ff}'),
  'colors_lab holds the Lab values of colors'
);
select is(
  (select colors_lab from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'pending.jpg'),
  '{}'::float8[],
  'an image without colors gets an empty colors_lab'
);
select is(
  (select search_vector from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'pending.jpg'),
  ''::tsvector,
  'an image without tags or description gets an empty search_vector'
);

-- Text search (as A) -----------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.search_images('bea') order by filename$$,
  array['close.jpg', 'desc-only.jpg', 'distant.jpg', 'target.jpg', 'tie-1.jpg', 'tie-2.jpg', 'tie-3.jpg', 'tie-4.jpg'],
  'the prefix "bea" matches the tag beach (and the word in a description)'
);
select ok(
  exists (select 1 from public.search_images('bea') where filename = 'distant.jpg'),
  'the prefix "bea" matches an image that has beach only as a tag'
);
select results_eq(
  $$select filename::text from public.search_images('palm') order by filename$$,
  array['close.jpg', 'tie-1.jpg', 'tie-2.jpg', 'tie-3.jpg', 'tie-4.jpg'],
  'a whole tag matches too'
);
select ok(
  (select min(score) from public.search_images('sunset') where filename <> 'desc-only.jpg')
    > (select score from public.search_images('sunset') where filename = 'desc-only.jpg'),
  'an image with sunset as a tag ranks above one that only mentions it in the description'
);
select is(
  (select filename::text from public.search_images('sunset') offset 6),
  'desc-only.jpg',
  'the description-only match comes last'
);
select ok(
  abs((select score from public.search_images('beach') where filename = 'distant.jpg')
      - (select score from public.search_images('bea') where filename = 'distant.jpg')
      - public.search_exact_tag_boost()) < 1e-9,
  'a query equal to a tag adds the exact-tag boost on top of the text rank'
);
select ok(
  (select score from public.search_images('palm') where filename = 'close.jpg') > 0,
  'ts_rank_cd ranks a prefix or whole-word match above zero'
);
select results_eq(
  $$select id from public.search_images('  BEACH ') order by id$$,
  $$select id from public.search_images('beach') order by id$$,
  'search ignores case and surrounding spaces'
);
select results_eq(
  $$select filename::text from public.search_images('beach sunset') order by filename$$,
  array['close.jpg', 'desc-only.jpg', 'target.jpg', 'tie-1.jpg', 'tie-2.jpg', 'tie-3.jpg', 'tie-4.jpg'],
  'multi-word input needs every word, and the words can come from tags and description'
);
select results_eq(
  $$select filename::text from public.search_images('Beach, Sunset!') order by filename$$,
  array['close.jpg', 'desc-only.jpg', 'target.jpg', 'tie-1.jpg', 'tie-2.jpg', 'tie-3.jpg', 'tie-4.jpg'],
  'punctuation only separates words'
);
select results_eq(
  $$select filename::text from public.search_images('beach dog')$$,
  array['distant.jpg'],
  'beach AND dog finds only the image that has both'
);
select is_empty(
  $$select id from public.search_images('beach unicorn')$$,
  'one unmatched word empties the result'
);
select is_empty(
  $$select id from public.search_images('')
    union all select id from public.search_images('   ')
    union all select id from public.search_images('!!! ...')
    union all select id from public.search_images(null)$$,
  'empty, blank, punctuation-only and NULL input return nothing'
);
select is_empty(
  $$select id from public.search_images('the')$$,
  'a stop word alone matches nothing and doesn''t fail'
);
select lives_ok(
  $$select id from public.search_images('beach & !( | <-> :* ''; select 1 --')$$,
  'tsquery syntax in the input is treated as plain text'
);
select ok(
  (select bool_and(score >= 0) from public.search_images('beach')),
  'scores are never negative'
);
select ok(
  (select count(*) = 0 from public.search_images('beach') where user_id <> '00000000-0000-4000-8000-00000000000a'),
  'A''s text search never returns B''s images'
);
select ok(
  not exists (select 1 from public.search_images('car') where filename = 'b-red.jpg'),
  'A can''t find B''s image by one of its tags'
);

-- Pagination: pages must add up to the full ordered list, including score ties -------------------

select is(
  (select array_agg(r.id order by r.ordinality) from public.search_images('beach', 100, 0) with ordinality as r),
  (select array_agg(r.id order by r.page * 100 + r.ordinality)
   from (
     select 1 as page, s.id, s.ordinality from public.search_images('beach', 3, 0) with ordinality as s
     union all select 2, s.id, s.ordinality from public.search_images('beach', 3, 3) with ordinality as s
     union all select 3, s.id, s.ordinality from public.search_images('beach', 3, 6) with ordinality as s
   ) as r),
  'pages of the text search concatenate to the full list'
);
select results_eq(
  $$select filename::text from public.search_images('palm', 5, 0)$$,
  array['tie-4.jpg', 'tie-3.jpg', 'tie-2.jpg', 'tie-1.jpg', 'close.jpg'],
  'equal ranks fall back to newest upload first, then highest id'
);
select is_empty($$select id from public.search_images('beach', 0, 0)$$, 'lim 0 returns nothing');
select is_empty($$select id from public.search_images('beach', 5, 100)$$, 'an offset past the end returns nothing');
select is_empty($$select id from public.search_images('beach', -1, 0)$$, 'a negative lim returns nothing instead of failing');
select results_eq(
  $$select id from public.search_images('beach', 3, -5)$$,
  $$select id from public.search_images('beach', 3, 0)$$,
  'a negative offset counts as 0'
);

-- Similar images (as A) --------------------------------------------------------------------------

select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.target_id')::bigint, 6)$$,
  array['tie-4.jpg', 'tie-3.jpg', 'tie-2.jpg', 'tie-1.jpg', 'close.jpg', 'twin.jpg'],
  'sharing 3 of 4 tags and similar colors beats a color-only match, and equal scores fall back to the highest id'
);
select ok(
  (select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'close.jpg')
    > (select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'distant.jpg'),
  'an image sharing 3 tags and similar colors outranks one sharing 1 tag'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'close.jpg')
      - (0.7 * 0.75 + 0.3 * public.color_similarity(public.colors_to_lab('{#ff8800,#1e90ff}'),
                                                    public.colors_to_lab('{#ff9a1a,#2a95f0}')))) < 1e-9,
  'the score is 0.7 · tag cosine + 0.3 · color similarity'
);
select ok(
  abs((select score from public.similar_images(current_setting('tests.target_id')::bigint) where filename = 'twin.jpg') - 0.3) < 1e-9,
  'the same palette with no shared tags scores the color weight alone'
);
select ok(
  not exists (
    select 1 from public.similar_images(current_setting('tests.target_id')::bigint) where id = current_setting('tests.target_id')::bigint
  ),
  'the target is never returned'
);
select ok(
  (select bool_and(score > 0.1) from public.similar_images(current_setting('tests.target_id')::bigint)),
  'every result scores above the minimum'
);
select ok(
  not exists (
    select 1 from public.similar_images(current_setting('tests.target_id')::bigint)
    where filename in ('unrelated.jpg', 'desc-only.jpg', 'pending.jpg', 'blue-1.jpg')
  ),
  'images scoring 0.1 or less are left out'
);
select ok(
  (select count(*) = 0 from public.similar_images(current_setting('tests.target_id')::bigint, 100)
   where user_id <> '00000000-0000-4000-8000-00000000000a'),
  'B''s images never appear for A, even b-beach, an exact copy of the target'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.b_beach_id')::bigint)$$,
  'with B''s image as the target, A gets nothing, because RLS hides the target'
);
select is_empty($$select id from public.similar_images(-1)$$, 'a target that doesn''t exist returns nothing');

select is(
  (select array_agg(r.id order by r.ordinality)
   from public.similar_images(current_setting('tests.target_id')::bigint, 100, 0) with ordinality as r),
  (select array_agg(r.id order by r.page * 100 + r.ordinality)
   from (
     select 1 as page, s.id, s.ordinality
       from public.similar_images(current_setting('tests.target_id')::bigint, 4, 0) with ordinality as s
     union all select 2, s.id, s.ordinality
       from public.similar_images(current_setting('tests.target_id')::bigint, 4, 4) with ordinality as s
     union all select 3, s.id, s.ordinality
       from public.similar_images(current_setting('tests.target_id')::bigint, 4, 8) with ordinality as s
   ) as r),
  'pages of similar images concatenate to the full list'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint, 0)$$,
  'lim 0 returns nothing'
);

-- Color search (as A) ----------------------------------------------------------------------------

select results_eq(
  $$select filename::text from public.images_by_color('#ff0000')$$,
  array['red-1b.jpg', 'red-1.jpg', 'red-2.jpg', 'red-3.jpg'],
  '#ff0000 finds the red images nearest first, with equal distances falling back to the highest id'
);
select ok(
  not exists (select 1 from public.images_by_color('#ff0000') where filename in ('blue-1.jpg', 'target.jpg', 'twin.jpg')),
  '#ff0000 excludes blue images'
);
select ok(
  not exists (select 1 from public.images_by_color('#ff0000') where filename = 'red-edge.jpg')
    and exists (select 1 from public.images_by_color('#ff0000') where filename = 'red-3.jpg'),
  'delta E 25.5 is out and delta E 22.5 is in'
);
select ok(
  abs((select score from public.images_by_color('#ff0000') where filename = 'red-1.jpg') - 1) < 1e-9,
  'an exact match scores 1'
);
select ok(
  abs((select score from public.images_by_color('#ff0000') where filename = 'red-2.jpg')
      - (1 - public.delta_e76(public.hex_to_lab('#ff0000'), public.hex_to_lab('#ff3b1d')) / 100)) < 1e-9,
  'the score is 1 - dist / 100, with dist taken from the image''s nearest color'
);
select results_eq(
  $$select id from public.images_by_color('FF0000')$$,
  $$select id from public.images_by_color('#ff0000')$$,
  'the query color ignores case and a missing #'
);
select is_empty(
  $$select id from public.images_by_color('nope')
    union all select id from public.images_by_color('#fff')
    union all select id from public.images_by_color(null)$$,
  'malformed colors return nothing'
);
select ok(
  not exists (select 1 from public.images_by_color('#ff0000') where user_id <> '00000000-0000-4000-8000-00000000000a'),
  'B''s exact #ff0000 image never appears for A'
);
select ok(
  (select count(*) = 0 from public.images_by_color('#ff0000', 100) where filename = 'b-red.jpg'),
  'A can''t find b-red by color'
);
select is(
  (select array_agg(r.id order by r.ordinality) from public.images_by_color('#ff0000', 100, 0) with ordinality as r),
  (select array_agg(r.id order by r.page * 100 + r.ordinality)
   from (
     select 1 as page, s.id, s.ordinality from public.images_by_color('#ff0000', 3, 0) with ordinality as s
     union all select 2, s.id, s.ordinality from public.images_by_color('#ff0000', 3, 3) with ordinality as s
   ) as r),
  'pages of the color search concatenate to the full list'
);
select is_empty($$select id from public.images_by_color('#ff0000', 5, 10)$$, 'an offset past the end returns nothing');

-- B's view of the same data ----------------------------------------------------------------------

set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000b", "role": "authenticated"}';

select results_eq(
  $$select filename::text from public.search_images('beach') order by filename$$,
  array['b-beach.jpg', 'b-near.jpg'],
  'B''s text search finds only B''s images'
);
select results_eq(
  $$select filename::text from public.similar_images(current_setting('tests.b_beach_id')::bigint)$$,
  array['b-near.jpg'],
  'B''s similar search ignores A''s better matches'
);
select is_empty(
  $$select id from public.similar_images(current_setting('tests.target_id')::bigint)$$,
  'with A''s image as the target, B gets nothing, because RLS hides the target'
);
select results_eq(
  $$select filename::text from public.images_by_color('#ff0000')$$,
  array['b-red.jpg'],
  'B''s color search finds only B''s images'
);

-- Grants at call time ----------------------------------------------------------------------------

set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  $$select id from public.search_images('beach')$$,
  '42501', 'permission denied for function search_images',
  'anon can''t call search_images'
);
select throws_ok(
  $$select id from public.similar_images(1)$$,
  '42501', 'permission denied for function similar_images',
  'anon can''t call similar_images'
);
select throws_ok(
  $$select id from public.images_by_color('#ff0000')$$,
  '42501', 'permission denied for function images_by_color',
  'anon can''t call images_by_color'
);

reset role;
set local role service_role;

select throws_ok(
  $$select id from public.search_images('beach')$$,
  '42501', 'permission denied for function search_images',
  'service_role can''t call search_images'
);

-- Derived columns follow writes by users and the worker ------------------------------------------

reset role;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-00000000000a", "role": "authenticated"}';

select lives_ok(
  $$insert into public.images (user_id, filename, original_path, thumbnail_path,
                               mime_type, size_bytes, width, height, content_sha256)
    values ('00000000-0000-4000-8000-00000000000a', 'queued.jpg',
            '00000000-0000-4000-8000-00000000000a/queued/original.jpg',
            '00000000-0000-4000-8000-00000000000a/queued/thumb.webp',
            'image/jpeg', 1, 1, 1, repeat('b', 64))$$,
  'A can insert an image'
);
select lives_ok(
  $$insert into public.image_metadata (image_id, user_id, description, tags, colors)
    select id, user_id, 'A green frog', '{frog}', '{#00ff00}' from public.images where filename = 'queued.jpg'$$,
  'A can queue its metadata, which fires the trigger as A'
);
select results_eq(
  $$select filename::text from public.search_images('frog')$$,
  array['queued.jpg'],
  'a user''s own insert is searchable right away'
);

reset role;

select is(
  (select cardinality(colors_lab) from public.image_metadata m join public.images i on i.id = m.image_id
   where i.filename = 'queued.jpg'),
  3,
  'the trigger filled colors_lab for a user''s insert'
);

-- The worker writes results as service_role.
set local role service_role;

select lives_ok(
  $$update public.image_metadata set tags = '{amphibian}', colors = '{#0000ff,#00ff00}', ai_processing_status = 'done'
    where image_id = (select id from public.images where filename = 'queued.jpg')$$,
  'service_role can write analysis results, which fires the trigger as service_role'
);

reset role;

select ok(
  (select search_vector @@ to_tsquery('english', 'amphibian:A') and not (search_vector @@ to_tsquery('english', 'frog:A'))
     and search_vector @@ to_tsquery('english', 'frog:B')
   from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'queued.jpg'),
  'updating tags rebuilds search_vector'
);
select is(
  (select colors_lab from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'queued.jpg'),
  public.colors_to_lab('{#0000ff,#00ff00}'),
  'updating colors rebuilds colors_lab'
);

update public.image_metadata
set description = 'A tiny pond creature'
where image_id = (select id from public.images where filename = 'queued.jpg');

select ok(
  (select search_vector @@ to_tsquery('english', 'pond:B') and not (search_vector @@ to_tsquery('english', 'frog'))
   from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'queued.jpg'),
  'updating the description rebuilds search_vector'
);

-- A column outside the trigger's list leaves the derived columns alone: plant a sentinel to see it.
update public.image_metadata
set search_vector = 'sentinel'::tsvector
where image_id = (select id from public.images where filename = 'queued.jpg');
update public.image_metadata
set last_error = 'unrelated change'
where image_id = (select id from public.images where filename = 'queued.jpg');

select is(
  (select search_vector from public.image_metadata m join public.images i on i.id = m.image_id where i.filename = 'queued.jpg'),
  'sentinel'::tsvector,
  'updating columns the trigger doesn''t watch leaves search_vector alone'
);

select * from finish();

rollback;
