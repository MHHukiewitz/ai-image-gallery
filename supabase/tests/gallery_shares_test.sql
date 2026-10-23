-- Gallery sharing: the gallery_shares table, its RPCs, and the read access an accepted share gives
-- its recipient to the owner's images, analyses and stored files, and to nothing else.
-- Run with `supabase test db`. Everything happens in one transaction that rolls back.
--
-- Owner O = 00000000-0000-4000-8000-000000000191 owns o-1 and shares with R and S.
-- Recipient R = 00000000-0000-4000-8000-000000000192 owns r-1 and accepts O's share.
-- S = 00000000-0000-4000-8000-000000000193 is asked but never accepts.
-- T = 00000000-0000-4000-8000-000000000194 is never asked.

begin;

select plan(43);

-- Fixtures (as postgres, which bypasses RLS) ----------------------------------------------------

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-000000000191', 'share-owner@example.test'),
  ('00000000-0000-4000-8000-000000000192', 'share-recipient@example.test'),
  ('00000000-0000-4000-8000-000000000193', 'share-pending@example.test'),
  ('00000000-0000-4000-8000-000000000194', 'share-stranger@example.test');

insert into public.images (
  user_id, filename, original_path, thumbnail_path,
  mime_type, size_bytes, width, height, content_sha256
)
select
  fixture.user_id,
  fixture.slug || '.jpg',
  format('%s/%s/original.jpg', fixture.user_id, fixture.slug),
  format('%s/%s/thumb.webp', fixture.user_id, fixture.slug),
  'image/jpeg', 1024, 640, 480, repeat('a', 64)
from (
  values
    ('o-1', '00000000-0000-4000-8000-000000000191'::uuid),
    ('r-1', '00000000-0000-4000-8000-000000000192'::uuid)
) as fixture (slug, user_id);

insert into public.image_metadata (image_id, user_id, ai_processing_status, tags)
select i.id, i.user_id, 'done', '{beach}'::text[]
from public.images i
where i.filename in ('o-1.jpg', 'r-1.jpg');

select set_config('tests.o1_id', id::text, true) from public.images where filename = 'o-1.jpg';

insert into storage.objects (bucket_id, name)
values
  ('images', '00000000-0000-4000-8000-000000000191/o-1/original.jpg'),
  ('images', '00000000-0000-4000-8000-000000000192/r-1/original.jpg');

-- Structure -------------------------------------------------------------------------------------

select ok(
  (select relrowsecurity from pg_class where oid = 'public.gallery_shares'::regclass),
  'RLS is enabled on gallery_shares'
);
select policies_are(
  'public', 'gallery_shares',
  array['gallery_shares: owner and recipient can select', 'gallery_shares: owner and recipient can delete'],
  'gallery_shares has select and delete policies, and no insert or update policy'
);
select table_privs_are(
  'public', 'gallery_shares', 'anon', array[]::text[], 'anon has no privileges on gallery_shares'
);
select table_privs_are(
  'public', 'gallery_shares', 'authenticated', array['SELECT', 'DELETE'],
  'authenticated can only select and delete gallery_shares; the RPCs create and accept them'
);
select ok(
  not has_function_privilege('anon', 'public.share_gallery(text)', 'execute'),
  'anon can''t share a gallery'
);
select ok(
  not has_function_privilege('anon', 'public.list_gallery_shares()', 'execute'),
  'anon can''t list shares'
);
select ok(
  not has_function_privilege('anon', 'public.accept_gallery_share(bigint)', 'execute'),
  'anon can''t accept a share'
);
select throws_ok(
  $$insert into public.gallery_shares (owner_id, recipient_id)
    values ('00000000-0000-4000-8000-000000000191', '00000000-0000-4000-8000-000000000191')$$,
  '23514', null,
  'a user can''t share with themselves, even as postgres'
);

-- share_gallery ---------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000191", "role": "authenticated"}';

select results_eq(
  $$select email, status from public.share_gallery('  Share-Recipient@Example.TEST ')$$,
  $$values ('share-recipient@example.test', 'pending')$$,
  'O shares with R by an address in any case and with spaces, and the share starts pending'
);
select lives_ok(
  $$select public.share_gallery('share-pending@example.test')$$,
  'O shares with S too'
);
select throws_ok(
  $$select public.share_gallery('share-recipient@example.test')$$,
  'PT409', 'Your gallery is already shared with this address.',
  'sharing twice with the same account is refused as a conflict'
);
select throws_ok(
  $$select public.share_gallery('nobody@example.test')$$,
  'PT404', 'No account uses this e-mail address.',
  'an address without an account is refused, so no request waits for whoever registers it'
);
select throws_ok(
  $$select public.share_gallery('share-owner@example.test')$$,
  'PT422', 'That is your own e-mail address.',
  'O can''t share with O''s own address'
);
select throws_ok(
  $$insert into public.gallery_shares (owner_id, recipient_id)
    values ('00000000-0000-4000-8000-000000000191', '00000000-0000-4000-8000-000000000194')$$,
  '42501', 'permission denied for table gallery_shares',
  'O can''t insert a share directly, past the e-mail lookup'
);
select throws_ok(
  $$update public.gallery_shares set status = 'accepted'$$,
  '42501', 'permission denied for table gallery_shares',
  'O can''t accept O''s own share by updating it'
);

select results_eq(
  $$select direction, email, status from public.list_gallery_shares() order by email$$,
  $$values
    ('outgoing', 'share-pending@example.test', 'pending'),
    ('outgoing', 'share-recipient@example.test', 'pending')$$,
  'O lists both shares as outgoing, with the recipients'' addresses'
);

