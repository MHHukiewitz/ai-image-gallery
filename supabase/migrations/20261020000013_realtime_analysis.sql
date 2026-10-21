-- Pushes analysis status changes to the browser. Supabase Realtime's Postgres Changes streams a
-- table's changes only if the table is in the supabase_realtime publication, and it sends each
-- subscriber only the rows its RLS select policy lets it read (image_metadata: user_id = auth.uid()).
-- Additive: no schema or policy changes, and a client that doesn't subscribe sees no difference.

-- The membership check keeps the migration from failing if the table was already added by hand
-- in the Supabase dashboard (Database > Replication), which ALTER PUBLICATION would reject.
do $$
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
$$;
