-- The generic job queue (20261020000014_jobs.sql): the jobs table, its RPCs, analysis on top of
-- them, enqueueing and the backfill. Run with `supabase test db`. Everything happens in one
-- transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000089a1 owns every fixture image except b-img.
-- User B = 00000000-0000-4000-8000-0000000089b1 owns b-img.
-- now() is frozen inside a transaction, so fixtures are placed relative to it and every claim in
-- this file sees the same clock. The queue sections run as service_role, the worker's role.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.
-- One transaction can't hold two claims at once, so SKIP LOCKED itself is checked by the
-- integration test test_concurrent_claims_never_share_a_job and by hand (see the PR). Here, two
-- claims in a row must not return the same job.

begin;

select plan(95);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

-- A claim takes every claimable job of its kinds, so start from an empty queue even on a dev
-- database with real uploads. The rollback at the end puts everything back.
delete from public.images;
delete from public.jobs;

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000089a1', 'jobs-a@example.test'),
  ('00000000-0000-4000-8000-0000000089b1', 'jobs-b@example.test');

-- An image's filename is its handle, because ids are generated.
create function pg_temp.add_image(owner uuid, slug text)
returns bigint
language sql
as $$
  insert into public.images (
    user_id, filename, original_path, thumbnail_path,
    mime_type, size_bytes, width, height, content_sha256
  )
  values (
    owner, slug || '.jpg',
    format('%s/%s/original.jpg', owner, slug), format('%s/%s/thumb.webp', owner, slug),
    'image/jpeg', 1024, 640, 480, md5(slug) || md5(slug)
  )
  returning id
$$;

create function pg_temp.image_id(slug text)
returns bigint
language sql
as $$ select id from public.images where filename = slug || '.jpg' $$;

create function pg_temp.metadata(slug text)
returns setof public.image_metadata
language sql
as $$
  select m.*
  from public.image_metadata m
  join public.images i on i.id = m.image_id
  where i.filename = slug || '.jpg'
$$;

-- The image's newest analysis job.
create function pg_temp.analysis_job(slug text)
returns setof public.jobs
language sql
as $$
  select j.*
  from public.jobs j
  join public.images i on i.id = j.image_id
  where i.filename = slug || '.jpg'
    and j.kind = 'analyze_image'
  order by j.id desc
  limit 1
$$;

create function pg_temp.open_analysis_jobs(slug text)
returns bigint
language sql
as $$
  select count(*)
  from public.jobs j
  join public.images i on i.id = j.image_id
  where i.filename = slug || '.jpg'
    and j.kind = 'analyze_image'
    and j.status in ('pending', 'processing')
$$;

-- Jobs that are about no image are looked up through this table.
create temp table fixture_job (slug text primary key, id bigint not null);
grant select on fixture_job to service_role;

create function pg_temp.add_job(
  p_slug text, p_kind text, p_status text, p_attempts int, p_run_after timestamptz,
  p_claimed_at timestamptz, p_last_error text, p_created_at timestamptz
)
returns void
language sql
as $$
  with inserted as (
    insert into public.jobs (kind, status, attempts, run_after, claimed_at, last_error, created_at)
    values (p_kind, p_status, p_attempts, p_run_after, p_claimed_at, p_last_error, p_created_at)
    returning id
  )
  insert into fixture_job (slug, id) select p_slug, id from inserted
$$;

create function pg_temp.job_id(p_slug text)
returns bigint
language sql
as $$ select id from fixture_job where slug = p_slug $$;

create function pg_temp.job(p_slug text)
returns setof public.jobs
language sql
as $$ select j.* from public.jobs j join fixture_job f on f.id = j.id where f.slug = p_slug $$;

