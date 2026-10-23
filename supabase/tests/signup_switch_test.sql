-- The sign-up switch: admins, site_settings, their functions, and the trigger that refuses new
-- auth.users rows while sign-ups are off.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- Admin A = 00000000-0000-4000-8000-000000000201 is in public.admins.
-- User U  = 00000000-0000-4000-8000-000000000202 is not.

begin;

select plan(38);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-000000000201', 'switch-admin@example.test'),
  ('00000000-0000-4000-8000-000000000202', 'switch-user@example.test');

insert into public.admins (user_id) values ('00000000-0000-4000-8000-000000000201');

-- Structure -------------------------------------------------------------------------------------

select ok(
  (select relrowsecurity from pg_class where oid = 'public.admins'::regclass),
  'RLS is enabled on admins'
);
select ok(
  (select relrowsecurity from pg_class where oid = 'public.site_settings'::regclass),
  'RLS is enabled on site_settings'
);
select is(
  (select count(*)::int from pg_policies
   where schemaname = 'public' and tablename in ('admins', 'site_settings')),
  0,
  'admins and site_settings have no policies, so RLS lets nobody in directly'
);
select table_privs_are('public', 'admins', 'anon', array[]::text[], 'anon has no privileges on admins');
select table_privs_are(
  'public', 'admins', 'authenticated', array[]::text[], 'authenticated has no privileges on admins'
);
select table_privs_are(
  'public', 'site_settings', 'anon', array[]::text[], 'anon has no privileges on site_settings'
);
select table_privs_are(
  'public', 'site_settings', 'authenticated', array[]::text[],
  'authenticated has no privileges on site_settings'
);
select ok(
  has_function_privilege('anon', 'public.signups_allowed()', 'execute'),
  'anon can ask whether sign-ups are open, for the sign-up page'
);
select ok(
  not has_function_privilege('anon', 'public.is_admin()', 'execute'),
  'anon can''t call is_admin'
);
select ok(
  not has_function_privilege('anon', 'public.get_site_settings()', 'execute'),
  'anon can''t read the settings'
);
select ok(
  not has_function_privilege('anon', 'public.set_allow_signups(boolean)', 'execute'),
  'anon can''t change the settings'
);
select ok(
  not has_function_privilege('authenticated', 'public.refuse_closed_signups()', 'execute'),
  'nobody calls the trigger function directly'
);
select trigger_is(
  'auth', 'users', 'refuse_closed_signups', 'public', 'refuse_closed_signups',
  'auth.users runs refuse_closed_signups'
);
select throws_ok(
  $$insert into public.site_settings (id) values (false)$$,
  '23514', null,
  'site_settings can''t get a second row with another id'
);
select throws_ok(
  $$insert into public.site_settings (id) values (true)$$,
  '23505', null,
  'site_settings can''t get a second row with the same id'
);
select ok(public.signups_allowed(), 'sign-ups start allowed, so the migration changes nothing');

-- A user who isn't an admin ---------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000202", "role": "authenticated"}';

select ok(not public.is_admin(), 'U is not an admin');
select ok(public.signups_allowed(), 'U can see that sign-ups are open');
select throws_ok(
  $$select * from public.get_site_settings()$$,
  'PT403', 'Only an admin can see the site settings.',
  'U can''t read the settings'
);
select throws_ok(
  $$select * from public.set_allow_signups(false)$$,
  'PT403', 'Only an admin can change the site settings.',
  'U can''t turn sign-ups off'
);
select throws_ok(
  $$select * from public.site_settings$$,
  '42501', 'permission denied for table site_settings',
  'U can''t read site_settings directly'
);
select throws_ok(
  $$update public.site_settings set allow_signups = false$$,
  '42501', 'permission denied for table site_settings',
  'U can''t update site_settings directly'
);
select throws_ok(
  $$insert into public.admins (user_id) values ('00000000-0000-4000-8000-000000000202')$$,
  '42501', 'permission denied for table admins',
  'U can''t make U an admin'
);

-- Anyone who isn't signed in --------------------------------------------------------------------

reset role;
set local role anon;
set local request.jwt.claims = '{"role": "anon"}';

select ok(public.signups_allowed(), 'anon can see that sign-ups are open');
select throws_ok(
  $$select * from public.set_allow_signups(false)$$,
  '42501', 'permission denied for function set_allow_signups',
  'anon can''t turn sign-ups off'
);

-- The admin -------------------------------------------------------------------------------------

reset role;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000201", "role": "authenticated"}';

select ok(public.is_admin(), 'A is an admin');
select results_eq(
  $$select allow_signups from public.get_site_settings()$$,
  $$values (true)$$,
  'A reads the settings'
);
select throws_ok(
  $$select * from public.set_allow_signups(null)$$,
  '22004', 'p_allow must be true or false.',
  'A can''t set the switch to null'
);
select results_eq(
  $$select allow_signups from public.set_allow_signups(false)$$,
  $$values (false)$$,
  'A turns sign-ups off, and gets the new settings back'
);
select ok(not public.signups_allowed(), 'sign-ups are now closed');

reset role;

select is(
  (select updated_by from public.site_settings),
  '00000000-0000-4000-8000-000000000201'::uuid,
  'the settings remember who changed them'
);

-- The enforcement -------------------------------------------------------------------------------

select throws_ok(
  $$insert into auth.users (id, email)
    values ('00000000-0000-4000-8000-000000000203', 'switch-new@example.test')$$,
  'P0001', 'Sign-ups are closed.',
  'no account can be created while sign-ups are off, not even as postgres'
);
select lives_ok(
  $$update auth.users set last_sign_in_at = now()
    where id = '00000000-0000-4000-8000-000000000202'$$,
  'an existing user can still sign in, which updates their row but inserts none'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000201", "role": "authenticated"}';
select results_eq(
  $$select allow_signups from public.set_allow_signups(true)$$,
  $$values (true)$$,
  'A turns sign-ups back on'
);
reset role;

select lives_ok(
  $$insert into auth.users (id, email)
    values ('00000000-0000-4000-8000-000000000203', 'switch-new@example.test')$$,
  'accounts can be created again once sign-ups are back on'
);

delete from public.site_settings;
select ok(not public.signups_allowed(), 'a missing settings row reads as closed, not open');
select throws_ok(
  $$insert into auth.users (id, email)
    values ('00000000-0000-4000-8000-000000000204', 'switch-other@example.test')$$,
  'P0001', 'Sign-ups are closed.',
  'and so refuses new accounts'
);

-- Deleting the admin's account ------------------------------------------------------------------

delete from auth.users where id = '00000000-0000-4000-8000-000000000201';
select is(
  (select count(*)::int from public.admins where user_id = '00000000-0000-4000-8000-000000000201'),
  0,
  'deleting an admin''s account removes them from admins, so a new account can''t inherit it'
);

select * from finish();

rollback;
