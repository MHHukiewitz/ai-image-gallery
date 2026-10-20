-- Analysis queue and result cache (docs/architecture.md sections 4 and 5).
-- The queue is image_metadata itself: a row is a job, ai_processing_status is its state. The worker
-- (secret key) claims, completes and fails jobs through the RPCs below. Users never update the
-- table; they ask for a retry through request_reanalysis.
--
-- Supabase's default privileges grant EXECUTE on every new function to anon, authenticated and
-- service_role, and PostgreSQL grants it to PUBLIC. Each function therefore revokes what it must
-- not hand out instead of relying on a bare GRANT.

-- ai_cache --------------------------------------------------------------------------------------

-- The worker's own cache of analyzer output, keyed by image content and analyzer, so the same
-- bytes are never sent to the AI provider twice. result is {"tags": [...], "description": "..."}.
create table public.ai_cache (
  content_sha256 char(64) not null,
  analyzer_id text not null,
  result jsonb not null,
  created_at timestamptz not null default now(),
  primary key (content_sha256, analyzer_id)
);

-- RLS on with no policies: even if a grant is ever widened, no user-facing role sees a row.
alter table public.ai_cache enable row level security;

-- Default privileges give every Data API role full access, service_role included. The worker only
-- upserts and reads, so it loses the rest (TRUNCATE ignores RLS).
revoke all on table public.ai_cache from anon, authenticated, service_role;
grant select, insert, update on table public.ai_cache to service_role;

-- claim_analysis_jobs ---------------------------------------------------------------------------

-- Hands the worker up to max_jobs jobs and marks them 'processing'. A job is claimable when it is
-- pending and due, or when it is 'processing' but its worker went quiet for stale_after_seconds
-- (a crash). Only jobs with attempts left are claimed. A stale job with none left is failed
-- instead, so a job that keeps crashing its worker can't loop forever.
-- SKIP LOCKED lets concurrent workers pass over each other's rows instead of waiting, so two
-- claims never return the same job.
create function public.claim_analysis_jobs(
  max_jobs int,
  stale_after_seconds int default 300,
  max_attempts int default 3
)
returns table (
  image_id bigint,
  user_id uuid,
  original_path text,
  content_sha256 text,
  attempts int
)
language sql
security definer
set search_path = ''
as $$
  update public.image_metadata m
  set ai_processing_status = 'failed',
      last_error = 'analysis timed out',
      claimed_at = null
  where m.ai_processing_status = 'processing'
    and m.claimed_at < now() - interval '1 second' * stale_after_seconds
    and m.attempts >= max_attempts;

  -- MATERIALIZED makes the row locks happen once, here, before the update joins to images.
  with picked as materialized (
    select m.id
    from public.image_metadata m
    where m.attempts < max_attempts
      and (
        (m.ai_processing_status = 'pending'
          and (m.next_attempt_at is null or m.next_attempt_at <= now()))
        or (m.ai_processing_status = 'processing'
          and m.claimed_at < now() - interval '1 second' * stale_after_seconds)
      )
    order by m.created_at, m.id
    limit max_jobs
    for update of m skip locked
  )
  update public.image_metadata m
  set ai_processing_status = 'processing',
      attempts = m.attempts + 1,
      claimed_at = now()
  from picked, public.images i
  where m.id = picked.id
    and i.id = m.image_id
  returning m.image_id, m.user_id, i.original_path, i.content_sha256::text, m.attempts;
$$;

-- complete_analysis_job -------------------------------------------------------------------------

-- Stores a finished analysis. Updating no row is not an error: the user may have deleted the image
-- while the worker was analyzing it.
create function public.complete_analysis_job(
  p_image_id bigint,
  p_tags text[],
  p_description text,
  p_colors text[],
  p_analyzer_id text
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.image_metadata
  set ai_processing_status = 'done',
      tags = p_tags,
      description = p_description,
      colors = p_colors,
      analyzer_id = p_analyzer_id,
      last_error = null,
      claimed_at = null
  where image_id = p_image_id;
$$;

-- fail_analysis_job -----------------------------------------------------------------------------

-- Records a failed attempt. A retryable failure with attempts left goes back to 'pending' and
-- waits 10 s, 20 s, 40 s ... (doubling per attempt already used) before it is claimable again.
-- Anything else is final. The colors are stored either way: they come from a local step that
-- doesn't depend on the AI provider, so a failed job still gets its color search.
create function public.fail_analysis_job(
  p_image_id bigint,
  p_error text,
  p_colors text[],
  p_retryable boolean,
  p_max_attempts int default 3
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.image_metadata m
  set colors = p_colors,
      last_error = p_error,
      claimed_at = null,
      ai_processing_status = case
        when p_retryable and m.attempts < p_max_attempts then 'pending' else 'failed'
      end,
      next_attempt_at = case
        when p_retryable and m.attempts < p_max_attempts
          then now() + interval '10 seconds' * power(2, m.attempts - 1)
      end
  where m.image_id = p_image_id;
$$;

-- request_reanalysis ----------------------------------------------------------------------------

-- Lets an owner re-queue their own image (the UI's "Retry"). Users have no UPDATE on
-- image_metadata, so this is the only way they change a job. A job being processed is left alone:
-- resetting it would let a second worker pick it up while the first is still running.
-- Returns whether a row changed, so the API can tell "not yours / busy / missing" from success.
create function public.request_reanalysis(p_image_id bigint)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.image_metadata m
  set ai_processing_status = 'pending',
      attempts = 0,
      next_attempt_at = null,
      last_error = null,
      claimed_at = null
  where m.image_id = p_image_id
    and m.user_id = (select auth.uid())
    and m.ai_processing_status <> 'processing';

  return found;
end;
$$;

-- Grants ----------------------------------------------------------------------------------------

revoke execute on function public.claim_analysis_jobs(int, int, int)
  from public, anon, authenticated;
revoke execute on function public.complete_analysis_job(bigint, text[], text, text[], text)
  from public, anon, authenticated;
revoke execute on function public.fail_analysis_job(bigint, text, text[], boolean, int)
  from public, anon, authenticated;

grant execute on function public.claim_analysis_jobs(int, int, int) to service_role;
grant execute on function public.complete_analysis_job(bigint, text[], text, text[], text)
  to service_role;
grant execute on function public.fail_analysis_job(bigint, text, text[], boolean, int)
  to service_role;

-- Authenticated only: the function reads auth.uid(), which is null for the worker.
revoke execute on function public.request_reanalysis(bigint) from public, anon, service_role;
grant execute on function public.request_reanalysis(bigint) to authenticated;