-- The generic queue, on a kind of its own. created_at fixes the claim order (oldest first).
select pg_temp.add_job(slug, kind, status, attempts, run_after, claimed_at, last_error, created_at)
from (
  values
    -- Oldest of its kind, but not due for another minute: a claim must skip it.
    ('future', 'test_claim', 'pending', 1, now() + interval '1 minute', null::timestamptz,
      null::text, now() - interval '9 minutes'),
    -- Worker died long ago with no attempts left: the claim must fail it, not retry it.
    ('stale-exhausted', 'test_claim', 'processing', 3, now(), now() - interval '10 minutes',
      'rate limited', now() - interval '8 minutes'),
    -- Worker died long ago with attempts left: the claim must take it over.
    ('stale-retry', 'test_claim', 'processing', 1, now(), now() - interval '10 minutes', null,
      now() - interval '7 minutes'),
    ('due-old', 'test_claim', 'pending', 0, now() - interval '6 minutes', null, null,
      now() - interval '6 minutes'),
    -- A retry whose backoff has passed.
    ('due-retry', 'test_claim', 'pending', 1, now() - interval '1 minute', null, 'busy',
      now() - interval '5 minutes'),
    -- Claimed 10 s ago: alive under the default 300 s timeout, stale under a 5 s one.
    ('busy', 'test_claim', 'processing', 1, now(), now() - interval '10 seconds', null,
      now() - interval '4 minutes'),
    ('busy-b', 'test_claim', 'processing', 2, now(), now() - interval '10 seconds', null,
      now() - interval '3 minutes'),
    ('done-job', 'test_claim', 'done', 1, now(), null, null, now() - interval '2 minutes'),
    -- The oldest job of all, but of a kind the claims below don't ask for.
    ('other-kind', 'test_other', 'pending', 0, now(), null, null, now() - interval '1 hour'),
    ('complete-me', 'test_claim', 'processing', 2, now(), now(), 'earlier failure', now()),
    ('fail-first', 'test_claim', 'processing', 1, now(), now(), null, now()),
    ('fail-second', 'test_claim', 'processing', 2, now(), now(), null, now()),
    ('fail-last', 'test_claim', 'processing', 3, now(), now(), null, now()),
    ('fail-fatal', 'test_claim', 'processing', 1, now(), now(), null, now()),
    ('fail-cap', 'test_claim', 'processing', 2, now(), now(), null, now())
) as fixture (slug, kind, status, attempts, run_after, claimed_at, last_error, created_at);

-- The analysis queue. Each image gets a metadata row in the given state. Inserting a 'pending' row
-- queues its job through the trigger under test. A job 'processing' is inserted by hand.
select pg_temp.add_image(owner, slug)
from (
  values
    ('00000000-0000-4000-8000-0000000089a1'::uuid, 'claim-me'),
    ('00000000-0000-4000-8000-0000000089a1', 'leftover'),
    ('00000000-0000-4000-8000-0000000089a1', 'old-busy'),
    ('00000000-0000-4000-8000-0000000089a1', 'busy-img'),
    ('00000000-0000-4000-8000-0000000089a1', 'stale-analysis'),
    ('00000000-0000-4000-8000-0000000089a1', 'complete-me'),
    ('00000000-0000-4000-8000-0000000089a1', 'fail-retry'),
    ('00000000-0000-4000-8000-0000000089a1', 'fail-final'),
    ('00000000-0000-4000-8000-0000000089a1', 'done-img'),
    ('00000000-0000-4000-8000-0000000089a1', 'failed-img'),
    ('00000000-0000-4000-8000-0000000089a1', 'deleted-img'),
    ('00000000-0000-4000-8000-0000000089b1', 'b-img')
) as fixture (owner, slug);

insert into public.image_metadata (
  image_id, user_id, ai_processing_status, attempts, claimed_at, last_error, created_at
)
select
  i.id, i.user_id, fixture.status, fixture.attempts, fixture.claimed_at, fixture.last_error,
  fixture.created_at
from (
  values
    ('claim-me', 'pending', 0, null::timestamptz, null::text, now() - interval '3 minutes'),
    -- Pending when the migration applied: the old worker finishes it while its job waits.
    ('leftover', 'pending', 0, null, null, now() - interval '2 minutes'),
    -- The old worker is analyzing it (its claim set the old columns), and its job waits.
    ('old-busy', 'pending', 0, null, null, now() - interval '1 minute'),
    ('busy-img', 'processing', 1, now(), null, now()),
    ('stale-analysis', 'processing', 3, now() - interval '10 minutes', null, now()),
    ('complete-me', 'processing', 2, now(), 'earlier failure', now()),
    ('fail-retry', 'processing', 1, now(), null, now()),
    ('fail-final', 'processing', 3, now(), null, now()),
    ('done-img', 'done', 1, null, null, now()),
    ('failed-img', 'failed', 3, null, 'boom', now()),
    ('deleted-img', 'processing', 1, now(), null, now()),
    ('b-img', 'done', 1, null, null, now())
) as fixture (slug, status, attempts, claimed_at, last_error, created_at)
join public.images i on i.filename = fixture.slug || '.jpg';

-- The trigger stamps each job with now(), so order the three queued ones as their images were.
update public.jobs j
set created_at = m.created_at
from public.image_metadata m
where m.image_id = j.image_id;

update public.image_metadata
set ai_processing_status = 'processing', attempts = 1, claimed_at = now()
where image_id = pg_temp.image_id('old-busy');

