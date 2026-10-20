-- Text search, "find similar" and color search (docs/architecture.md sections 5 and 8).
-- Everything runs in Postgres, so there is no vector DB. The three search functions are
-- `security invoker`: they read image_metadata and images as the caller, so the RLS from
-- 20261020000002_rls.sql keeps every search inside the caller's own images.
--
-- Style note: SQL functions here use SQL-standard bodies (`return` / `begin atomic`), which Postgres
-- resolves when the function is created. That leaves no search_path to hijack. The plpgsql
-- functions are resolved at run time, so they pin `search_path = ''` and schema-qualify names.

-- Tunables --------------------------------------------------------------------------------------
-- Each constant is an immutable function, because Postgres has no global constants. They are
-- literal-only, so there is nothing to hijack and they need no search_path.

-- TAG_WEIGHT: share of the "find similar" score that comes from shared tags (they carry the meaning).
create function public.search_tag_weight() returns float8
language sql immutable parallel safe
return 0.7;

-- COLOR_WEIGHT: share of the "find similar" score that comes from palette closeness (a tie-breaker).
create function public.search_color_weight() returns float8
language sql immutable parallel safe
return 0.3;

-- SIMILAR_MIN_SCORE: "find similar" drops anything scoring this low or lower, so it never lists noise.
create function public.search_similar_min_score() returns float8
language sql immutable parallel safe
return 0.1;

-- COLOR_MAX_DELTA_E: widest CIE76 distance still shown as a match in "filter by color" (about "same hue family").
create function public.search_color_max_delta_e() returns float8
language sql immutable parallel safe
return 25;

-- EXACT_TAG_BOOST: added to the text-search rank when the whole query equals one of the image's tags.
create function public.search_exact_tag_boost() returns float8
language sql immutable parallel safe
return 0.5;

-- Color helpers ---------------------------------------------------------------------------------

-- Converts '#rrggbb' (the '#' is optional) to CIE Lab under D65: sRGB -> linear light -> XYZ -> Lab.
-- Returns {L, a, b}, or NULL for NULL or malformed input, so one bad palette entry can't fail an insert.
create function public.hex_to_lab(hex text) returns float8[]
language plpgsql immutable strict parallel safe
set search_path = ''
as $$
declare
  -- D65 reference white, matching the sRGB-to-XYZ matrix below.
  white constant float8[] := array[0.95047, 1.0, 1.08883];
  -- CIE constants: the cube-root curve is replaced by a straight line below epsilon = 216/24389.
  epsilon constant float8 := 216.0 / 24389.0;
  kappa constant float8 := 24389.0 / 27.0;
  rgb bytea;
  channel float8;
  linear float8[] := array[0, 0, 0];
  xyz float8[];
  pivot float8[] := array[0, 0, 0];
  ratio float8;
begin
  if hex !~ '^#?[0-9a-fA-F]{6}$' then
    return null;
  end if;
  rgb := decode(right(hex, 6), 'hex');

  for i in 1..3 loop
    channel := get_byte(rgb, i - 1) / 255.0;
    linear[i] := case
      when channel <= 0.04045 then channel / 12.92
      else ((channel + 0.055) / 1.055) ^ 2.4
    end;
  end loop;

  xyz := array[
    0.4124564 * linear[1] + 0.3575761 * linear[2] + 0.1804375 * linear[3],
    0.2126729 * linear[1] + 0.7151522 * linear[2] + 0.0721750 * linear[3],
    0.0193339 * linear[1] + 0.1191920 * linear[2] + 0.9503041 * linear[3]
  ];

  for i in 1..3 loop
    ratio := xyz[i] / white[i];
    pivot[i] := case
      when ratio > epsilon then cbrt(ratio)
      else (kappa * ratio + 16) / 116
    end;
  end loop;

  return array[
    116 * pivot[2] - 16,
    500 * (pivot[1] - pivot[2]),
    200 * (pivot[2] - pivot[3])
  ];
end;
$$;

-- Converts a palette to one flattened array {L1, a1, b1, L2, a2, b2, ...} in palette order.
-- Entries that aren't valid hex are skipped, so the result always holds 3 values per valid color.
create function public.colors_to_lab(colors text[]) returns float8[]
language sql immutable parallel safe
begin atomic
  select coalesce(array_agg(lab.value order by palette.color_index, lab.channel_index), '{}')
  from unnest(colors) with ordinality as palette(hex, color_index),
       unnest(public.hex_to_lab(palette.hex)) with ordinality as lab(value, channel_index);
