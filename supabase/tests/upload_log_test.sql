-- The upload log behind the daily quota (20261020000012_upload_log.sql): the insert trigger, the
-- backfill, RLS, and bumps staying free. Run with `supabase test db`. Everything happens in one
-- transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000084a1 uploads through RLS, as the API does.
-- User B = 00000000-0000-4000-8000-0000000084b1 has two images, inserted as postgres.
-- User C = 00000000-0000-4000-8000-0000000084c1 has two older images that stand for the ones
-- production holds when the migration applies.
-- now() is frozen inside a transaction, so every row the trigger logs here has uploaded_at = now().
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok also pins the
-- message to prove which layer stopped the statement.

begin;

select plan(39);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000084a1', 'upload-log-a@example.test'),
  ('00000000-0000-4000-8000-0000000084b1', 'upload-log-b@example.test'),
  ('00000000-0000-4000-8000-0000000084c1', 'upload-log-c@example.test');

-- Inserts one image as whichever role is active, so under A's role it goes through RLS.
create function pg_temp.upload(owner uuid, slug text, age interval default interval '0')
returns bigint
language sql
as $$
  insert into public.images (
    user_id, filename, original_path, thumbnail_path,
    mime_type, size_bytes, width, height, content_sha256, uploaded_at
  )
  values (
    owner, slug || '.jpg',
    format('%s/%s/original.jpg', owner, slug), format('%s/%s/thumb.webp', owner, slug),
    'image/jpeg', 1024, 640, 480, repeat('a', 64), now() - age
  )
  returning id
$$;

create function pg_temp.image_id(slug text)
returns bigint
language sql
as $$ select id from public.images where filename = slug || '.jpg' $$;

-- The owner's log rows that the active role can see.
create function pg_temp.logged(owner uuid)
returns bigint
language sql
as $$ select count(*) from public.upload_log where user_id = owner $$;

-- B's two images come in one statement, to show the trigger logs each row of it.
insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256
)
select
  '00000000-0000-4000-8000-0000000084b1', slug || '.jpg',
  format('00000000-0000-4000-8000-0000000084b1/%s/original.jpg', slug),
  format('00000000-0000-4000-8000-0000000084b1/%s/thumb.webp', slug),
  'image/jpeg', 1024, 640, 480, repeat('b', 64)
from unnest(array['b-1', 'b-2']) as slug;

-- Structure -------------------------------------------------------------------------------------

select has_table('public', 'upload_log', 'upload_log exists');
select columns_are(
  'public', 'upload_log', array['id', 'user_id', 'uploaded_at'], 'upload_log has exactly its columns'
);
select col_type_is('public', 'upload_log', 'user_id', 'uuid', 'user_id is a uuid');
select col_not_null('public', 'upload_log', 'user_id', 'user_id is not null');
select col_type_is(
  'public', 'upload_log', 'uploaded_at', 'timestamp with time zone', 'uploaded_at is a timestamptz'
);
select col_not_null('public', 'upload_log', 'uploaded_at', 'uploaded_at is not null');
select col_default_is('public', 'upload_log', 'uploaded_at', 'now()', 'uploaded_at defaults to now()');
select has_index(
  'public', 'upload_log', 'upload_log_user_id_uploaded_at_idx', array['user_id', 'uploaded_at'],
  'the quota count has an index on (user_id, uploaded_at)'
);
select is(
  (select relrowsecurity from pg_class where oid = 'public.upload_log'::regclass),
  true,
  'upload_log has RLS enabled'
);
select policies_are(
  'public', 'upload_log', array['upload_log: owner can select'],
  'upload_log has a select policy and nothing else'
);
select policy_roles_are(
  'public', 'upload_log', 'upload_log: owner can select', array['authenticated'],
  'the select policy applies to authenticated'
);
select policy_cmd_is(
  'public', 'upload_log', 'upload_log: owner can select', 'select',
  'the only policy is for select'
);
select table_privs_are(
  'public', 'upload_log', 'authenticated', array['SELECT'], 'authenticated may only read upload_log'
);
select table_privs_are(
  'public', 'upload_log', 'anon', array[]::text[], 'anon has no rights on upload_log'
);
select table_privs_are(
  'public', 'upload_log', 'service_role', array['SELECT'], 'service_role may only read upload_log'
);
select results_eq(
  $$select r.role, has_sequence_privilege(r.role, 'public.upload_log_id_seq', 'usage, select, update')
    from (values ('anon'), ('authenticated'), ('service_role')) as r(role) order by 1$$,
  $$values ('anon', false), ('authenticated', false), ('service_role', false)$$,
  'no API role can use the upload_log id sequence'
);
select trigger_is(
  'public', 'images', 'images_log_upload', 'public', 'log_upload',
  'images_log_upload runs log_upload'
);
select results_eq(
  -- information_schema's columns are collated "C", and the expected rows are not.
  $$select
      action_timing::text collate "default",
      event_manipulation::text collate "default",
      action_orientation::text collate "default"
    from information_schema.triggers
    where event_object_schema = 'public' and trigger_name = 'images_log_upload'$$,
  $$values ('AFTER', 'INSERT', 'ROW')$$,
  'the trigger fires after each inserted row, and on nothing else'
);
select is_definer('public', 'log_upload', array[]::text[], 'log_upload is security definer');
select is(
  (select proconfig from pg_proc where oid = 'public.log_upload()'::regprocedure),
  array['search_path=""'],
  'log_upload pins an empty search_path'
);
select results_eq(
  $$select r.role, has_function_privilege(r.role, 'public.log_upload()', 'execute')
    from (values ('anon'), ('authenticated'), ('service_role')) as r(role) order by 1$$,
  $$values ('anon', false), ('authenticated', false), ('service_role', false)$$,
  'no API role can call log_upload directly'
);