insert into public.jobs (kind, image_id, user_id, status, attempts, claimed_at)
select 'analyze_image', m.image_id, m.user_id, 'processing', m.attempts, m.claimed_at
from public.image_metadata m
join public.images i on i.id = m.image_id
where i.filename in (
  'busy-img.jpg', 'stale-analysis.jpg', 'complete-me.jpg', 'fail-retry.jpg', 'fail-final.jpg',
  'deleted-img.jpg'
);

-- A can't read B's rows, so remember B's id for the statements that run as A.
select set_config('tests.b_img_id', pg_temp.image_id('b-img')::text, true);

-- Structure -------------------------------------------------------------------------------------

select has_table('public', 'jobs', 'jobs exists');
select columns_are(
  'public', 'jobs',
  array[
    'id', 'kind', 'image_id', 'user_id', 'status', 'attempts', 'run_after', 'claimed_at',
    'last_error', 'created_at', 'updated_at'
  ],
  'jobs has a kind, an optional image and owner, the queue state and timestamps'
);
select col_is_null('public', 'jobs', 'image_id', 'a job need not be about an image');
select fk_ok('public', 'jobs', 'image_id', 'public', 'images', 'id', 'a job''s image is an image');
select ok(
  (select relrowsecurity from pg_class where oid = 'public.jobs'::regclass),
  'RLS is enabled on jobs'
);
select policies_are('public', 'jobs', array[]::text[], 'jobs has no policies');
select table_privs_are('public', 'jobs', 'anon', array[]::text[], 'anon has no privileges on jobs');
select table_privs_are(
  'public', 'jobs', 'authenticated', array[]::text[],
  'authenticated has no privileges on jobs'
);
select table_privs_are(
  'public', 'jobs', 'service_role', array['SELECT'],
  'service_role can read jobs, and changes them only through the RPCs'
);

-- Catalog names have the C collation; results_eq needs a collation both sides agree on.
select results_eq(
  $$select proname::text collate "default", prosecdef, 'search_path=""' = any (proconfig)
    from pg_proc
    where pronamespace = 'public'::regnamespace
      and proname in ('claim_jobs', 'complete_job', 'fail_job', 'analysis_target',
                      'complete_analysis', 'fail_analysis', 'request_reanalysis')
    order by proname$$,
  $$values
    ('analysis_target', true, true),
    ('claim_jobs', true, true),
    ('complete_analysis', true, true),
    ('complete_job', true, true),
    ('fail_analysis', true, true),
    ('fail_job', true, true),
    ('request_reanalysis', true, true)$$,
  'the RPCs are security definer with an empty search_path'
);

-- PUBLIC's default EXECUTE counts here: has_function_privilege covers every way a role can hold it.
select results_eq(
  $$select p.proname::text collate "default",
           bool_or(has_function_privilege('anon', p.oid, 'execute')),
           bool_or(has_function_privilege('authenticated', p.oid, 'execute')),
           bool_and(has_function_privilege('service_role', p.oid, 'execute'))
    from pg_proc p
    where p.pronamespace = 'public'::regnamespace
      and p.proname in ('claim_jobs', 'complete_job', 'fail_job', 'analysis_target',
                        'complete_analysis', 'fail_analysis')
    group by p.proname
    order by p.proname$$,
  $$values
    ('analysis_target', false, false, true),
    ('claim_jobs', false, false, true),
    ('complete_analysis', false, false, true),
    ('complete_job', false, false, true),
    ('fail_analysis', false, false, true),
    ('fail_job', false, false, true)$$,
  'only service_role runs the job RPCs'
);
select ok(
  pg_get_functiondef('public.claim_jobs(text[], int, int, int)'::regprocedure)
    ~* 'for update of j skip locked',
  'a claim skips the jobs another claim has locked instead of waiting for them'
);

-- claim_jobs ------------------------------------------------------------------------------------

set local role service_role;

create temp table claim_1 as select * from public.claim_jobs(array['test_claim'], 2);

select results_eq(
  $$select f.slug from claim_1 c join fixture_job f on f.id = c.id order by f.slug$$,
  array['due-old', 'stale-retry'],
  'a claim returns the oldest claimable jobs, up to p_limit: stale processing and due pending'
);
select results_eq(
  $$select kind, image_id, user_id, attempts from claim_1 where id = pg_temp.job_id('due-old')$$,
  $$values ('test_claim', null::bigint, null::uuid, 1)$$,
  'a claim returns the job''s kind, image, owner and the attempt it is on'
);
select results_eq(
  $$select slug, (j).status, (j).attempts, (j).claimed_at = now()
    from (select s as slug, pg_temp.job(s) as j from unnest(array['due-old', 'stale-retry']) as s)
      as claimed
    order by slug$$,
  $$values ('due-old', 'processing', 1, true), ('stale-retry', 'processing', 2, true)$$,
  'claimed jobs are processing, with one more attempt used and a fresh claim time'
);
select results_eq(
  $$select status, attempts, last_error, claimed_at is null from pg_temp.job('stale-exhausted')$$,
  $$values ('failed', 3, 'Timed out', true)$$,
  'a stale job with no attempts left is failed with "Timed out", not claimed'
);

