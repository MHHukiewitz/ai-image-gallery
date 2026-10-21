-- The daily upload quota in the database (20261020000015_upload_quota.sql): the limit, the
-- exemptions, what counts, and who can change them. Run with `supabase test db`. Everything
-- happens in one transaction that rolls back.
--
-- User A = 00000000-0000-4000-8000-0000000095a1 is the user who hits the limit.
-- User B = 00000000-0000-4000-8000-0000000095b1 shows another user is unaffected, and batches.
-- User C = 00000000-0000-4000-8000-0000000095c1 sits at the limit while A probes it, then ages.
-- User D = 00000000-0000-4000-8000-0000000095d1 checks the limit setting.
-- User E = 00000000-0000-4000-8000-0000000095e1 is exempt.
-- now() is frozen inside a transaction, so every log row the trigger writes here has
-- uploaded_at = now(), and tests age rows by moving them back from it.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok on them also pins
-- the message to prove which layer stopped the statement.

begin;

select plan(79);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000095a1', 'upload-quota-a@example.test'),
  ('00000000-0000-4000-8000-0000000095b1', 'upload-quota-b@example.test'),
  ('00000000-0000-4000-8000-0000000095c1', 'upload-quota-c@example.test'),
  ('00000000-0000-4000-8000-0000000095d1', 'upload-quota-d@example.test'),
  ('00000000-0000-4000-8000-0000000095e1', 'upload-quota-e@example.test');

-- Inserts `how_many` images for `owner` in ONE statement, like a bulk insert through PostgREST.
-- `slug` keeps the storage paths of separate calls apart.
create function pg_temp.upload_many(owner uuid, how_many int, slug text)
returns void
language sql
as $$
  insert into public.images (
    user_id, filename, original_path, thumbnail_path,
    mime_type, size_bytes, width, height, content_sha256
  )
  select
    owner, format('%s-%s.jpg', slug, n),
    format('%s/%s-%s/original.jpg', owner, slug, n), format('%s/%s-%s/thumb.webp', owner, slug, n),
    'image/jpeg', 1024, 640, 480, repeat('a', 64)
  from generate_series(1, how_many) as n
$$;

-- The owner's images and log rows, as seen by the active role.
create function pg_temp.images_of(owner uuid)
returns bigint
language sql
as $$ select count(*) from public.images where user_id = owner $$;

create function pg_temp.logged(owner uuid)
returns bigint
language sql
as $$ select count(*) from public.upload_log where user_id = owner $$;

-- The ids of the owner's oldest `how_many` images, in id order.
create function pg_temp.some_image_ids(owner uuid, how_many int)
returns bigint[]
language sql
as $$
  select array_agg(id order by id)
  from (select id from public.images where user_id = owner order by id limit how_many) as oldest
$$;

-- Advisory locks this transaction holds on a user's quota (see enforce_upload_quota).
create function pg_temp.quota_locks(owner uuid)
returns bigint
language sql
as $$
  select count(*)
  from pg_locks l
  where l.locktype = 'advisory'
    and l.pid = pg_backend_pid()
    and l.classid::oid::bigint = (hashtext('upload_quota')::bigint & 4294967295)
    and l.objid::oid::bigint = (hashtext(owner::text)::bigint & 4294967295)
$$;

-- Structure -------------------------------------------------------------------------------------

select has_table('public', 'quota_settings', 'quota_settings exists');
select columns_are(
  -- max_storage_objects is the storage quota's (20261020000017_storage_quota.sql).
  'public', 'quota_settings', array['singleton', 'daily_upload_limit', 'max_storage_objects'],
  'quota_settings has exactly its columns'
);
select col_is_pk('public', 'quota_settings', 'singleton', 'singleton is the primary key');
select col_type_is('public', 'quota_settings', 'daily_upload_limit', 'integer', 'the limit is an integer');
select col_not_null('public', 'quota_settings', 'daily_upload_limit', 'the limit is not null');
select col_default_is('public', 'quota_settings', 'daily_upload_limit', '300', 'the limit defaults to 300');
select is(
  (select array_agg(daily_upload_limit) from public.quota_settings),
  array[300],
  'the migration seeds one row, with the limit at 300'
);
select throws_ok(
  $$insert into public.quota_settings (singleton) values (true)$$,
  '23505', null,
  'a second settings row is refused'
);
select throws_ok(
  $$insert into public.quota_settings (singleton) values (false)$$,
  '23514', null,
  'a settings row can only have singleton = true'
);
select throws_ok(
  $$update public.quota_settings set daily_upload_limit = -1$$,
  '23514', null,
  'a negative limit is refused'
);

