-- The per-user cap on objects in the images bucket (20261020000017_storage_quota.sql): the limit,
-- what counts, the exemptions, and who can change them. Run with `supabase test db`. Everything
-- happens in one transaction that rolls back.
--
-- pgTAP can't drive the Storage HTTP API, so the tests insert storage.objects rows directly, as
-- authenticated users, the way storage-api does for a signed-in caller.
--
-- User A = 00000000-0000-4000-8000-0000000103a1 is the user who hits the limit.
-- User B = 00000000-0000-4000-8000-0000000103b1 shows another user is unaffected.
-- User C = 00000000-0000-4000-8000-0000000103c1 sends batches.
-- User D = 00000000-0000-4000-8000-0000000103d1 checks the default limit.
-- User E = 00000000-0000-4000-8000-0000000103e1 is exempt.
-- Most tests lower the limit to 3, so a handful of rows reach it.
-- Policy violations and missing grants share SQLSTATE 42501, so every throws_ok on them also pins
-- the message to prove which layer stopped the statement.

begin;

select plan(50);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-0000000103a1', 'storage-quota-a@example.test'),
  ('00000000-0000-4000-8000-0000000103b1', 'storage-quota-b@example.test'),
  ('00000000-0000-4000-8000-0000000103c1', 'storage-quota-c@example.test'),
  ('00000000-0000-4000-8000-0000000103d1', 'storage-quota-d@example.test'),
  ('00000000-0000-4000-8000-0000000103e1', 'storage-quota-e@example.test');

insert into storage.buckets (id, name) values ('other-bucket', 'other-bucket');

-- Inserts `how_many` objects into `folder_owner`'s folder of the images bucket in ONE statement,
-- as the active role. `slug` keeps the keys of separate calls apart. (Not `owner`: that is a column
-- of storage.objects, and would shadow the parameter.)
create function pg_temp.put_many(folder_owner uuid, how_many int, slug text)
returns void
language sql
as $$
  insert into storage.objects (bucket_id, name)
  select 'images', format('%s/%s-%s/original.jpg', folder_owner, slug, n)
  from generate_series(1, how_many) as n
$$;

-- The objects in the user's folder of the images bucket, whoever is asking.
create function pg_temp.objects_of(folder_owner uuid)
returns bigint
language sql
security definer
as $$
  select count(*) from storage.objects
  where bucket_id = 'images' and (storage.foldername(name))[1] = folder_owner::text
$$;

-- Acts as `who`, as PostgREST and storage-api do for a signed-in user.
create function pg_temp.sign_in(who uuid)
returns void
language sql
as $$
  select set_config('role', 'authenticated', true),
         set_config('request.jwt.claims', json_build_object('sub', who, 'role', 'authenticated')::text, true)
$$;

-- Structure -------------------------------------------------------------------------------------

select has_column('public', 'quota_settings', 'max_storage_objects', 'quota_settings has the object limit');
select col_type_is('public', 'quota_settings', 'max_storage_objects', 'integer', 'the limit is an integer');
select col_not_null('public', 'quota_settings', 'max_storage_objects', 'the limit is not null');
select col_default_is('public', 'quota_settings', 'max_storage_objects', '2000', 'the limit defaults to 2000');
select is(
  (select array_agg(max_storage_objects) from public.quota_settings),
  array[2000],
  'the existing settings row gets the limit of 2000'
);
select throws_ok(
  $$update public.quota_settings set max_storage_objects = -1$$,
  '23514', null,
  'a negative limit is refused'
);

select has_trigger('storage', 'objects', 'enforce_storage_quota', 'the quota trigger exists');
-- pg_get_triggerdef leaves out the function's schema when it is on the search_path, so the
-- function is checked on its own.
select matches(
  (select pg_get_triggerdef(t.oid) from pg_trigger t
   where t.tgrelid = 'storage.objects'::regclass and t.tgname = 'enforce_storage_quota'),
  '^CREATE TRIGGER enforce_storage_quota BEFORE INSERT ON storage\.objects FOR EACH ROW '
    'WHEN \(\(new\.bucket_id = ''images''::text\)\) EXECUTE FUNCTION ',
  'the trigger fires before each inserted row of the images bucket'
);
select is(
  (select t.tgfoid::regprocedure from pg_trigger t
   where t.tgrelid = 'storage.objects'::regclass and t.tgname = 'enforce_storage_quota'),
  'public.enforce_storage_quota()'::regprocedure,
  'and runs enforce_storage_quota'
);
select is_definer('public', 'enforce_storage_quota', array[]::text[], 'enforce_storage_quota is security definer');
select is(
  (select proconfig from pg_proc where oid = 'public.enforce_storage_quota()'::regprocedure),
  array['search_path=""'],
  'enforce_storage_quota pins an empty search_path'
);
select results_eq(
  $$select r.role, has_function_privilege(r.role, 'public.enforce_storage_quota()', 'execute')
    from (values ('anon'), ('authenticated'), ('service_role')) as r(role) order by 1$$,
  $$values ('anon', false), ('authenticated', false), ('service_role', false)$$,
  'no API role can call enforce_storage_quota directly'
);