-- The trigger -------------------------------------------------------------------------------------

select is(
  pg_temp.logged('00000000-0000-4000-8000-0000000084b1'),
  2::bigint,
  'a two-row insert logs two uploads'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000084a1", "role": "authenticated"}';

-- A has no insert grant on upload_log, so the row can only come from the security definer trigger.
select pg_temp.upload('00000000-0000-4000-8000-0000000084a1', 'a-1');
select results_eq(
  $$select user_id, uploaded_at from public.upload_log$$,
  $$values ('00000000-0000-4000-8000-0000000084a1'::uuid, now())$$,
  'an upload through RLS is logged once, for its owner, at the time of the insert'
);

delete from public.images where filename = 'a-1.jpg';
select is(pg_temp.image_id('a-1'), null, 'A deleted the image');
select is(
  pg_temp.logged('00000000-0000-4000-8000-0000000084a1'),
  1::bigint,
  'deleting the image keeps its log row, so its slot stays used'
);

-- RLS: each user sees only their own rows ---------------------------------------------------------

select is(
  pg_temp.logged('00000000-0000-4000-8000-0000000084b1'),
  0::bigint,
  'A sees none of B''s uploads'
);
select results_eq(
  $$select distinct user_id from public.upload_log$$,
  $$values ('00000000-0000-4000-8000-0000000084a1'::uuid)$$,
  'every row A can read is A''s own'
);

-- No writes for users -----------------------------------------------------------------------------

select throws_ok(
  $$insert into public.upload_log (user_id) values ('00000000-0000-4000-8000-0000000084a1')$$,
  '42501', 'permission denied for table upload_log',
  'A can''t log an upload by hand'
);
select throws_ok(
  $$update public.upload_log set uploaded_at = now() - interval '2 days'$$,
  '42501', 'permission denied for table upload_log',
  'A can''t move an upload out of the window'
);
select throws_ok(
  $$delete from public.upload_log$$,
  '42501', 'permission denied for table upload_log',
  'A can''t delete log rows'
);
-- TRUNCATE ignores RLS, so only the missing grant stops it.
select throws_ok(
  $$truncate public.upload_log$$,
  '42501', 'permission denied for table upload_log',
  'A can''t truncate the log'
);

-- Bumps stay free ---------------------------------------------------------------------------------

select pg_temp.upload('00000000-0000-4000-8000-0000000084a1', 'a-2');
select is(public.bump_image(pg_temp.image_id('a-2')), true, 'bump_image moves a-2 to the top');
select is(
  array(select public.bump_images(array[pg_temp.image_id('a-2')])),
  array[pg_temp.image_id('a-2')],
  'bump_images moves it again'
);
select is(
  pg_temp.logged('00000000-0000-4000-8000-0000000084a1'),
  2::bigint,
  'the bumps logged nothing: A has the deleted a-1 and the new a-2'
);

reset role;

set local role anon;
select throws_ok(
  $$select count(*) from public.upload_log$$,
  '42501', 'permission denied for table upload_log',
  'anon can''t read the log'
);
reset role;

-- The backfill ------------------------------------------------------------------------------------

-- C's images stand for the ones that exist when the migration applies: the trigger logged them, so
-- drop those rows to get back the state the backfill starts from.
select pg_temp.upload('00000000-0000-4000-8000-0000000084c1', 'c-1', interval '3 days');
select pg_temp.upload('00000000-0000-4000-8000-0000000084c1', 'c-2', interval '2 hours');
delete from public.upload_log where user_id = '00000000-0000-4000-8000-0000000084c1';

-- The migration's own backfill statement, as the CLI recorded it when it applied the migration.
-- Looked up by name, which survives a renumbering, and run again here, so the test covers the
-- statement that ran, not a copy of it.
create function pg_temp.backfill_statements()
returns setof text
language sql
as $$
  select statement
  from supabase_migrations.schema_migrations m, unnest(m.statements) as statement
  where m.name = 'upload_log'
    and statement ~* '^\s*(--[^\n]*\n\s*)*insert\s+into\s+public\.upload_log\M'
$$;

select is(
  (select count(*) from pg_temp.backfill_statements()),
  1::bigint,
  'the migration has one backfill statement'
);
select lives_ok(
  (select pg_temp.backfill_statements() limit 1),
  'the backfill statement runs'
);
select results_eq(
  $$select uploaded_at from public.upload_log
    where user_id = '00000000-0000-4000-8000-0000000084c1' order by uploaded_at$$,
  $$values (now() - interval '3 days'), (now() - interval '2 hours')$$,
  'the backfill logs each existing image once, at its own upload time'
);

-- Deleting the user -------------------------------------------------------------------------------

delete from auth.users where id = '00000000-0000-4000-8000-0000000084b1';
select is(
  pg_temp.logged('00000000-0000-4000-8000-0000000084b1'),
  0::bigint,
  'deleting a user removes their log rows'
);

select * from finish();
rollback;
