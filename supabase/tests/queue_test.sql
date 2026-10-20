-- Analysis queue RPCs, request_reanalysis and the ai_cache table.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000004a1 owns every fixture image except b-done.
-- User B = 00000000-0000-4000-8000-0000000004b1 owns b-done.
-- now() is frozen inside a transaction, so fixtures are placed relative to it and every claim in
-- this file sees the same clock. The behavior sections run as service_role, the worker's role.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.

begin;

select plan(63);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

-- A claim takes every claimable row in the table, so start from an empty one even on a dev
-- database with real uploads. The rollback at the end puts them back.
delete from public.images;

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000004a1', 'queue-a@example.test'),
  ('00000000-0000-4000-8000-0000000004b1', 'queue-b@example.test');

-- The filename is the fixture's handle: image_metadata rows are looked up through it, because
-- ids are generated.
insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256
)
select
  owner.user_id,
  fixture.slug || '.jpg',
  format('%s/%s/original.jpg', owner.user_id, fixture.slug),
  format('%s/%s/thumb.webp', owner.user_id, fixture.slug),
  'image/jpeg', 1024, 640, 480, md5(fixture.slug) || md5(fixture.slug)
from (
  values
    ('future'), ('stale-exhausted'), ('stale-retry'), ('due-old'), ('due-retry'), ('busy'),
    ('busy-b'), ('exhausted-pending'), ('done-row'), ('failed-row'), ('reanalyze-busy'),
    ('complete-me'), ('fail-first'), ('fail-second'), ('fail-last'), ('fail-fatal'), ('fail-cap')
) as fixture (slug)
cross join (select '00000000-0000-4000-8000-0000000004a1'::uuid as user_id) as owner
union all
select
  '00000000-0000-4000-8000-0000000004b1'::uuid,
  'b-done.jpg',
  '00000000-0000-4000-8000-0000000004b1/b-done/original.jpg',
  '00000000-0000-4000-8000-0000000004b1/b-done/thumb.webp',
  'image/jpeg', 1024, 640, 480, md5('b-done') || md5('b-done');

-- created_at fixes the claim order (oldest first) among the rows that are claimable.
insert into public.image_metadata (
  image_id, user_id, ai_processing_status, attempts, next_attempt_at, claimed_at, last_error,
  created_at
)
select
  i.id, i.user_id, fixture.status, fixture.attempts, fixture.next_attempt_at, fixture.claimed_at,
  fixture.last_error, fixture.created_at
from (
  values
    -- Oldest row, but not due for another minute: a claim must skip it.
    ('future', 'pending', 1, now() + interval '1 minute', null::timestamptz, null::text,
      now() - interval '9 minutes'),
    -- Worker died long ago with no attempts left: the claim must fail it, not retry it.
    ('stale-exhausted', 'processing', 3, null, now() - interval '10 minutes', 'rate limited',
      now() - interval '8 minutes'),
    -- Worker died long ago with attempts left: the claim must take it over.
    ('stale-retry', 'processing', 1, null, now() - interval '10 minutes', null,
      now() - interval '7 minutes'),
    ('due-old', 'pending', 0, null, null, null, now() - interval '6 minutes'),
    -- A retry whose backoff has passed.
    ('due-retry', 'pending', 1, now() - interval '1 minute', null, null,
      now() - interval '5 minutes'),
    -- Claimed 10 s ago: alive under the default 300 s timeout, stale under a 5 s one.
    ('busy', 'processing', 1, null, now() - interval '10 seconds', null,
      now() - interval '4 minutes'),
    ('busy-b', 'processing', 2, null, now() - interval '10 seconds', null,
      now() - interval '3 minutes'),
    -- Pending but out of attempts, which only inconsistent callers can produce: never claimed.
    ('exhausted-pending', 'pending', 3, null, null, null, now() - interval '2 minutes'),
    ('done-row', 'done', 1, null, null, null, now()),
    -- Leftover timing fields that a reanalysis must clear.
    ('failed-row', 'failed', 3, now() + interval '1 hour', now() - interval '1 hour', 'boom',
      now()),
    ('reanalyze-busy', 'processing', 1, null, now(), null, now()),
    ('complete-me', 'processing', 2, null, now(), 'earlier failure', now()),
    ('fail-first', 'processing', 1, null, now(), null, now()),
    ('fail-second', 'processing', 2, null, now(), null, now()),
    ('fail-last', 'processing', 3, null, now(), null, now()),
    ('fail-fatal', 'processing', 1, null, now(), null, now()),
    ('fail-cap', 'processing', 2, null, now(), null, now()),
    ('b-done', 'done', 1, null, null, null, now())
) as fixture (slug, status, attempts, next_attempt_at, claimed_at, last_error, created_at)
join public.images i on i.filename = fixture.slug || '.jpg';