-- Keys that only look like A's don't count against A ----------------------------------------------

-- The first folder is A's id only for keys under "A/". These are at the root, or in folders whose
-- names start with A's id, and sort on either side of A's folder.
insert into storage.objects (bucket_id, name)
values
  ('images', '00000000-0000-4000-8000-0000000103a1.jpg'),
  ('images', '00000000-0000-4000-8000-0000000103a1-copy/original.jpg'),
  ('images', '00000000-0000-4000-8000-0000000103a10/original.jpg'),
  ('images', '00000000-0000-4000-8000-0000000103a1_copy/original.jpg');

-- A's objects in other buckets aren't in the images bucket, and no limit applies there.
update public.quota_settings set max_storage_objects = 3;
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    select 'other-bucket', format('00000000-0000-4000-8000-0000000103a1/other-%s.jpg', n)
    from generate_series(1, 5) as n$$,
  'other buckets take any number of objects'
);

-- The limit -----------------------------------------------------------------------------------------

select pg_temp.sign_in('00000000-0000-4000-8000-0000000103a1');

-- The limit is "at most 3", so all three go in.
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103a1', 2, 'a')$$,
  'A uploads two objects'
);
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103a1/a-3/original.jpg')$$,
  'and a third, which reaches the limit; look-alike keys and other buckets didn''t count'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103a1/a-4/original.jpg')$$,
  'PT429',
  'You''ve reached your storage limit of 3 files. Delete some images to make room.',
  'the fourth is refused with PostgREST''s 429 code and the API''s wording'
);
select is(pg_temp.objects_of('00000000-0000-4000-8000-0000000103a1'), 3::bigint, 'A still has 3 objects');

-- RLS runs before the trigger can matter: A uploading into B's folder gets the policy's error.
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103b1/planted/original.jpg')$$,
  '42501', 'new row violates row-level security policy for table "objects"',
  'A can''t upload into B''s folder'
);

reset role;

-- Another user is unaffected --------------------------------------------------------------------------

select pg_temp.sign_in('00000000-0000-4000-8000-0000000103b1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103b1', 3, 'b')$$,
  'B uploads three objects while A is at the limit'
);
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103b1', 1, 'b-4')$$,
  'PT429', null,
  'and is then at the limit too'
);
reset role;

-- A statement is counted whole ------------------------------------------------------------------------

select pg_temp.sign_in('00000000-0000-4000-8000-0000000103c1');
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103c1', 4, 'c-bulk')$$,
  'PT429', null,
  'one statement of 4 objects is refused: each row sees the ones before it'
);
select is(pg_temp.objects_of('00000000-0000-4000-8000-0000000103c1'), 0::bigint, 'none of the 4 was stored');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103c1', 3, 'c')$$,
  'a statement of exactly 3 goes in'
);
reset role;

-- The owner is the folder, whoever inserts ------------------------------------------------------------

-- service_role bypasses RLS, but an object it puts in A's folder is still A's.
set local role service_role;
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103a1', 1, 'a-service')$$,
  'PT429', null,
  'service_role can''t put a fourth object into A''s folder either'
);
reset role;

-- A row with an existing key replaces that object rather than adding one, so it isn't refused.
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103a1/a-3/original.jpg')
    on conflict do nothing$$,
  'a row with a key A already holds is not counted as a new object'
);
select is(pg_temp.objects_of('00000000-0000-4000-8000-0000000103a1'), 3::bigint, 'A still has 3 objects');

-- Deleting frees a slot -------------------------------------------------------------------------------

-- Unlike the upload quota, this one is about capacity, so a deleted object gives its slot back.
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103a1');
-- Storage blocks direct deletes unless this is set; the Storage API sets it for its own deletes.
set local storage.allow_delete_query = 'true';

select results_eq(
  $$delete from storage.objects
    where bucket_id = 'images' and name = '00000000-0000-4000-8000-0000000103a1/a-1/original.jpg'
    returning name$$,
  array['00000000-0000-4000-8000-0000000103a1/a-1/original.jpg'],
  'A deletes an object'
);
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103a1/a-again/original.jpg')$$,
  'which lets A upload one more'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-0000000103a1/a-again-2/original.jpg')$$,
  'PT429', null,
  'and only one'
);