create temp table claim_2 as select * from public.claim_jobs(array['test_claim'], 10);

select results_eq(
  $$select f.slug from claim_2 c join fixture_job f on f.id = c.id$$,
  array['due-retry'],
  'a second claim gets only what the first left behind: a retry whose backoff has passed'
);
select is_empty(
  'select id from claim_1 intersect select id from claim_2',
  'two claims never return the same job'
);
select is_empty(
  $$select id from public.claim_jobs(array['test_claim'], 10)$$,
  'a third claim finds nothing: the rest is future-dated, still alive or finished'
);
select results_eq(
  $$select slug, (j).status, (j).attempts
    from (
      select s as slug, pg_temp.job(s) as j
      from unnest(array['future', 'busy', 'busy-b', 'done-job', 'other-kind']) as s
    ) as untouched
    order by slug$$,
  $$values
    ('busy', 'processing', 1),
    ('busy-b', 'processing', 2),
    ('done-job', 'done', 1),
    ('future', 'pending', 1),
    ('other-kind', 'pending', 0)$$,
  'the claims left every other job as it was, including the older one of another kind'
);
select results_eq(
  $$select f.slug from public.claim_jobs(array['test_other', 'test_claim'], 10) c
    join fixture_job f on f.id = c.id$$,
  array['other-kind'],
  'a claim takes every kind it names'
);

-- The clock moving past run_after is what makes a backed-off job claimable.
reset role;
update public.jobs set run_after = now() - interval '1 second' where id = pg_temp.job_id('future');
set local role service_role;

select results_eq(
  $$select f.slug, c.attempts from public.claim_jobs(array['test_claim'], 10) c
    join fixture_job f on f.id = c.id$$,
  $$values ('future', 2)$$,
  'a job is claimed once its run_after has passed'
);

-- Both limits are parameters: with a 5 s timeout, "busy" and "busy-b" are stale, and with 2 attempts
-- allowed "busy" (1 used) is reclaimed while "busy-b" (2 used) is failed.
select results_eq(
  $$select f.slug, c.attempts from public.claim_jobs(array['test_claim'], 10, 5, 2) c
    join fixture_job f on f.id = c.id$$,
  $$values ('busy', 2)$$,
  'p_stale_after_seconds and p_max_attempts are honored when given'
);
select results_eq(
  $$select status, last_error from pg_temp.job('busy-b')$$,
  $$values ('failed', 'Timed out')$$,
  'a stale job that has used p_max_attempts is failed under a custom limit too'
);

-- complete_job and fail_job ---------------------------------------------------------------------

select lives_ok(
  $$select public.complete_job(pg_temp.job_id('complete-me'))$$,
  'the worker can complete a job'
);
select results_eq(
  $$select status, attempts, last_error, claimed_at is null from pg_temp.job('complete-me')$$,
  $$values ('done', 2, null::text, true)$$,
  'a completed job is done, and its earlier error and claim are cleared'
);
select lives_ok(
  'select public.complete_job(-1)',
  'completing a job that is gone (its image was deleted) is not an error'
);

select lives_ok(
  $$select public.fail_job(pg_temp.job_id('fail-first'), 'rate limited')$$,
  'the worker can fail a job'
);
select results_eq(
  $$select status, attempts, run_after, last_error, claimed_at is null from pg_temp.job('fail-first')$$,
  $$values ('pending', 1, now() + interval '10 seconds', 'rate limited', true)$$,
  'a retryable failure on the first attempt goes back to pending, due in 10 s'
);
select lives_ok(
  $$select public.fail_job(pg_temp.job_id('fail-second'), 'timeout', 3, true)$$,
  'the worker can fail a job on its second attempt'
);
select results_eq(
  $$select status, attempts, run_after from pg_temp.job('fail-second')$$,
  $$values ('pending', 2, now() + interval '20 seconds')$$,
  'the backoff doubles with each attempt used: 20 s after the second'
);
select lives_ok(
  $$select public.fail_job(pg_temp.job_id('fail-last'), 'timeout again', 3, true)$$,
  'the worker can fail a job on its last attempt'
);
select results_eq(
  $$select status, attempts, run_after, last_error, claimed_at is null from pg_temp.job('fail-last')$$,
  $$values ('failed', 3, now(), 'timeout again', true)$$,
  'a retryable failure with no attempts left is final, and keeps its run_after'
);
select lives_ok(
  $$select public.fail_job(pg_temp.job_id('fail-fatal'), 'unsupported', 3, false)$$,
  'the worker can fail a job for good'
);
select results_eq(
  $$select status, attempts, last_error from pg_temp.job('fail-fatal')$$,
  $$values ('failed', 1, 'unsupported')$$,
  'a non-retryable failure is final even with attempts left'
);
select lives_ok(
  $$select public.fail_job(pg_temp.job_id('fail-cap'), 'timeout', 2, true)$$,
  'the worker can fail a job under its own attempt limit'
);
select results_eq(
  $$select status, attempts from pg_temp.job('fail-cap')$$,
  $$values ('failed', 2)$$,
  'p_max_attempts is honored: 2 of 2 attempts used makes the failure final'
);
select lives_ok('select public.fail_job(-1, $$x$$)', 'failing a job that is gone is not an error');
select is_empty(
  $$select id from public.claim_jobs(array['test_claim'], 10)$$,
  'jobs waiting out their backoff are not claimable yet'
);

