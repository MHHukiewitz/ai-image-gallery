-- image_metadata is streamed over Supabase Realtime, so the web app hears about analysis status
-- changes without polling. Run with `supabase test db`. Everything happens in one transaction that
-- rolls back.
--
-- Realtime sends each subscriber only the rows its RLS select policy allows. That filtering lives
-- in the Realtime server, so this file checks what the database owns: the publication, and that
-- the table has RLS on (a table without RLS would stream every user's rows to every subscriber).
-- Who can see which row is covered in rls_test.sql.

begin;

select plan(5);

select ok(
  exists (select from pg_publication where pubname = 'supabase_realtime'),
  'the supabase_realtime publication exists'
);

select ok(
  exists (
    select
    from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'image_metadata'
  ),
  'image_metadata is in the supabase_realtime publication'
);

-- The web app listens for UPDATEs of ai_processing_status.
select ok(
  (select pubupdate from pg_publication where pubname = 'supabase_realtime'),
  'the supabase_realtime publication publishes UPDATEs'
);

select ok(
  (select relrowsecurity from pg_class where oid = 'public.image_metadata'::regclass),
  'image_metadata has row level security on, so Realtime only sends a subscriber their own rows'
);

-- The migration's statement, run again on a database that already has the table in the
-- publication: re-applying it (a reset, a replay) must not fail.
select lives_ok(
  $$
    do $migration$
    begin
      if not exists (
        select
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'image_metadata'
      ) then
        alter publication supabase_realtime add table public.image_metadata;
      end if;
    end
    $migration$
  $$,
  'adding image_metadata to the publication again is a no-op'
);

select * from finish();

rollback;