end;

-- CIE76 color difference: the straight-line distance between two Lab colors, each given as {L, a, b}.
-- Roughly, 2.3 is the smallest difference an eye notices, and 100 is black against white.
create function public.delta_e76(lab_a float8[], lab_b float8[]) returns float8
language sql immutable strict parallel safe
return sqrt(
  (lab_a[1] - lab_b[1]) ^ 2 + (lab_a[2] - lab_b[2]) ^ 2 + (lab_a[3] - lab_b[3]) ^ 2
);

-- The smallest delta_e76 between one Lab color {L, a, b} and any color in a flattened palette.
-- NULL when the palette is empty or NULL.
create function public.min_delta_e76(lab float8[], palette_lab float8[]) returns float8
language sql immutable parallel safe
begin atomic
  select min(public.delta_e76(lab, palette_lab[3 * color_index + 1 : 3 * color_index + 3]))
  from generate_series(0, coalesce(cardinality(palette_lab), 0) / 3 - 1) as color_index;
end;

-- How close two palettes are, from 0 (nothing alike) to 1 (identical): the mean over the target's
-- colors of the best match in the candidate, where a match scores 1 - min(delta E, 100) / 100.
-- An empty palette on either side scores 0. The `least` call relies on ignoring NULL, which gives a
-- target color with no candidate colors the worst distance, 100.
-- This is one statement on purpose: it runs once per candidate image, and calling min_delta_e76
-- for each target color made it several times slower.
create function public.color_similarity(target_lab float8[], candidate_lab float8[]) returns float8
language sql immutable parallel safe
begin atomic
  select coalesce(avg(1 - least(nearest.dist, 100) / 100), 0)
  from generate_series(0, coalesce(cardinality(target_lab), 0) / 3 - 1) as target_index
  cross join lateral (
    select min(public.delta_e76(
      target_lab[3 * target_index + 1 : 3 * target_index + 3],
      candidate_lab[3 * candidate_index + 1 : 3 * candidate_index + 3]
    )) as dist
    from generate_series(0, coalesce(cardinality(candidate_lab), 0) / 3 - 1) as candidate_index
  ) as nearest;
end;

-- Tag helper ------------------------------------------------------------------------------------

-- Cosine similarity of two tag sets as binary vectors: |A ∩ B| / sqrt(|A| · |B|).
-- Duplicates and NULL elements are ignored, and an empty set on either side scores 0.
create function public.tag_cosine(a text[], b text[]) returns float8
language sql immutable parallel safe
begin atomic
  select coalesce(
    (select count(distinct tag) from unnest(a) as tag where tag = any (b))::float8
      / nullif(
          sqrt((select count(distinct tag) from unnest(a) as tag)::float8
             * (select count(distinct tag) from unnest(b) as tag)::float8),
          0
        ),
    0
  );
end;

-- Grants for the constants and helpers ----------------------------------------------------------

-- New functions are executable by PUBLIC, and Supabase also grants them to anon, authenticated and
-- service_role. Start from nothing and grant back only what is needed. The helpers are pure, and the
-- roles that write image_metadata (users insert as authenticated, the worker updates as service_role)
-- need them, because the derive trigger and the search functions call them as the caller.
revoke all on function
  public.search_tag_weight(),
  public.search_color_weight(),
  public.search_similar_min_score(),
  public.search_color_max_delta_e(),
  public.search_exact_tag_boost(),
  public.hex_to_lab(text),
  public.colors_to_lab(text[]),
  public.delta_e76(float8[], float8[]),
  public.min_delta_e76(float8[], float8[]),
  public.color_similarity(float8[], float8[]),
  public.tag_cosine(text[], text[])
  from public, anon, authenticated, service_role;

grant execute on function
  public.search_tag_weight(),
  public.search_color_weight(),
  public.search_similar_min_score(),
  public.search_color_max_delta_e(),
  public.search_exact_tag_boost(),
  public.hex_to_lab(text),
  public.colors_to_lab(text[]),
  public.delta_e76(float8[], float8[]),
  public.min_delta_e76(float8[], float8[]),
  public.color_similarity(float8[], float8[]),
  public.tag_cosine(text[], text[])
  to authenticated, service_role;