-- Enqueueing ------------------------------------------------------------------------------------

-- An upload as the API does it: the user inserts the image and its pending metadata through RLS.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000089a1", "role": "authenticated"}';

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256
)
values (
  '00000000-0000-4000-8000-0000000089a1', 'upload.jpg',
  '00000000-0000-4000-8000-0000000089a1/upload/original.jpg',
  '00000000-0000-4000-8000-0000000089a1/upload/thumb.webp',
  'image/jpeg', 1024, 640, 480, repeat('c', 64)
);
select lives_ok(
  $$insert into public.image_metadata (image_id, user_id, ai_processing_status)
    values (pg_temp.image_id('upload'), '00000000-0000-4000-8000-0000000089a1', 'pending')$$,
  'a user can still insert the pending metadata of a new upload'
);

reset role;

select results_eq(
  $$select kind, status, user_id, attempts, run_after = now(), claimed_at is null
    from pg_temp.analysis_job('upload')$$,
  $$values ('analyze_image', 'pending', '00000000-0000-4000-8000-0000000089a1'::uuid, 0, true, true)$$,
  'a new upload gets a pending analyze_image job, owned by the uploader'
);
select is(pg_temp.open_analysis_jobs('upload'), 1::bigint, 'a new upload gets exactly one job');

-- request_reanalysis, as the owner.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000089a1", "role": "authenticated"}';

select is(
  public.request_reanalysis(pg_temp.image_id('done-img')), true,
  'A can request reanalysis of A''s done image'
);
select is(
  public.request_reanalysis(pg_temp.image_id('failed-img')), true,
  'A can request reanalysis of A''s failed image'
);
select is(
  public.request_reanalysis(pg_temp.image_id('done-img')), true,
  'A can request it again while the job waits'
);
select is(
  public.request_reanalysis(pg_temp.image_id('busy-img')), false,
  'A can''t reset an image whose job is processing'
);
select is(
  public.request_reanalysis(pg_temp.image_id('old-busy')), false,
  'A can''t reset an image the old worker is processing, even though its job waits'
);
select is(
  public.request_reanalysis(current_setting('tests.b_img_id')::bigint), false,
  'A can''t request reanalysis of B''s image'
);
select is(public.request_reanalysis(-1), false, 'requesting reanalysis of a missing image changes nothing');

reset role;

select results_eq(
  $$select slug, pg_temp.open_analysis_jobs(slug), (j).status, (j).attempts, (j).run_after = now(),
           (m).ai_processing_status::text, (m).attempts, (m).last_error, (m).next_attempt_at
    from (
      select s as slug, pg_temp.analysis_job(s) as j, pg_temp.metadata(s) as m
      from unnest(array['done-img', 'failed-img']) as s
    ) as requeued
    order by slug$$,
  $$values
    ('done-img', 1::bigint, 'pending', 0, true, 'pending', 0, null::text, null::timestamptz),
    ('failed-img', 1::bigint, 'pending', 0, true, 'pending', 0, null::text, null::timestamptz)$$,
  'reanalysis queues exactly one fresh job, even when asked twice, and the image shows pending'
);
select results_eq(
  $$select slug, (m).ai_processing_status::text, (j).status
    from (
      select s as slug, pg_temp.metadata(s) as m, pg_temp.analysis_job(s) as j
      from unnest(array['busy-img', 'old-busy']) as s
    ) as unchanged
    order by slug$$,
  $$values ('busy-img', 'processing', 'processing'), ('old-busy', 'processing', 'pending')$$,
  'a refused reanalysis changes neither the image nor its job'
);
select is(pg_temp.open_analysis_jobs('b-img'), 0::bigint, 'B''s image got no job from A''s request');