-- Look fixtures up by name. Both read through the caller's privileges, so under a user role they
-- see only that user's rows.
create function pg_temp.image_id(slug text)
returns bigint
language sql
as $$ select id from public.images where filename = slug || '.jpg' $$;

create function pg_temp.job(slug text)
returns setof public.image_metadata
language sql
as $$
  select m.*
  from public.image_metadata m
  join public.images i on i.id = m.image_id
  where i.filename = slug || '.jpg'
$$;

-- A can't read B's rows, so remember B's id for the statements that run as A.
select set_config('tests.b_done_id', pg_temp.image_id('b-done')::text, true);

-- Structure -------------------------------------------------------------------------------------

select has_table('public', 'ai_cache', 'ai_cache exists');
select columns_are(
  'public', 'ai_cache',
  array['content_sha256', 'analyzer_id', 'result', 'created_at'],
  'ai_cache has the cache key, the result and a timestamp'
);
select col_type_is('public', 'ai_cache', 'content_sha256', 'character(64)', 'the key is a sha256 hex digest');
select col_is_pk(
  'public', 'ai_cache', array['content_sha256', 'analyzer_id'],
  'a cached result is keyed by image content and analyzer'
);
select col_not_null('public', 'ai_cache', 'result', 'a cache row always has a result');
select ok(
  (select relrowsecurity from pg_class where oid = 'public.ai_cache'::regclass),
  'RLS is enabled on ai_cache'
);
select policies_are('public', 'ai_cache', array[]::text[], 'ai_cache has no policies');
select table_privs_are('public', 'ai_cache', 'anon', array[]::text[], 'anon has no privileges on ai_cache');
select table_privs_are(
  'public', 'ai_cache', 'authenticated', array[]::text[],
  'authenticated has no privileges on ai_cache'
);
select table_privs_are(
  'public', 'ai_cache', 'service_role', array['SELECT', 'INSERT', 'UPDATE'],
  'service_role can read and upsert ai_cache, but not delete or truncate'
);

-- Security definer, and an empty search_path so a caller can't shadow a table or operator.
-- Catalog names have the C collation; results_eq needs a collation both sides agree on.
select results_eq(
  $$select proname::text collate "default", prosecdef, 'search_path=""' = any (proconfig)
    from pg_proc
    where pronamespace = 'public'::regnamespace
      and proname in ('claim_analysis_jobs', 'complete_analysis_job', 'fail_analysis_job',
                      'request_reanalysis')
    order by proname$$,
  $$values
    ('claim_analysis_jobs', true, true),
    ('complete_analysis_job', true, true),
    ('fail_analysis_job', true, true),
    ('request_reanalysis', true, true)$$,
  'the four RPCs are security definer with an empty search_path'
);

-- PUBLIC's default EXECUTE counts here: has_function_privilege covers every way a role can hold it.
select results_eq(
  $$select p.proname::text collate "default", r.rolname::text collate "default",
           has_function_privilege(r.oid, p.oid, 'execute')
    from pg_proc p
    cross join pg_roles r
    where p.pronamespace = 'public'::regnamespace
      and p.proname in ('claim_analysis_jobs', 'complete_analysis_job', 'fail_analysis_job',
                        'request_reanalysis')
      and r.rolname in ('anon', 'authenticated', 'service_role')
    order by p.proname, r.rolname$$,
  $$values
    ('claim_analysis_jobs', 'anon', false),
    ('claim_analysis_jobs', 'authenticated', false),
    ('claim_analysis_jobs', 'service_role', true),
    ('complete_analysis_job', 'anon', false),
    ('complete_analysis_job', 'authenticated', false),
    ('complete_analysis_job', 'service_role', true),
    ('fail_analysis_job', 'anon', false),
    ('fail_analysis_job', 'authenticated', false),
    ('fail_analysis_job', 'service_role', true),
    ('request_reanalysis', 'anon', false),
    ('request_reanalysis', 'authenticated', true),
    ('request_reanalysis', 'service_role', false)$$,
  'only service_role runs the job RPCs, and only authenticated runs request_reanalysis'
);

-- claim_analysis_jobs ---------------------------------------------------------------------------

set local role service_role;

-- Two claims in a row inside one transaction: the second must not see the first one's rows.
-- (The row locks that keep two concurrent workers apart are checked by hand, see the PR.)
create temp table claim_1 as select * from public.claim_analysis_jobs(2);

