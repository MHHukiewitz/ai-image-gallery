-- Semantic text search (docs/architecture.md section 8).
-- search_images matches words only, so "puppy" never finds a photo tagged "dog". The API now embeds the
-- query with the same local model that embeds the images (20261020000008_image_embeddings.sql) and
-- calls search_images_semantic, which keeps every word match and adds the images whose embedding is
-- close to the query's. search_images stays as it is: the API calls it when it has no query embedding
-- (EMBEDDER=none, or the model failed), and a second overload would be ambiguous to PostgREST.
--
-- Additive and backward-compatible: three constants and one new function. Nothing existing changes.

-- Tunables ----------------------------------------------------------------------------------------
-- Each constant is an immutable function, as in 20261020000005_search.sql. All three were calibrated
-- with the real model on short queries against description-and-tags texts (docs/architecture.md
-- section 8, "Text search"), and calibrating again is needed if the model or the query text changes.

-- SEMANTIC_MIN: the calibrated similarity (search_embedding_floor() applied: 0 at the floor, 1 for
-- identical vectors) an image needs to be a candidate without matching a word. It is what keeps
-- unrelated photos out: 0.20 is a cosine of 0.72 between "Tags: <query>" and the image's text.
create function public.search_semantic_min() returns float8
language sql immutable parallel safe
return 0.20;

-- TEXT_WEIGHT: share of the score from the word match, rank / (rank + 1), which grows from 0 toward 1.
-- It is twice SEMANTIC_WEIGHT, so an image that matches the words outranks one that matches only by
-- meaning, and meaning breaks ties among word matches.
create function public.search_text_weight() returns float8
language sql immutable parallel safe
return 1.0;

-- SEMANTIC_WEIGHT: share of the score from the calibrated similarity. Related photos reach 0.2 to 0.5.
create function public.search_semantic_weight() returns float8
language sql immutable parallel safe
return 0.5;

-- Same grants as the other constants in 20261020000005_search.sql.
revoke all on function
  public.search_semantic_min(),
  public.search_text_weight(),
  public.search_semantic_weight()
  from public, anon, authenticated, service_role;

grant execute on function
  public.search_semantic_min(),
  public.search_text_weight(),
  public.search_semantic_weight()
  to authenticated, service_role;

-- search_images_semantic --------------------------------------------------------------------------

-- Text search by words and by meaning. An image is a candidate when every word of q matches it as a
-- prefix (exactly as in search_images), or when its embedding is calibrated-similar to q_embedding by
-- at least search_semantic_min(). Score = text weight · rank / (rank + 1) + semantic weight · similarity
-- + search_exact_tag_boost() when q is exactly one of the image's tags. Best first, newest first among
-- equals, so paging is stable.
-- The similarity is 0 for an image without an embedding (an upload not embedded yet) and for a NULL or
-- all-zero q_embedding, so those rank on words alone. Blank input, or input without a single word,
-- returns nothing, as in search_images. q_embedding must come from the model that embedded the images:
-- the function can't tell, so changing EMBEDDING_MODEL needs the backfill before the API restarts.
create function public.search_images_semantic(
  q text,
  q_embedding extensions.vector(384),
  lim int default 20,
  off int default 0
)
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
  with parsed as (
    select
      to_tsquery('english', string_agg(word || ':*', ' & ')) as tsquery,
      lower(btrim(q)) as exact_tag,
      -- An all-zero vector has a NaN cosine distance, which sorts above every number.
      case
        when q_embedding is not null and extensions.vector_norm(q_embedding) > 0 then q_embedding
      end as embedding
    from regexp_split_to_table(lower(q), '[^[:alnum:]]+') as word
    where word <> ''
  )
  select
    i.id, i.user_id, i.filename, i.original_path, i.thumbnail_path, i.mime_type, i.size_bytes,
    i.width, i.height, i.content_sha256, i.uploaded_at,
    m.description, m.tags, m.colors, m.ai_processing_status, m.last_error,
    scored.score
  from parsed
  cross join public.image_metadata m
  join public.images i on i.id = m.image_id
  cross join lateral (
    -- offset 0 keeps the planner from copying these expressions into the WHERE and ORDER BY below,
    -- which would compute the rank and the cosine up to three times per image.
    select
      -- NULL when the words don't match, which is what tells a word match from a rank of 0.
      case
        when m.search_vector @@ parsed.tsquery then ts_rank_cd(m.search_vector, parsed.tsquery)
      end as rank,
      case
        when parsed.embedding is not null and m.embedding is not null then greatest(
          0,
          ((1 - (m.embedding operator(extensions.<=>) parsed.embedding))
            - public.search_embedding_floor()) / (1 - public.search_embedding_floor())
        )
        else 0
      end as similarity
    offset 0
  ) as signals
  cross join lateral (
    select
      public.search_text_weight() * coalesce(signals.rank / (signals.rank + 1), 0)
        + public.search_semantic_weight() * signals.similarity
        + case
            when exists (select 1 from unnest(m.tags) as tag where lower(tag) = parsed.exact_tag)
              then public.search_exact_tag_boost()
            else 0
          end as score
    offset 0
  ) as scored
  where parsed.tsquery is not null
    and (signals.rank is not null or signals.similarity >= public.search_semantic_min())
  order by scored.score desc, i.uploaded_at desc, i.id desc
  limit greatest(lim, 0) offset greatest(off, 0);
end;

-- Only signed-in users search, as for search_images.
revoke all on function public.search_images_semantic(text, extensions.vector, int, int)
  from public, anon, authenticated, service_role;

grant execute on function public.search_images_semantic(text, extensions.vector, int, int)
  to authenticated;