-- The one open job per kind and image ----------------------------------------------------------

select throws_ok(
  $$insert into public.jobs (kind, image_id, user_id)
    values ('analyze_image', pg_temp.image_id('done-img'), '00000000-0000-4000-8000-0000000089a1')$$,
  '23505',
  'duplicate key value violates unique constraint "jobs_one_open_job_per_kind_and_image_idx"',
  'an image can''t have a second open job of the same kind'
);
select lives_ok(
  $$insert into public.jobs (kind, image_id, user_id, status)
    values ('analyze_image', pg_temp.image_id('done-img'), '00000000-0000-4000-8000-0000000089a1',
            'done')$$,
  'finished jobs don''t count against the limit'
);
select lives_ok(
  $$insert into public.jobs (kind, image_id, user_id)
    values ('re_embed', pg_temp.image_id('done-img'), '00000000-0000-4000-8000-0000000089a1')$$,
  'another kind of job for the same image is fine'
);
select lives_ok(
  $$insert into public.jobs (kind) values ('test_unbound'), ('test_unbound')$$,
  'jobs about no image are not limited'
);
delete from public.jobs
where kind in ('re_embed', 'test_unbound')
  or (status = 'done' and image_id = pg_temp.image_id('done-img'));

-- Analysis on top of the queue ------------------------------------------------------------------

-- In the window between the merge and the deploy, the old worker finishes an image through the old
-- RPC. Its job is left pending.
set local role service_role;

select lives_ok(
  $$select public.complete_analysis_job(
      pg_temp.image_id('leftover'), array['old'], 'Analyzed by the old worker.', array['#010203'],
      'old:v1')$$,
  'the old worker can still complete an analysis'
);
select results_eq(
  $$select (j).status, (m).ai_processing_status::text
    from (select pg_temp.analysis_job('leftover') as j, pg_temp.metadata('leftover') as m) as state$$,
  $$values ('pending', 'done')$$,
  'the old RPC leaves the job pending'
);

create temp table analysis_claim as select * from public.claim_jobs(array['analyze_image'], 10);

select results_eq(
  $$select i.filename::text from analysis_claim c join public.images i on i.id = c.image_id
    order by 1$$,
  array['claim-me.jpg', 'done-img.jpg', 'failed-img.jpg', 'leftover.jpg', 'old-busy.jpg', 'upload.jpg'],
  'a claim of analyze_image takes the pending jobs and leaves the live processing ones'
);
select results_eq(
  $$select c.user_id, c.attempts from analysis_claim c
    where c.image_id = pg_temp.image_id('claim-me')$$,
  $$values ('00000000-0000-4000-8000-0000000089a1'::uuid, 1)$$,
  'an analysis job carries the image''s owner'
);
select results_eq(
  $$select ai_processing_status::text, attempts, claimed_at = now(), next_attempt_at
    from pg_temp.metadata('claim-me')$$,
  $$values ('processing', 1, true, null::timestamptz)$$,
  'claiming an analysis job shows the image as processing, with the job''s attempt and claim'
);
select results_eq(
  $$select ai_processing_status::text, tags, analyzer_id from pg_temp.metadata('leftover')$$,
  $$values ('done', array['old'], 'old:v1')$$,
  'claiming a job whose analysis the old worker finished leaves the result alone'
);
select results_eq(
  $$select (j).status, (j).last_error, (m).ai_processing_status::text, (m).last_error
    from (select pg_temp.analysis_job('stale-analysis') as j,
                 pg_temp.metadata('stale-analysis') as m) as state$$,
  $$values ('failed', 'Timed out', 'failed', 'Timed out')$$,
  'an analysis that timed out on its last attempt fails, and the image shows it'
);

select results_eq(
  $$select image_id, original_path, content_sha256, ai_processing_status, last_error
    from public.analysis_target((select id from pg_temp.analysis_job('claim-me')))$$,
  $$select i.id, i.original_path, i.content_sha256::text, 'processing'::text, null::text
    from public.images i where i.filename = 'claim-me.jpg'$$,
  'analysis_target returns what the worker needs to download and analyze the image'
);
select results_eq(
  $$select ai_processing_status from public.analysis_target(
      (select id from pg_temp.analysis_job('leftover')))$$,
  array['done'],
  'analysis_target shows that the old worker already finished the leftover'
);
select is_empty(
  $$select image_id from public.analysis_target(pg_temp.job_id('due-old'))$$,
  'analysis_target finds nothing for a job of another kind'
);