select results_eq(
  $$select i.filename::text from claim_1 c join public.images i on i.id = c.image_id order by 1$$,
  array['due-old.jpg', 'stale-retry.jpg'],
  'a claim returns the oldest claimable jobs, up to max_jobs: stale processing and due pending'
);
select results_eq(
  $$select image_id, user_id, original_path, content_sha256, attempts
    from claim_1 where image_id = pg_temp.image_id('due-old')$$,
  $$select i.id, i.user_id, i.original_path, i.content_sha256::text, 1
    from public.images i where i.filename = 'due-old.jpg'$$,
  'a claim returns what the worker needs to download and analyze the image'
);
select results_eq(
  $$select slug, (j).ai_processing_status::text, (j).attempts, (j).claimed_at = now()
    from (
      select s as slug, pg_temp.job(s) as j from unnest(array['due-old', 'stale-retry']) as s
    ) as claimed
    order by slug$$,
  $$values
    ('due-old', 'processing', 1, true),
    ('stale-retry', 'processing', 2, true)$$,
  'claimed jobs are processing, with one more attempt used and a fresh claim time'
);
select results_eq(
  $$select ai_processing_status::text, attempts, last_error, claimed_at is null
    from pg_temp.job('stale-exhausted')$$,
  $$values ('failed', 3, 'analysis timed out', true)$$,
  'a stale job with no attempts left is failed with "analysis timed out", not claimed'
);

create temp table claim_2 as select * from public.claim_analysis_jobs(10);

select results_eq(
  $$select i.filename::text from claim_2 c join public.images i on i.id = c.image_id order by 1$$,
  array['due-retry.jpg'],
  'a second claim gets only what the first left behind: a retry whose backoff has passed'
);
select is_empty(
  'select image_id from claim_1 intersect select image_id from claim_2',
  'two claims in sequence never return the same job'
);
select is_empty(
  'select image_id from public.claim_analysis_jobs(10)',
  'a third claim finds nothing: the rest is future-dated, still alive, finished or out of attempts'
);
select results_eq(
  $$select slug, (j).ai_processing_status::text, (j).attempts
    from (
      select s as slug, pg_temp.job(s) as j
      from unnest(array['future', 'busy', 'busy-b', 'exhausted-pending', 'done-row', 'failed-row'])
        as s
    ) as untouched
    order by slug$$,
  $$values
    ('busy', 'processing', 1),
    ('busy-b', 'processing', 2),
    ('done-row', 'done', 1),
    ('exhausted-pending', 'pending', 3),
    ('failed-row', 'failed', 3),
    ('future', 'pending', 1)$$,
  'the claims left every other job as it was'
);

-- The clock moving past next_attempt_at is what makes a backed-off job claimable.
select lives_ok(
  $$update public.image_metadata set next_attempt_at = now() - interval '1 second'
    where image_id = pg_temp.image_id('future')$$,
  'a future-dated job can be made due'
);
select results_eq(
  $$select i.filename::text, c.attempts
    from public.claim_analysis_jobs(10) c join public.images i on i.id = c.image_id$$,
  $$values ('future.jpg', 2)$$,
  'a job is claimed once its next_attempt_at has passed'
);

-- Both limits are parameters: with a 5 s timeout, "busy" and "busy-b" are stale, and with 2 attempts
-- allowed "busy" (1 used) is reclaimed while "busy-b" (2 used) is failed.
select results_eq(
  $$select i.filename::text, c.attempts
    from public.claim_analysis_jobs(10, 5, 2) c join public.images i on i.id = c.image_id$$,
  $$values ('busy.jpg', 2)$$,
  'stale_after_seconds and max_attempts are honored when given'
);
select results_eq(
  $$select ai_processing_status::text, last_error from pg_temp.job('busy-b')$$,
  $$values ('failed', 'analysis timed out')$$,
  'a stale job that has used max_attempts is failed under a custom max_attempts too'
);

-- complete_analysis_job -------------------------------------------------------------------------

select lives_ok(
  $$select public.complete_analysis_job(
      pg_temp.image_id('complete-me'),
      array['beach', 'sky'], 'A beach at sunset', array['#aabbcc', '#112233'], 'fake:v1')$$,
  'the worker can complete a job'
);
select results_eq(
  $$select ai_processing_status::text, tags, description, colors::text[], analyzer_id,
           last_error, claimed_at is null, attempts
    from pg_temp.job('complete-me')$$,
  $$values ('done', array['beach', 'sky'], 'A beach at sunset', array['#aabbcc', '#112233'],
            'fake:v1', null::text, true, 2)$$,
  'a completed job is done with its results, and the earlier error and claim are cleared'
);
select lives_ok(
  $$select public.complete_analysis_job(-1, array['x'], 'x', array['#000000'], 'fake:v1')$$,
  'completing a job whose image was deleted meanwhile is not an error'
);

