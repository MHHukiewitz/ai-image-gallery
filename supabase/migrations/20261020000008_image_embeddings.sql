-- Semantic "find similar" (docs/architecture.md section 8, decision D7).
-- The worker embeds each analyzed image's description and tags with a small local model
-- (BAAI/bge-small-en-v1.5, 384 dimensions) and stores the vector next to the row it describes, with
-- pgvector in this same database, so there is no separate vector database. `similar_images` scores
-- the semantic term on those vectors, and falls back to the word-level tag_lexemes of
-- 20261020000006_word_level_similarity.sql when either image has no embedding yet.
--
-- Additive and backward-compatible: an extension, two nullable columns, new functions, and
-- `create or replace` for one function whose signature, return columns and grants don't change.
-- Rows analyzed before this migration have no embedding and keep matching on tag_lexemes until the
-- backfill (python -m gallery.backfill_embeddings) embeds them.

-- pgvector ----------------------------------------------------------------------------------------

-- Supabase keeps extensions in the `extensions` schema. Every use below is schema-qualified, so
-- nothing depends on a role's search_path.
create extension if not exists vector with schema extensions;

-- The vector and the id of the model that produced it. Vectors from different models are not
-- comparable even at the same dimension, so similar_images only compares equal model ids. The two
-- columns are set and cleared together.
-- No index: "find similar" compares one target against one user's own images, a handful of rows
-- under RLS, so an approximate-nearest-neighbour index would cost more than it saves.
alter table public.image_metadata
  add column embedding extensions.vector(384),
  add column embedding_model text,
  add constraint image_metadata_embedding_model_check
    check ((embedding is null) = (embedding_model is null));

-- Tunable ----------------------------------------------------------------------------------------

-- EMBEDDING_FLOOR: the cosine similarity at which two images count as having nothing in common.
-- bge-small-en-v1.5 compresses its scores into a narrow band: two unrelated texts mostly score
-- 0.5 to 0.7 and related ones 0.7 to 0.93, so the raw cosine can't be used as a 0..1 score. The
-- semantic term is (cosine - floor) / (1 - floor), clamped at 0. 0.65 comes from calibrating the real
-- model (docs/architecture.md section 8): it maps 78% of unrelated image pairs to exactly 0 (mean
-- 0.03) and related pairs to 0.12 to 0.80 (median 0.57). Changing the model means calibrating again.
create function public.search_embedding_floor() returns float8
language sql immutable parallel safe
return 0.65;

-- Same grants as the other constants in 20261020000005_search.sql.
revoke all on function public.search_embedding_floor() from public, anon, authenticated, service_role;
grant execute on function public.search_embedding_floor() to authenticated, service_role;

-- store_image_embedding ---------------------------------------------------------------------------

-- Stores the embedding of one image. The worker calls it after complete_analysis_job. A deleted image
-- updates no row, which is not an error, as in complete_analysis_job. A vector of the wrong size is
-- rejected by its type. An all-zero vector is rejected here, because its cosine distance is NaN and
-- NaN sorts before every number, so it would top every "find similar" list.
create function public.store_image_embedding(
  p_image_id bigint,
  p_embedding extensions.vector(384),
  p_model text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_embedding is null or p_model is null or p_model = '' then
    raise exception 'store_image_embedding needs an embedding and a model id'
      using errcode = '22023';
  end if;
  if extensions.vector_norm(p_embedding) = 0 then
    raise exception 'an embedding must not be the zero vector'
      using errcode = '22023';
  end if;

  update public.image_metadata
  set embedding = p_embedding,
      embedding_model = p_model
  where image_id = p_image_id;
end;
$$;

-- Only the worker stores embeddings. It runs with the secret key.
revoke execute on function public.store_image_embedding(bigint, extensions.vector, text)
  from public, anon, authenticated;
grant execute on function public.store_image_embedding(bigint, extensions.vector, text)
  to service_role;

-- Stale embeddings --------------------------------------------------------------------------------

-- An embedding describes the tags and description it was computed from. When a re-analysis changes
-- either, the old vector no longer matches, so drop it and let similar_images fall back to the tags
-- until the worker stores a new one. An update that sets a new embedding in the same statement
-- keeps it. Postgres doesn't check EXECUTE when a trigger fires, so no role needs the right to call it.
create function public.clear_stale_embedding() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.embedding := null;
  new.embedding_model := null;
  return new;
end;
$$;

revoke all on function public.clear_stale_embedding() from public, anon, authenticated, service_role;

create trigger image_metadata_clear_stale_embedding
  before update of tags, description on public.image_metadata
  for each row
  when (
    (old.tags is distinct from new.tags or old.description is distinct from new.description)
    and new.embedding is not distinct from old.embedding
  )
  execute function public.clear_stale_embedding();

-- Find similar ------------------------------------------------------------------------------------

-- Images like the target: 0.7 · semantic + 0.3 · palette closeness, best first. The target is read
-- through RLS, so someone else's id (or a missing one) finds no target and returns nothing.
-- The semantic term is the calibrated cosine similarity of the two embeddings when both images have
-- one from the same model, and otherwise tag_cosine on the word-level tag_lexemes of 20261020000006.
-- The weights, the threshold and the ordering are exactly as in 20261020000005_search.sql, and
-- `create or replace` keeps the grants (authenticated only). search_tag_weight() keeps its name
-- because renaming it would break callers, but it is now the weight of the semantic term.
-- `1 - (a <=> b)` is cosine similarity, since pgvector's <=> is cosine distance.
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
    select public.search_tag_weight() * case
        when target.embedding is not null
          and m.embedding is not null
          and target.embedding_model = m.embedding_model
          then greatest(
            0,
            ((1 - (target.embedding operator(extensions.<=>) m.embedding))
              - public.search_embedding_floor()) / (1 - public.search_embedding_floor())
          )
        else public.tag_cosine(target.tag_lexemes, m.tag_lexemes)
      end
      + public.search_color_weight() * public.color_similarity(target.colors_lab, m.colors_lab) as score
    offset 0
  ) as similarity
  where target.image_id = target_id
    and similarity.score > public.search_similar_min_score()
  order by similarity.score desc, i.id desc
  limit greatest(lim, 0) offset greatest(off, 0);
end;