-- The worker closes the leftover without analyzing it.
select lives_ok(
  $$select public.complete_job((select id from pg_temp.analysis_job('leftover')))$$,
  'the worker can close a job whose analysis already ended'
);
select results_eq(
  $$select (j).status, (m).ai_processing_status::text, (m).tags, (m).analyzer_id
    from (select pg_temp.analysis_job('leftover') as j, pg_temp.metadata('leftover') as m) as state$$,
  $$values ('done', 'done', array['old'], 'old:v1')$$,
  'closing the leftover keeps the old worker''s result'
);

select lives_ok(
  $$select public.complete_analysis(
      (select id from pg_temp.analysis_job('complete-me')),
      array['beach', 'sky'], 'A beach at sunset', array['#aabbcc', '#112233'], 'fake:v1')$$,
  'the worker can complete an analysis'
);
select results_eq(
  $$select (j).status, (j).last_error, (m).ai_processing_status::text, (m).tags, (m).description,
           (m).colors::text[], (m).analyzer_id, (m).last_error, (m).claimed_at
    from (select pg_temp.analysis_job('complete-me') as j,
                 pg_temp.metadata('complete-me') as m) as state$$,
  $$values ('done', null::text, 'done', array['beach', 'sky'], 'A beach at sunset',
            array['#aabbcc', '#112233'], 'fake:v1', null::text, null::timestamptz)$$,
  'completing an analysis stores the results on the image and completes its job, in one call'
);

select lives_ok(
  $$select public.fail_analysis(
      (select id from pg_temp.analysis_job('fail-retry')), 'rate limited', array['#112233'], true)$$,
  'the worker can fail an analysis'
);
select results_eq(
  $$select (j).status, (j).run_after, (m).ai_processing_status::text, (m).next_attempt_at,
           (m).last_error, (m).colors::text[], (m).claimed_at
    from (select pg_temp.analysis_job('fail-retry') as j,
                 pg_temp.metadata('fail-retry') as m) as state$$,
  $$values ('pending', now() + interval '10 seconds', 'pending', now() + interval '10 seconds',
            'rate limited', array['#112233'], null::timestamptz)$$,
  'a retryable failure requeues the job with its backoff, and the image shows the retry and colors'
);
select lives_ok(
  $$select public.fail_analysis(
      (select id from pg_temp.analysis_job('fail-final')), 'timeout', array['#223344'], true)$$,
  'the worker can fail an analysis on its last attempt'
);
select results_eq(
  $$select (j).status, (m).ai_processing_status::text, (m).next_attempt_at, (m).last_error,
           (m).colors::text[]
    from (select pg_temp.analysis_job('fail-final') as j,
                 pg_temp.metadata('fail-final') as m) as state$$,
  $$values ('failed', 'failed', null::timestamptz, 'timeout', array['#223344'])$$,
  'a final failure fails the job and the image, with the colors kept'
);

-- The user deletes an image while the worker analyzes it.
create temp table deleted_job as select id from pg_temp.analysis_job('deleted-img');
reset role;
delete from public.images where id = pg_temp.image_id('deleted-img');
set local role service_role;

select is_empty(
  $$select j.id from public.jobs j join deleted_job d on d.id = j.id$$,
  'deleting an image deletes its jobs'
);
select lives_ok(
  $$select public.complete_analysis((select id from deleted_job), array['x'], 'x', array['#000000'],
      'fake:v1')$$,
  'completing the analysis of a deleted image is not an error'
);
select lives_ok(
  $$select public.fail_analysis((select id from deleted_job), 'x', array['#000000'], true)$$,
  'failing the analysis of a deleted image is not an error'
);
select is_empty(
  $$select image_id from public.analysis_target((select id from deleted_job))$$,
  'analysis_target finds nothing for a deleted image'
);

reset role;

-- The backfill ----------------------------------------------------------------------------------

-- Images that stand for the ones production holds when the migration applies. The trigger queued
-- the pending ones, so drop those jobs to get back the state the backfill starts from.
select pg_temp.add_image('00000000-0000-4000-8000-0000000089a1', slug)
from unnest(array['bf-pending', 'bf-retry', 'bf-processing', 'bf-done', 'bf-failed']) as slug;

insert into public.image_metadata (
  image_id, user_id, ai_processing_status, attempts, next_attempt_at, created_at
)
select i.id, i.user_id, fixture.status, fixture.attempts, fixture.next_attempt_at, fixture.created_at
from (
  values
    ('bf-pending', 'pending', 0, null::timestamptz, now() - interval '1 day'),
    ('bf-retry', 'pending', 2, now() + interval '40 seconds', now() - interval '2 days'),
    ('bf-processing', 'processing', 3, null, now() - interval '3 days'),
    ('bf-done', 'done', 1, null, now() - interval '4 days'),
    ('bf-failed', 'failed', 3, null, now() - interval '5 days')
) as fixture (slug, status, attempts, next_attempt_at, created_at)
join public.images i on i.filename = fixture.slug || '.jpg';