-- fail_analysis_job -----------------------------------------------------------------------------

select lives_ok(
  $$select public.fail_analysis_job(
      pg_temp.image_id('fail-first'), 'rate limited', array['#112233'], true)$$,
  'the worker can fail a job'
);
select results_eq(
  $$select ai_processing_status::text, attempts, next_attempt_at, last_error, colors::text[],
           claimed_at is null
    from pg_temp.job('fail-first')$$,
  $$values ('pending', 1, now() + interval '10 seconds', 'rate limited', array['#112233'], true)$$,
  'a retryable failure on the first attempt goes back to pending, due in 10 s, with colors kept'
);

select lives_ok(
  $$select public.fail_analysis_job(
      pg_temp.image_id('fail-second'), 'timeout', array['#223344'], true)$$,
  'the worker can fail a job on its second attempt'
);
select results_eq(
  $$select ai_processing_status::text, attempts, next_attempt_at, last_error, colors::text[]
    from pg_temp.job('fail-second')$$,
  $$values ('pending', 2, now() + interval '20 seconds', 'timeout', array['#223344'])$$,
  'the backoff doubles with each attempt used: 20 s after the second'
);

select lives_ok(
  $$select public.fail_analysis_job(
      pg_temp.image_id('fail-last'), 'timeout again', array['#334455'], true)$$,
  'the worker can fail a job on its last attempt'
);
select results_eq(
  $$select ai_processing_status::text, attempts, next_attempt_at, last_error, colors::text[],
           claimed_at is null
    from pg_temp.job('fail-last')$$,
  $$values ('failed', 3, null::timestamptz, 'timeout again', array['#334455'], true)$$,
  'a retryable failure with no attempts left is final, with colors kept'
);

select lives_ok(
  $$select public.fail_analysis_job(
      pg_temp.image_id('fail-fatal'), 'unsupported image', array['#445566'], false)$$,
  'the worker can fail a job for good'
);
select results_eq(
  $$select ai_processing_status::text, attempts, next_attempt_at, last_error, colors::text[]
    from pg_temp.job('fail-fatal')$$,
  $$values ('failed', 1, null::timestamptz, 'unsupported image', array['#445566'])$$,
  'a non-retryable failure is final even with attempts left, with colors kept'
);

select lives_ok(
  $$select public.fail_analysis_job(
      pg_temp.image_id('fail-cap'), 'timeout', array['#556677'], true, 2)$$,
  'the worker can fail a job under its own attempt limit'
);
select results_eq(
  $$select ai_processing_status::text, attempts, last_error from pg_temp.job('fail-cap')$$,
  $$values ('failed', 2, 'timeout')$$,
  'p_max_attempts is honored: 2 of 2 attempts used makes the failure final'
);
select lives_ok(
  $$select public.fail_analysis_job(-1, 'x', array['#000000'], true)$$,
  'failing a job whose image was deleted meanwhile is not an error'
);

select is_empty(
  'select image_id from public.claim_analysis_jobs(10)',
  'jobs waiting out their backoff are not claimable yet'
);

-- ai_cache as the worker ------------------------------------------------------------------------

select lives_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('a', 64), 'fake:v1', '{"tags": ["beach"], "description": "first"}')$$,
  'the worker can write a cache row'
);
select lives_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('a', 64), 'fake:v1', '{"tags": ["beach"], "description": "second"}')
    on conflict (content_sha256, analyzer_id) do update set result = excluded.result$$,
  'the worker can upsert a cache row'
);
select results_eq(
  $$select result ->> 'description' from public.ai_cache
    where content_sha256 = repeat('a', 64) and analyzer_id = 'fake:v1'$$,
  array['second'],
  'the upsert replaced the cached result'
);
select throws_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('a', 64), 'fake:v1', '{}')$$,
  '23505', 'duplicate key value violates unique constraint "ai_cache_pkey"',
  'one analyzer caches one result per image content'
);
select lives_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('a', 64), 'other:v1', '{"tags": [], "description": "other"}')$$,
  'another analyzer caches its own result for the same content'
);

-- Job RPCs and ai_cache as users ----------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000004a1", "role": "authenticated"}';

