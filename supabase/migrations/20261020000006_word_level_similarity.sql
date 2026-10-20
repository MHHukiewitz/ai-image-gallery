-- Word-level "find similar" (docs/architecture.md section 8).
-- tag_cosine compares whole tag strings, so `house` and `red house`, or `house` and `houses`, shared
-- nothing. This migration derives a word set per image and scores similarity on that set instead.
-- It is for matching only: `tags` stays exactly as the model wrote it, so compounds such as `hot dog`
-- or `New York` display intact and still get the exact-tag boost in text search.
--
-- Additive and backward-compatible: a new column with a default, and `create or replace` for two
-- functions whose signatures, return columns and grants don't change.

-- The stemmed, deduplicated words of `tags`, in tsvector order (so `{red house, Palm Trees}` becomes
-- `{hous, palm, red, tree}`; stop words such as `the` drop out). Filled by the derive trigger below.
alter table public.image_metadata
  add column tag_lexemes text[] not null default '{}';

-- Same function as in 20261020000005_search.sql with one more derived column. `create or replace`
-- keeps the trigger and the revoked EXECUTE grants. The function runs with search_path = '', so the
-- new expression is schema-qualified. The search_vector and colors_lab lines are unchanged.
create or replace function public.derive_image_metadata() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.search_vector :=
    setweight(to_tsvector('english', array_to_string(new.tags, ' ')), 'A')
    || setweight(to_tsvector('english', coalesce(new.description, '')), 'B');
  new.colors_lab := public.colors_to_lab(new.colors);
  new.tag_lexemes := pg_catalog.tsvector_to_array(
    pg_catalog.to_tsvector(
      'english'::pg_catalog.regconfig,
      pg_catalog.array_to_string(coalesce(new.tags, '{}'), ' ')
    )
  );
  return new;
end;
$$;

-- Fill the column for rows that existed before this migration (a no-op on a fresh database). The
-- trigger only fires on updates of tags, description and colors, so this update doesn't fire it and
-- the expression has to be repeated here. The set_updated_at trigger does fire and bumps updated_at,
-- which nothing reads.
update public.image_metadata
set tag_lexemes = pg_catalog.tsvector_to_array(
  pg_catalog.to_tsvector(
    'english'::pg_catalog.regconfig,
    pg_catalog.array_to_string(coalesce(tags, '{}'), ' ')
  )
);

-- "Find similar" scores the tag term on word sets. The signature, return columns, weights, threshold
-- and ordering are exactly as in 20261020000005_search.sql, and `create or replace` keeps the grants
-- (authenticated only), so only tag_cosine's inputs change from tags to tag_lexemes.
create or replace function public.similar_images(target_id bigint, lim int default 20, off int default 0)
returns table (
  id bigint,
  user_id uuid,
  filename text,
  original_path text,
  thumbnail_path text,
  mime_type text,
  size_bytes int,
  width int,
  height int,
  content_sha256 text,
  uploaded_at timestamptz,
  description text,
  tags text[],
  colors text[],
  ai_processing_status text,
  last_error text,
  score float8
)
language sql stable security invoker
begin atomic
  select
    i.id, i.user_id, i.filename, i.original_path, i.thumbnail_path, i.mime_type, i.size_bytes,
    i.width, i.height, i.content_sha256, i.uploaded_at,
    m.description, m.tags, m.colors, m.ai_processing_status, m.last_error,
    similarity.score
  from public.image_metadata target
  join public.image_metadata m on m.image_id <> target.image_id
  join public.images i on i.id = m.image_id
  cross join lateral (
    -- offset 0 keeps the planner from copying this expression into the WHERE and ORDER BY
    -- below, which would compute the score three times per candidate.
    select public.search_tag_weight() * public.tag_cosine(target.tag_lexemes, m.tag_lexemes)
      + public.search_color_weight() * public.color_similarity(target.colors_lab, m.colors_lab) as score
    offset 0
  ) as similarity
  where target.image_id = target_id
    and similarity.score > public.search_similar_min_score()
  order by similarity.score desc, i.id desc
  limit greatest(lim, 0) offset greatest(off, 0);
end;