select has_table('public', 'quota_exempt_users', 'quota_exempt_users exists');
select columns_are(
  'public', 'quota_exempt_users', array['user_id'], 'quota_exempt_users has exactly its columns'
);
select col_is_pk('public', 'quota_exempt_users', 'user_id', 'user_id is the primary key');
select col_type_is('public', 'quota_exempt_users', 'user_id', 'uuid', 'user_id is a uuid');
select fk_ok(
  'public', 'quota_exempt_users', 'user_id', 'auth', 'users', 'id',
  'an exempt user is a real user'
);

select results_eq(
  $$select c.relname::text collate "default", c.relrowsecurity
    from pg_class c
    where c.oid in ('public.quota_settings'::regclass, 'public.quota_exempt_users'::regclass)
    order by 1$$,
  $$values ('quota_exempt_users', true), ('quota_settings', true)$$,
  'both tables have RLS enabled'
);
select policies_are('public', 'quota_settings', array[]::name[], 'quota_settings has no policies');
select policies_are(
  'public', 'quota_exempt_users', array[]::name[], 'quota_exempt_users has no policies'
);
select results_eq(
  $$select t.tbl, r.role, has_table_privilege(r.role, t.tbl, 'select, insert, update, delete, truncate, references, trigger')
    from (values ('public.quota_settings'), ('public.quota_exempt_users')) as t(tbl)
    cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role)
    order by 1, 2$$,
  $$values
    ('public.quota_exempt_users', 'anon', false),
    ('public.quota_exempt_users', 'authenticated', false),
    ('public.quota_exempt_users', 'service_role', false),
    ('public.quota_settings', 'anon', false),
    ('public.quota_settings', 'authenticated', false),
    ('public.quota_settings', 'service_role', false)$$,
  'no API role has any privilege on either table'
);

select has_trigger('public', 'images', 'images_enforce_upload_quota', 'the quota trigger exists');
select results_eq(
  -- information_schema's columns are collated "C", and the expected rows are not.
  $$select
      action_timing::text collate "default",
      event_manipulation::text collate "default",
      action_orientation::text collate "default",
      action_reference_new_table::text collate "default"
    from information_schema.triggers
    where event_object_schema = 'public' and trigger_name = 'images_enforce_upload_quota'$$,
  $$values ('AFTER', 'INSERT', 'STATEMENT', 'new_rows')$$,
  'the trigger fires once after each insert statement, and sees all its rows'
);
select is_definer('public', 'enforce_upload_quota', array[]::text[], 'enforce_upload_quota is security definer');
select is(
  (select proconfig from pg_proc where oid = 'public.enforce_upload_quota()'::regprocedure),
  array['search_path=""'],
  'enforce_upload_quota pins an empty search_path'
);
select results_eq(
  $$select r.role, has_function_privilege(r.role, 'public.enforce_upload_quota()', 'execute')
    from (values ('anon'), ('authenticated'), ('service_role')) as r(role) order by 1$$,
  $$values ('anon', false), ('authenticated', false), ('service_role', false)$$,
  'no API role can call enforce_upload_quota directly'
);

-- The 301st insert is refused ---------------------------------------------------------------------

-- 300 in one statement fills A's quota. The limit is "at most 300", so all of them go in.
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 300, 'a')$$,
  'A can insert 300 images in 24 hours'
);
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095a1'), 300::bigint, 'A has 300 uploads logged');
select is(
  pg_temp.quota_locks('00000000-0000-4000-8000-0000000095a1'),
  1::bigint,
  'the insert queued on A''s quota lock, so concurrent inserts can''t both see a free slot'
);

select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 1, 'a301')$$,
  'PT429',
  'You''ve reached today''s upload limit of 300 images. Try again tomorrow.',
  'the 301st insert is refused with PostgREST''s 429 code and the API''s wording'
);
select is(pg_temp.images_of('00000000-0000-4000-8000-0000000095a1'), 300::bigint, 'the refused image was not stored');
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095a1'), 300::bigint, 'and was not logged');

-- The same, as PostgREST runs it for a signed-in user: through RLS, as `authenticated`.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000095a1", "role": "authenticated"}';

select throws_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256
    )
    values (
      '00000000-0000-4000-8000-0000000095a1', 'direct.jpg',
      '00000000-0000-4000-8000-0000000095a1/direct/original.jpg',
      '00000000-0000-4000-8000-0000000095a1/direct/thumb.webp',
      'image/jpeg', 1024, 640, 480, repeat('a', 64)
    )$$,
  'PT429',
  'You''ve reached today''s upload limit of 300 images. Try again tomorrow.',
  'a signed-in client inserting into images directly is refused too'
);
select is(
  pg_temp.images_of('00000000-0000-4000-8000-0000000095a1'),
  300::bigint,
  'A still has 300 images'
);

-- Bumps stay free ---------------------------------------------------------------------------------