select throws_ok(
  'select * from public.claim_analysis_jobs(10)',
  '42501', 'permission denied for function claim_analysis_jobs',
  'authenticated can''t claim jobs'
);
select throws_ok(
  $$select public.complete_analysis_job(
      pg_temp.image_id('done-row'), array['x'], 'x', array['#000000'], 'fake:v1')$$,
  '42501', 'permission denied for function complete_analysis_job',
  'authenticated can''t complete jobs'
);
select throws_ok(
  $$select public.fail_analysis_job(pg_temp.image_id('done-row'), 'x', array['#000000'], false)$$,
  '42501', 'permission denied for function fail_analysis_job',
  'authenticated can''t fail jobs'
);
select throws_ok(
  'select * from public.ai_cache',
  '42501', 'permission denied for table ai_cache',
  'authenticated can''t read ai_cache'
);
select throws_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('b', 64), 'fake:v1', '{}')$$,
  '42501', 'permission denied for table ai_cache',
  'authenticated can''t write ai_cache'
);

-- Second layer: even if the table grants were widened, there is no policy, so nothing matches.
reset role;
grant select, insert on public.ai_cache to authenticated;
set local role authenticated;

select is_empty(
  'select content_sha256 from public.ai_cache',
  'with SELECT granted, A still sees no ai_cache rows'
);
select throws_ok(
  $$insert into public.ai_cache (content_sha256, analyzer_id, result)
    values (repeat('b', 64), 'fake:v1', '{}')$$,
  '42501', 'new row violates row-level security policy for table "ai_cache"',
  'with INSERT granted, A still can''t write ai_cache'
);

reset role;
revoke select, insert on public.ai_cache from authenticated;
set local role authenticated;

-- request_reanalysis ----------------------------------------------------------------------------

select is(
  (select public.request_reanalysis(pg_temp.image_id('done-row'))),
  true,
  'A can request reanalysis of A''s done image'
);
select is(
  (select public.request_reanalysis(pg_temp.image_id('failed-row'))),
  true,
  'A can request reanalysis of A''s failed image'
);
select is(
  (select public.request_reanalysis(pg_temp.image_id('reanalyze-busy'))),
  false,
  'A can''t reset an image that is being processed'
);
select is(
  (select public.request_reanalysis(current_setting('tests.b_done_id')::bigint)),
  false,
  'A can''t request reanalysis of B''s image'
);
select is(
  (select public.request_reanalysis(-1)),
  false,
  'requesting reanalysis of a missing image changes nothing'
);

reset role;

select results_eq(
  $$select slug, (j).ai_processing_status::text, (j).attempts, (j).next_attempt_at,
           (j).last_error, (j).claimed_at
    from (
      select s as slug, pg_temp.job(s) as j from unnest(array['done-row', 'failed-row']) as s
    ) as requeued
    order by slug$$,
  $$values
    ('done-row', 'pending', 0, null::timestamptz, null::text, null::timestamptz),
    ('failed-row', 'pending', 0, null::timestamptz, null::text, null::timestamptz)$$,
  'a requeued image is pending with a fresh attempt budget and no leftover error or timing'
);
select results_eq(
  $$select slug, (j).ai_processing_status::text, (j).attempts
    from (
      select s as slug, pg_temp.job(s) as j from unnest(array['reanalyze-busy', 'b-done']) as s
    ) as unchanged
    order by slug$$,
  $$values
    ('b-done', 'done', 1),
    ('reanalyze-busy', 'processing', 1)$$,
  'a busy image and another user''s image were left alone'
);

set local role service_role;

select results_eq(
  $$select i.filename::text, c.attempts
    from public.claim_analysis_jobs(10) c join public.images i on i.id = c.image_id
    order by 1$$,
  $$values ('done-row.jpg', 1), ('failed-row.jpg', 1)$$,
  'requeued images are claimable at once'
);
select throws_ok(
  $$select public.request_reanalysis(pg_temp.image_id('done-row'))$$,
  '42501', 'permission denied for function request_reanalysis',
  'service_role can''t call request_reanalysis'
);

-- anon ------------------------------------------------------------------------------------------

set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select throws_ok(
  $$select public.request_reanalysis(pg_temp.image_id('done-row'))$$,
  '42501', 'permission denied for function request_reanalysis',
  'anon can''t call request_reanalysis'
);
select throws_ok(
  'select * from public.claim_analysis_jobs(10)',
  '42501', 'permission denied for function claim_analysis_jobs',
  'anon can''t claim jobs'
);
select throws_ok(
  'select * from public.ai_cache',
  '42501', 'permission denied for table ai_cache',
  'anon can''t read ai_cache'
);

reset role;

select * from finish();

rollback;