reset role;
select set_config(
  'tests.share_r_id',
  (select id::text from public.gallery_shares where recipient_id = '00000000-0000-4000-8000-000000000192'),
  true
);
select set_config(
  'tests.share_s_id',
  (select id::text from public.gallery_shares where recipient_id = '00000000-0000-4000-8000-000000000193'),
  true
);

-- A pending share gives nothing -----------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000192", "role": "authenticated"}';

select results_eq(
  $$select direction, other_user_id, email, status from public.list_gallery_shares()$$,
  $$values ('incoming', '00000000-0000-4000-8000-000000000191'::uuid, 'share-owner@example.test', 'pending')$$,
  'R sees O''s request as incoming, with O''s address'
);
select results_eq(
  'select filename::text from public.image_details order by filename',
  array['r-1.jpg'],
  'before accepting, R sees only R''s own images'
);
select is_empty(
  $$select name from storage.objects where name like '00000000-0000-4000-8000-000000000191/%'$$,
  'before accepting, R can''t read O''s files'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000194", "role": "authenticated"}';

select is(
  public.accept_gallery_share(current_setting('tests.share_r_id')::bigint),
  false,
  'T can''t accept a share made to R'
);
select is_empty('select id from public.gallery_shares', 'T sees no shares');
select is_empty('select id from public.list_gallery_shares()', 'T lists no shares');
select is_empty(
  $$delete from public.gallery_shares returning id$$,
  'T can''t delete anyone''s share'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000191", "role": "authenticated"}';

select is(
  public.accept_gallery_share(current_setting('tests.share_r_id')::bigint),
  false,
  'O can''t accept O''s own share on R''s behalf'
);

-- Accepting -------------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000192", "role": "authenticated"}';

select is(
  public.accept_gallery_share(current_setting('tests.share_r_id')::bigint),
  true,
  'R accepts O''s share'
);
select is(
  public.accept_gallery_share(current_setting('tests.share_r_id')::bigint),
  true,
  'accepting again succeeds, so a retried request is harmless'
);
select results_eq(
  $$select status, accepted_at is not null from public.list_gallery_shares()$$,
  $$values ('accepted', true)$$,
  'the share is accepted, with the time'
);
select results_eq(
  'select filename::text from public.image_details order by filename',
  array['o-1.jpg', 'r-1.jpg'],
  'R now sees O''s images next to R''s own'
);
select results_eq(
  'select user_id from public.image_metadata order by user_id',
  array['00000000-0000-4000-8000-000000000191', '00000000-0000-4000-8000-000000000192']::uuid[],
  'R reads the analysis of O''s images'
);
select results_eq(
  $$select filename::text from public.search_images('beach') order by filename$$,
  array['o-1.jpg', 'r-1.jpg'],
  'R''s search covers O''s images'
);
select results_eq(
  $$select name from storage.objects where bucket_id = 'images' order by name$$,
  array[
    '00000000-0000-4000-8000-000000000191/o-1/original.jpg',
    '00000000-0000-4000-8000-000000000192/r-1/original.jpg'
  ],
  'R can read O''s files, so the API can sign their URLs as R'
);

-- Read only: a recipient changes nothing of the owner's ------------------------------------------

select is_empty(
  $$delete from public.images where id = current_setting('tests.o1_id')::bigint returning id$$,
  'R can''t delete O''s image'
);
select is(
  public.bump_image(current_setting('tests.o1_id')::bigint),
  false,
  'R can''t bump O''s image'
);
select is(
  public.request_reanalysis(current_setting('tests.o1_id')::bigint),
  false,
  'R can''t re-analyze O''s image'
);

set local storage.allow_delete_query = 'true';
select is_empty(
  $$delete from storage.objects
    where name = '00000000-0000-4000-8000-000000000191/o-1/original.jpg'
    returning name$$,
  'R can''t delete O''s files'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('images', '00000000-0000-4000-8000-000000000191/o-1/planted.jpg')$$,
  '42501', 'new row violates row-level security policy for table "objects"',
  'R can''t add a file to O''s folder'
);

-- Only the recipient of an accepted share gets in -----------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000193", "role": "authenticated"}';

select results_eq(
  'select filename::text from public.image_details',
  array[]::text[],
  'S, whose share is still pending, sees none of O''s images'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000191", "role": "authenticated"}';

select results_eq(
  'select filename::text from public.image_details',
  array['o-1.jpg'],
  'sharing is one way: O doesn''t see R''s images'
);

-- Ending a share ----------------------------------------------------------------------------------

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000193", "role": "authenticated"}';

select results_eq(
  $$delete from public.gallery_shares returning id$$,
  array[current_setting('tests.share_s_id')::bigint],
  'S declines O''s request by deleting it, and only that one'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000191", "role": "authenticated"}';

select results_eq(
  $$delete from public.gallery_shares
    where id = current_setting('tests.share_r_id')::bigint
    returning id$$,
  array[current_setting('tests.share_r_id')::bigint],
  'O revokes R''s share'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-4000-8000-000000000192", "role": "authenticated"}';

select results_eq(
  'select filename::text from public.image_details',
  array['r-1.jpg'],
  'after the revoke, R sees only R''s own images again'
);
select is_empty(
  $$select name from storage.objects where name like '00000000-0000-4000-8000-000000000191/%'$$,
  'after the revoke, R can''t read O''s files'
);

-- A deleted account takes its shares along ------------------------------------------------------

reset role;
insert into public.gallery_shares (owner_id, recipient_id, status)
values ('00000000-0000-4000-8000-000000000191', '00000000-0000-4000-8000-000000000194', 'accepted');
delete from auth.users where id = '00000000-0000-4000-8000-000000000194';

select is_empty(
  $$select id from public.gallery_shares where recipient_id = '00000000-0000-4000-8000-000000000194'$$,
  'deleting a recipient''s account removes the shares made to it'
);

select * from finish();

rollback;