select is(
  public.bump_image((pg_temp.some_image_ids('00000000-0000-4000-8000-0000000095a1', 1))[1]),
  true,
  'bump_image still works at the limit'
);
select is(
  array(select public.bump_images(pg_temp.some_image_ids('00000000-0000-4000-8000-0000000095a1', 3))),
  pg_temp.some_image_ids('00000000-0000-4000-8000-0000000095a1', 3),
  'bump_images still works at the limit'
);
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095a1'), 300::bigint, 'the bumps logged nothing');

-- Deleting an image does not free its slot --------------------------------------------------------

delete from public.images
where id = (select min(id) from public.images where user_id = '00000000-0000-4000-8000-0000000095a1');
select is(pg_temp.images_of('00000000-0000-4000-8000-0000000095a1'), 299::bigint, 'A deleted an image');
select throws_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256
    )
    values (
      '00000000-0000-4000-8000-0000000095a1', 'after-delete.jpg',
      '00000000-0000-4000-8000-0000000095a1/after-delete/original.jpg',
      '00000000-0000-4000-8000-0000000095a1/after-delete/thumb.webp',
      'image/jpeg', 1024, 640, 480, repeat('a', 64)
    )$$,
  'PT429', null,
  'the freed image gives back no slot: upload, delete, upload again is still limited'
);

reset role;

-- Another user is unaffected ----------------------------------------------------------------------

select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095b1', 299, 'b')$$,
  'B inserts 299 images while A is at the limit'
);
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095b1'), 299::bigint, 'B has 299 uploads logged');

-- A batch is judged whole ----------------------------------------------------------------------------

select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095b1', 2, 'b-over')$$,
  'PT429', null,
  'a batch that would take B past 300 is refused'
);
select is(
  pg_temp.images_of('00000000-0000-4000-8000-0000000095b1'),
  299::bigint,
  'none of the batch was stored, not even the row that still fit'
);
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095b1'), 299::bigint, 'and none of it was logged');
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095b1', 1, 'b-last')$$,
  'the batch that takes B to exactly 300 goes in'
);

-- A single statement of 301 rows is where a per-row check fails: no row sees the earlier ones.
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095c1', 301, 'c-bulk')$$,
  'PT429', null,
  'one bulk insert of 301 images is refused'
);
select is(pg_temp.images_of('00000000-0000-4000-8000-0000000095c1'), 0::bigint, 'none of the 301 was stored');

-- One user over the limit refuses the statement for everyone in it.
select throws_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256
    )
    select u, 'mix.jpg', format('%s/mix/original.jpg', u), format('%s/mix/thumb.webp', u),
      'image/jpeg', 1024, 640, 480, repeat('a', 64)
    from unnest(array[
      '00000000-0000-4000-8000-0000000095c1'::uuid, '00000000-0000-4000-8000-0000000095b1'
    ]) as u$$,
  'PT429', null,
  'a statement with a row for B, who is at the limit, is refused'
);
select is(
  pg_temp.images_of('00000000-0000-4000-8000-0000000095c1'),
  0::bigint,
  'and C''s row in it was not stored either'
);

-- Exempt users --------------------------------------------------------------------------------------

insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000095e1');

select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095e1', 305, 'e')$$,
  'an exempt user can insert 305 images in 24 hours'
);
select is(pg_temp.logged('00000000-0000-4000-8000-0000000095e1'), 305::bigint, 'their uploads are still logged');
select is(
  pg_temp.quota_locks('00000000-0000-4000-8000-0000000095e1'),
  0::bigint,
  'and they never queue on a quota lock'
);
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095e1', 1, 'e-more')$$,
  'they can keep going'
);

delete from auth.users where id = '00000000-0000-4000-8000-0000000095e1';
select is(
  (select count(*) from public.quota_exempt_users where user_id = '00000000-0000-4000-8000-0000000095e1'),
  0::bigint,
  'deleting the user removes the exemption'
);

-- A user can't probe another user's quota ---------------------------------------------------------

-- B is at 300. A has no right to insert a row for B, and must get the policy's error, never the
-- quota's: the quota's would say whether B is at their limit.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-0000000095a1", "role": "authenticated"}';

select throws_ok(
  $$insert into public.images (
      user_id, filename, original_path, thumbnail_path,
      mime_type, size_bytes, width, height, content_sha256
    )
    values (
      '00000000-0000-4000-8000-0000000095b1', 'probe.jpg',
      '00000000-0000-4000-8000-0000000095b1/probe/original.jpg',
      '00000000-0000-4000-8000-0000000095b1/probe/thumb.webp',
      'image/jpeg', 1024, 640, 480, repeat('a', 64)
    )$$,
  '42501', 'new row violates row-level security policy for table "images"',
  'A inserting a row for B, who is at the limit, gets the RLS error and learns nothing about B'
);

-- Users can't read or change the settings ------------------------------------------------------------

