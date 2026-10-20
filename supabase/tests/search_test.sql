-- The constants and helpers behind search: color conversion, CIE76 distance, tag cosine and palette
-- similarity. Run with `supabase test db`. Everything happens in one transaction that rolls back.

begin;

select plan(39);

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

select * from finish();

rollback;
