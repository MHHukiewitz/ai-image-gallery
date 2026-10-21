-- A generic job queue (docs/architecture.md sections 4 and 5, #89).
-- The analysis queue used to be image_metadata's own columns, which tied it to one kind of work.
-- public.jobs holds any kind of background work, claimed through kind-agnostic RPCs. Analysis is
-- the first kind, 'analyze_image'. image_metadata stays its read model: the API, the web and the
-- realtime channel keep reading ai_processing_status, last_error and analyzer_id, which a trigger
-- keeps in step with the job.
--
-- Additive and backward compatible. Production applies this on merge, while the old worker still
-- runs on claim_analysis_jobs, complete_analysis_job and fail_analysis_job, which this migration
-- doesn't change. Until the new worker is deployed:
--   * an upload or a request_reanalysis still sets image_metadata to 'pending', so the old worker
--     analyzes it as before. It also gets an 'analyze_image' job.
--   * that job stays 'pending', because the old RPCs never touch jobs. When the new worker claims
--     it, it finds the analysis already finished and closes the job without analyzing again.
-- The old columns and RPCs stay until a later migration drops them.
--
-- Lock order: a function that changes both a job and its image's metadata row locks the job first,
-- as claim_jobs does (its trigger then updates the metadata row). The other order could deadlock
-- with a claim.
--
-- Supabase's default privileges grant every Data API role full access to a new table and EXECUTE
-- on a new function, and PostgreSQL grants EXECUTE to PUBLIC. Everything below revokes what it must
-- not hand out instead of relying on a bare GRANT.

-- jobs ------------------------------------------------------------------------------------------

create table public.jobs (
  id bigint generated always as identity primary key,
  kind text not null check (kind <> ''),
  -- Null for a kind that isn't about one image.
  image_id bigint references public.images (id) on delete cascade,
  user_id uuid references auth.users (id) on delete cascade,
  status text not null default 'pending'
    check (status in ('pending', 'processing', 'done', 'failed')),
  attempts int not null default 0 check (attempts >= 0),
  -- A pending job is claimable once this has passed. A retry pushes it out by its backoff.
  run_after timestamptz not null default now(),
  claimed_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- At most one open job per kind and image, so the same work is never queued twice. Finished jobs
-- stay as a record and don't count. Nulls never conflict, so jobs without an image are not limited.
create unique index jobs_one_open_job_per_kind_and_image_idx
  on public.jobs (kind, image_id)
  where status in ('pending', 'processing');

-- claim_jobs walks the open jobs oldest first.
create index jobs_open_created_at_idx
  on public.jobs (created_at, id)
  where status in ('pending', 'processing');

-- Deleting an image cascades to its jobs, finished ones included.
create index jobs_image_id_idx
  on public.jobs (image_id);

create trigger jobs_set_updated_at
  before update on public.jobs
  for each row execute function public.set_updated_at();

-- RLS on with no policies: no user-facing role sees or changes a job, even if a grant is ever
-- widened. The worker changes jobs only through the security definer RPCs below. It may read them,
-- which operations and the tests use.
alter table public.jobs enable row level security;

revoke all on table public.jobs from anon, authenticated, service_role;
revoke all on sequence public.jobs_id_seq from anon, authenticated, service_role;
grant select on table public.jobs to service_role;

-- image_metadata as the read model --------------------------------------------------------------

-- Mirrors an analyze_image job's state onto its image's metadata row. The old queue columns
-- (attempts, next_attempt_at, claimed_at) follow too, so the old worker could take over again
-- after a rollback.
-- A finished analysis (done or failed) is left alone: only an upload or request_reanalysis reopens
-- it. That keeps the old worker's result when the new worker claims a job the old worker already
-- finished, and lets complete_analysis write its results and 'done' in one update.
-- next_attempt_at is only set while a retry waits, as fail_analysis_job set it.
-- It runs as the caller, because only the security definer RPCs below update jobs. Postgres doesn't
-- check EXECUTE when a trigger fires, so no role needs the right to call it.
create function public.mirror_analysis_job() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  update public.image_metadata m
  set ai_processing_status = new.status,
      attempts = new.attempts,
      next_attempt_at = case
        when new.status = 'pending' and new.run_after > now() then new.run_after
      end,
      claimed_at = new.claimed_at,
      last_error = new.last_error
  where m.image_id = new.image_id
    and m.ai_processing_status in ('pending', 'processing');
  return null;
end;
$$;

revoke all on function public.mirror_analysis_job() from public, anon, authenticated, service_role;

create trigger jobs_mirror_analysis
  after update of status, attempts, run_after, claimed_at, last_error on public.jobs
  for each row
  when (new.kind = 'analyze_image')
  execute function public.mirror_analysis_job();

-- Enqueueing ------------------------------------------------------------------------------------

-- The API inserts a new image's metadata row as 'pending', as the user. This gives it its analysis
-- job. It is security definer because users have no privileges on jobs. RLS has already checked
-- that the row and its image are the user's own.
create function public.enqueue_new_analysis() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.jobs (kind, image_id, user_id)
  values ('analyze_image', new.image_id, new.user_id)
  on conflict (kind, image_id) where status in ('pending', 'processing') do nothing;
  return null;
end;
$$;

revoke all on function public.enqueue_new_analysis() from public, anon, authenticated, service_role;

create trigger image_metadata_enqueue_analysis
  after insert on public.image_metadata
  for each row
  when (new.ai_processing_status = 'pending')
  execute function public.enqueue_new_analysis();

-- The same name, signature, result and grants as in 20261020000004_queue.sql, so the API doesn't
-- change. It also queues a job now, or resets the open one. It still refuses while the analysis is
-- processing in either queue: the open job can't be queued a second time, and resetting a running
-- analysis would let a second worker start on it.
create or replace function public.request_reanalysis(p_image_id bigint)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_job_status text;
begin
  -- The open job first (see the lock order above). FOR UPDATE waits for a claim in flight, so a
  -- job that is being claimed right now is seen as 'processing'.
  select j.status into v_job_status
  from public.jobs j
  where j.kind = 'analyze_image'
    and j.image_id = p_image_id
    and j.user_id = (select auth.uid())
    and j.status in ('pending', 'processing')
  for update;

  if v_job_status = 'processing' then
    return false;
  end if;

  update public.image_metadata m
  set ai_processing_status = 'pending',
      attempts = 0,
      next_attempt_at = null,
      last_error = null,
      claimed_at = null
  where m.image_id = p_image_id
    and m.user_id = (select auth.uid())
    and m.ai_processing_status <> 'processing';

  if not found then
    return false;
  end if;

  insert into public.jobs (kind, image_id, user_id)
  values ('analyze_image', p_image_id, (select auth.uid()))
  on conflict (kind, image_id) where status in ('pending', 'processing')
  do update set attempts = 0, run_after = now(), last_error = null;

  return true;
end;
$$;

-- claim_jobs ------------------------------------------------------------------------------------

-- Hands the worker up to p_limit jobs of the kinds it can run, and marks them 'processing'. A job
-- is claimable when it is pending and due, or when it is 'processing' but its worker went quiet for
-- p_stale_after_seconds (a crash). Only jobs with attempts left are claimed. A stale job with none
-- left is failed instead, so a job that keeps crashing its worker can't loop forever.
-- SKIP LOCKED lets concurrent workers pass over each other's rows instead of waiting, so two
-- claims never return the same job.
-- The worker names its kinds, so it never takes a job it has no handler for, such as a new kind
-- that a migration starts queueing before the worker that runs it is deployed.
create function public.claim_jobs(
  p_kinds text[],
  p_limit int,
  p_stale_after_seconds int default 300,
  p_max_attempts int default 3
)
returns table (
  id bigint,
  kind text,
  image_id bigint,
  user_id uuid,
  attempts int
)
language sql
security definer
set search_path = ''
as $$
  update public.jobs j
  set status = 'failed',
      last_error = 'Timed out',
      claimed_at = null
  where j.kind = any (p_kinds)
    and j.status = 'processing'
    and j.claimed_at < now() - interval '1 second' * p_stale_after_seconds
    and j.attempts >= p_max_attempts;

  -- MATERIALIZED makes the row locks happen once, here, before the update.
  with picked as materialized (
    select j.id
    from public.jobs j
    where j.kind = any (p_kinds)
      and j.attempts < p_max_attempts
      and (
        (j.status = 'pending' and j.run_after <= now())
        or (j.status = 'processing'
          and j.claimed_at < now() - interval '1 second' * p_stale_after_seconds)
      )
    order by j.created_at, j.id
    limit p_limit
    for update of j skip locked
  )
  update public.jobs j
  set status = 'processing',
      attempts = j.attempts + 1,
      claimed_at = now()
  from picked
  where j.id = picked.id
  returning j.id, j.kind, j.image_id, j.user_id, j.attempts;
$$;

-- complete_job ----------------------------------------------------------------------------------

-- Marks a job done. A missing job is not an error: deleting an image deletes its jobs.
create function public.complete_job(p_job_id bigint)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.jobs
  set status = 'done',
      last_error = null,
      claimed_at = null
  where id = p_job_id;
$$;

-- fail_job --------------------------------------------------------------------------------------

-- Records a failed attempt, with the same backoff as fail_analysis_job. A retryable failure with
-- attempts left goes back to 'pending' and waits 10 s, 20 s, 40 s ... (doubling per attempt
-- already used) before it is claimable again. Anything else is final.
create function public.fail_job(
  p_job_id bigint,
  p_error text,
  p_max_attempts int default 3,
  p_retryable boolean default true
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.jobs j
  set last_error = p_error,
      claimed_at = null,
      status = case
        when p_retryable and j.attempts < p_max_attempts then 'pending' else 'failed'
      end,
      run_after = case
        when p_retryable and j.attempts < p_max_attempts
          then now() + interval '10 seconds' * power(2, j.attempts - 1)
        else j.run_after
      end
  where j.id = p_job_id;
$$;

-- analyze_image ---------------------------------------------------------------------------------

-- What the worker needs to analyze the image of an analyze_image job. No row means the image was
-- deleted, and the job with it. ai_processing_status tells whether the analysis already ended
-- outside this job: the old worker may have finished it while the job waited.
create function public.analysis_target(p_job_id bigint)
returns table (
  image_id bigint,
  original_path text,
  content_sha256 text,
  ai_processing_status text,
  last_error text
)
language sql
stable
security definer
set search_path = ''
as $$
  select i.id, i.original_path, i.content_sha256::text, m.ai_processing_status::text, m.last_error
  from public.jobs j
  join public.images i on i.id = j.image_id
  join public.image_metadata m on m.image_id = j.image_id
  where j.id = p_job_id
    and j.kind = 'analyze_image';
$$;

-- Stores a finished analysis and completes its job, in one transaction. The metadata row gets the
-- results and 'done' in one update, so the job's mirror then finds it finished and leaves it be.
-- A missing job is not an error: the user may have deleted the image during the analysis.
create function public.complete_analysis(
  p_job_id bigint,
  p_tags text[],
  p_description text,
  p_colors text[],
  p_analyzer_id text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_image_id bigint;
begin
  -- The job first (see the lock order above).
  select j.image_id into v_image_id
  from public.jobs j
  where j.id = p_job_id
    and j.kind = 'analyze_image'
  for update;

  if not found then
    return;
  end if;

  update public.image_metadata
  set ai_processing_status = 'done',
      tags = p_tags,
      description = p_description,
      colors = p_colors,
      analyzer_id = p_analyzer_id,
      last_error = null,
      next_attempt_at = null,
      claimed_at = null
  where image_id = v_image_id;

  perform public.complete_job(p_job_id);
end;
$$;

-- Records a failed analysis attempt. fail_job decides between a retry and the end, and the mirror
-- carries that onto the image. The colors are stored either way: they come from a local step that
-- doesn't depend on the AI provider, so a failed analysis still gets its color search.
create function public.fail_analysis(
  p_job_id bigint,
  p_error text,
  p_colors text[],
  p_retryable boolean,
  p_max_attempts int default 3
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_image_id bigint;
begin
  -- The job first (see the lock order above).
  select j.image_id into v_image_id
  from public.jobs j
  where j.id = p_job_id
    and j.kind = 'analyze_image'
  for update;

  if not found then
    return;
  end if;

  update public.image_metadata
  set colors = p_colors
  where image_id = v_image_id;

  perform public.fail_job(p_job_id, p_error, p_max_attempts, p_retryable);
end;
$$;

-- Backfill --------------------------------------------------------------------------------------

-- Every analysis still to do gets its job: the pending ones, and the processing ones in case their
-- worker never finishes them. The old worker may well finish them first, and the new worker then
-- closes their jobs without analyzing. Each job starts with a fresh attempt budget, because the
-- old attempts counted against the old queue. A waiting retry keeps its backoff, and created_at
-- keeps the old queue's order.
-- It runs after the enqueue trigger exists, so an upload during the migration is covered by one of
-- the two, and ON CONFLICT keeps it from getting both.
insert into public.jobs (kind, image_id, user_id, run_after, created_at)
select 'analyze_image', m.image_id, m.user_id, coalesce(m.next_attempt_at, now()), m.created_at
from public.image_metadata m
where m.ai_processing_status in ('pending', 'processing')
on conflict (kind, image_id) where status in ('pending', 'processing') do nothing;

-- Grants ----------------------------------------------------------------------------------------

-- The worker's RPCs: service_role only.
revoke execute on function
  public.claim_jobs(text[], int, int, int),
  public.complete_job(bigint),
  public.fail_job(bigint, text, int, boolean),
  public.analysis_target(bigint),
  public.complete_analysis(bigint, text[], text, text[], text),
  public.fail_analysis(bigint, text, text[], boolean, int)
from public, anon, authenticated;

grant execute on function
  public.claim_jobs(text[], int, int, int),
  public.complete_job(bigint),
  public.fail_job(bigint, text, int, boolean),
  public.analysis_target(bigint),
  public.complete_analysis(bigint, text[], text, text[], text),
  public.fail_analysis(bigint, text, text[], boolean, int)
to service_role;