select throws_ok(
  $$select * from public.quota_settings$$,
  '42501', 'permission denied for table quota_settings', 'A can''t read the limit'
);
select throws_ok(
  $$update public.quota_settings set daily_upload_limit = 0$$,
  '42501', 'permission denied for table quota_settings', 'A can''t turn the quota off'
);
select throws_ok(
  $$update public.quota_settings set daily_upload_limit = 1000000$$,
  '42501', 'permission denied for table quota_settings', 'A can''t raise the limit'
);
select throws_ok(
  $$insert into public.quota_settings (singleton, daily_upload_limit) values (true, 0)
    on conflict (singleton) do update set daily_upload_limit = 0$$,
  '42501', 'permission denied for table quota_settings', 'A can''t upsert the limit'
);
select throws_ok(
  $$delete from public.quota_settings$$,
  '42501', 'permission denied for table quota_settings', 'A can''t delete the settings'
);
select throws_ok(
  $$truncate public.quota_settings$$,
  '42501', 'permission denied for table quota_settings', 'A can''t truncate the settings'
);
select throws_ok(
  $$select * from public.quota_exempt_users$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t see who is exempt'
);
select throws_ok(
  $$insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000095a1')$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t exempt themselves'
);
select throws_ok(
  $$update public.quota_exempt_users set user_id = '00000000-0000-4000-8000-0000000095a1'$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t edit the exemptions'
);
select throws_ok(
  $$delete from public.quota_exempt_users$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t remove an exemption'
);
select throws_ok(
  $$truncate public.quota_exempt_users$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t truncate the exemptions'
);
select throws_ok(
  $$select public.enforce_upload_quota()$$,
  '42501', 'permission denied for function enforce_upload_quota', 'A can''t call the trigger function'
);

reset role;

set local role anon;
select throws_ok(
  $$select * from public.quota_settings$$,
  '42501', 'permission denied for table quota_settings', 'anon can''t read the limit'
);
select throws_ok(
  $$insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000095a1')$$,
  '42501', 'permission denied for table quota_exempt_users', 'anon can''t exempt anyone'
);
reset role;

-- The worker's key is no way around it either.
set local role service_role;
select throws_ok(
  $$update public.quota_settings set daily_upload_limit = 0$$,
  '42501', 'permission denied for table quota_settings', 'service_role can''t change the limit'
);
select throws_ok(
  $$insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000095a1')$$,
  '42501', 'permission denied for table quota_exempt_users', 'service_role can''t exempt anyone'
);
reset role;

-- The 24-hour window --------------------------------------------------------------------------------

select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095c1', 300, 'c')$$,
  'C inserts 300 images'
);
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095c1', 1, 'c-301')$$,
  'PT429', null,
  'C is at the limit'
);

-- The API counts uploaded_at >= now() - 24 hours, so a row exactly 24 hours old still counts.
update public.upload_log set uploaded_at = now() - interval '24 hours'
where user_id = '00000000-0000-4000-8000-0000000095c1';
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095c1', 1, 'c-edge')$$,
  'PT429', null,
  'uploads exactly 24 hours old still count'
);

update public.upload_log set uploaded_at = now() - interval '24 hours 1 second'
where user_id = '00000000-0000-4000-8000-0000000095c1';
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095c1', 1, 'c-free')$$,
  'a slot is free again once the uploads are older than 24 hours'
);

-- Changing the limit ----------------------------------------------------------------------------------

-- A holds 300 uploads. One more slot lets one more image in, and the message names the new limit.
update public.quota_settings set daily_upload_limit = 301;
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 1, 'a-raised')$$,
  'raising the limit to 301 lets A''s 301st image in'
);
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 1, 'a-302')$$,
  'PT429',
  'You''ve reached today''s upload limit of 301 images. Try again tomorrow.',
  'and the 302nd is refused, with the new limit in the message'
);

update public.quota_settings set daily_upload_limit = 1;
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095d1', 1, 'd')$$,
  'a limit of 1 lets D insert one image'
);
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095d1', 1, 'd-2')$$,
  'PT429',
  'You''ve reached today''s upload limit of 1 image. Try again tomorrow.',
  'and refuses the second, in the singular'
);

update public.quota_settings set daily_upload_limit = 0;
select lives_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 5, 'a-off')$$,
  'a limit of 0 turns the quota off, even for a user who is far past the old one'
);

-- The settings row can't go missing and switch the quota off: the default of 300 applies.
delete from public.quota_settings;
select throws_ok(
  $$select pg_temp.upload_many('00000000-0000-4000-8000-0000000095a1', 1, 'a-no-row')$$,
  'PT429',
  'You''ve reached today''s upload limit of 300 images. Try again tomorrow.',
  'with no settings row, the quota falls back to 300'
);

select * from finish();
rollback;