delete from public.jobs j
using public.images i
where i.id = j.image_id and i.filename like 'bf-%';

-- The migration's own backfill statement, as the CLI recorded it when it applied the migration.
-- Looked up by name, which survives a renumbering, and run again here, so the test covers the
-- statement that ran, not a copy of it.
create function pg_temp.backfill_statements()
returns setof text
language sql
as $$
  select statement
  from supabase_migrations.schema_migrations m, unnest(m.statements) as statement
  where m.name = 'jobs'
    and statement ~* '^\s*(--[^\n]*\n\s*)*insert\s+into\s+public\.jobs\M'
$$;

select is(
  (select count(*) from pg_temp.backfill_statements()),
  1::bigint,
  'the migration has one backfill statement'
);
select lives_ok((select pg_temp.backfill_statements() limit 1), 'the backfill statement runs');
select results_eq(
  $$select i.filename::text, j.kind, j.status, j.attempts, j.run_after, j.created_at = m.created_at,
           j.user_id
    from public.jobs j
    join public.images i on i.id = j.image_id
    join public.image_metadata m on m.image_id = j.image_id
    where i.filename like 'bf-%'
    order by 1$$,
  $$values
    ('bf-pending.jpg', 'analyze_image', 'pending', 0, now(), true,
      '00000000-0000-4000-8000-0000000089a1'::uuid),
    ('bf-processing.jpg', 'analyze_image', 'pending', 0, now(), true,
      '00000000-0000-4000-8000-0000000089a1'::uuid),
    ('bf-retry.jpg', 'analyze_image', 'pending', 0, now() + interval '40 seconds', true,
      '00000000-0000-4000-8000-0000000089a1'::uuid)$$,
  'the backfill queues each unfinished analysis once, fresh, in its old order and backoff'
);
select lives_ok(
  (select pg_temp.backfill_statements() limit 1),
  'the backfill can run again'
);
select is(
  (select count(*) from public.jobs j join public.images i on i.id = j.image_id
    where i.filename like 'bf-%'),
  3::bigint,
  'running the backfill again queues nothing twice'
);

-- Users -----------------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000089a1", "role": "authenticated"}';

select throws_ok(
  'select * from public.jobs',
  '42501', 'permission denied for table jobs',
  'authenticated can''t read jobs'
);
select throws_ok(
  $$insert into public.jobs (kind, image_id) values ('analyze_image', pg_temp.image_id('done-img'))$$,
  '42501', 'permission denied for table jobs',
  'authenticated can''t insert jobs'
);
select throws_ok(
  $$update public.jobs set status = 'done'$$,
  '42501', 'permission denied for table jobs',
  'authenticated can''t update jobs'
);
select throws_ok(
  'delete from public.jobs',
  '42501', 'permission denied for table jobs',
  'authenticated can''t delete jobs'
);
select throws_ok(
  $$select * from public.claim_jobs(array['analyze_image'], 10)$$,
  '42501', 'permission denied for function claim_jobs',
  'authenticated can''t claim jobs'
);
select throws_ok(
  'select public.complete_job(1)',
  '42501', 'permission denied for function complete_job',
  'authenticated can''t complete jobs'
);
select throws_ok(
  $$select public.fail_job(1, 'x')$$,
  '42501', 'permission denied for function fail_job',
  'authenticated can''t fail jobs'
);
select throws_ok(
  $$select public.complete_analysis(1, array['x'], 'x', array['#000000'], 'fake:v1')$$,
  '42501', 'permission denied for function complete_analysis',
  'authenticated can''t complete analyses'
);

-- Second layer: even if the table grants were widened, there is no policy, so nothing matches.
reset role;
grant select, insert, update, delete on public.jobs to authenticated;
set local role authenticated;

select is_empty('select id from public.jobs', 'with SELECT granted, A still sees no jobs');
select throws_ok(
  $$insert into public.jobs (kind, image_id) values ('analyze_image', pg_temp.image_id('done-img'))$$,
  '42501', 'new row violates row-level security policy for table "jobs"',
  'with INSERT granted, A still can''t add a job'
);

reset role;
revoke select, insert, update, delete on public.jobs from authenticated;

set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  'select * from public.jobs',
  '42501', 'permission denied for table jobs',
  'anon can''t read jobs'
);
select throws_ok(
  $$select * from public.claim_jobs(array['analyze_image'], 10)$$,
  '42501', 'permission denied for function claim_jobs',
  'anon can''t claim jobs'
);

reset role;

select * from finish();

rollback;