-- Users can't read or change the setting ---------------------------------------------------------------

select throws_ok(
  $$update public.quota_settings set max_storage_objects = 0$$,
  '42501', 'permission denied for table quota_settings', 'A can''t turn the quota off'
);
select throws_ok(
  $$update public.quota_settings set max_storage_objects = 1000000$$,
  '42501', 'permission denied for table quota_settings', 'A can''t raise the limit'
);
select throws_ok(
  $$insert into public.quota_settings (singleton, max_storage_objects) values (true, 0)
    on conflict (singleton) do update set max_storage_objects = 0$$,
  '42501', 'permission denied for table quota_settings', 'A can''t upsert the limit'
);
select throws_ok(
  $$delete from public.quota_settings$$,
  '42501', 'permission denied for table quota_settings', 'A can''t delete the settings'
);
select throws_ok(
  $$insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000103a1')$$,
  '42501', 'permission denied for table quota_exempt_users', 'A can''t exempt themselves'
);
select throws_ok(
  $$select public.enforce_storage_quota()$$,
  '42501', 'permission denied for function enforce_storage_quota', 'A can''t call the trigger function'
);
reset role;

set local role anon;
select throws_ok(
  $$update public.quota_settings set max_storage_objects = 0$$,
  '42501', 'permission denied for table quota_settings', 'anon can''t change the limit'
);
reset role;

set local role service_role;
select throws_ok(
  $$update public.quota_settings set max_storage_objects = 0$$,
  '42501', 'permission denied for table quota_settings', 'service_role can''t change the limit'
);
select throws_ok(
  $$insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000103a1')$$,
  '42501', 'permission denied for table quota_exempt_users', 'service_role can''t exempt anyone'
);
reset role;

select is(
  (select array_agg(max_storage_objects) from public.quota_settings),
  array[3],
  'the limit is unchanged'
);

-- Exempt users -------------------------------------------------------------------------------------------

insert into public.quota_exempt_users (user_id) values ('00000000-0000-4000-8000-0000000103e1');

select pg_temp.sign_in('00000000-0000-4000-8000-0000000103e1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103e1', 5, 'e')$$,
  'an exempt user can upload 5 objects past a limit of 3'
);
select is(pg_temp.objects_of('00000000-0000-4000-8000-0000000103e1'), 5::bigint, 'and holds all 5');
reset role;

-- Changing the limit -------------------------------------------------------------------------------------

update public.quota_settings set max_storage_objects = 4;
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103a1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103a1', 1, 'a-raised')$$,
  'raising the limit to 4 lets A''s fourth object in'
);
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103a1', 1, 'a-5')$$,
  'PT429',
  'You''ve reached your storage limit of 4 files. Delete some images to make room.',
  'and the fifth is refused, with the new limit in the message'
);
reset role;

update public.quota_settings set max_storage_objects = 1;
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103d1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103d1', 1, 'd')$$,
  'a limit of 1 lets D upload one object'
);
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103d1', 1, 'd-2')$$,
  'PT429',
  'You''ve reached your storage limit of 1 file. Delete some images to make room.',
  'and refuses the second, in the singular'
);
reset role;

update public.quota_settings set max_storage_objects = 0;
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103a1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103a1', 5, 'a-off')$$,
  'a limit of 0 turns the quota off, even for a user past the old limit'
);
select is(pg_temp.objects_of('00000000-0000-4000-8000-0000000103a1'), 9::bigint, 'A now holds 9 objects');
reset role;

-- The default ------------------------------------------------------------------------------------------

update public.quota_settings set max_storage_objects = default;
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103d1');
select lives_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103d1', 1999, 'd-default')$$,
  'at the default, D can hold 2000 objects'
);
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103d1', 1, 'd-2001')$$,
  'PT429',
  'You''ve reached your storage limit of 2000 files. Delete some images to make room.',
  'and not 2001'
);
reset role;

-- The settings row can't go missing and switch the quota off: the default of 2000 applies.
delete from public.quota_settings;
select pg_temp.sign_in('00000000-0000-4000-8000-0000000103d1');
select throws_ok(
  $$select pg_temp.put_many('00000000-0000-4000-8000-0000000103d1', 1, 'd-no-row')$$,
  'PT429',
  'You''ve reached your storage limit of 2000 files. Delete some images to make room.',
  'with no settings row, the quota falls back to 2000'
);
reset role;

select * from finish();
rollback;
