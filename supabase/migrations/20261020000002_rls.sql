-- Row-level security for the gallery tables (docs/architecture.md sections 3 and 5).
-- The API calls PostgREST with the user's JWT, so these policies are the only thing keeping one
-- tenant's rows from another. The worker uses the secret key (service_role bypasses RLS).
-- auth.uid() is wrapped in (select ...) so Postgres evaluates it once per statement, not per row.

alter table public.images enable row level security;
alter table public.image_metadata enable row level security;

-- images ----------------------------------------------------------------------------------------

-- Owners list and open only their own images.
create policy "images: owner can select"
  on public.images for select to authenticated
  using (user_id = (select auth.uid()));

-- Owners can only create images that belong to themselves.
create policy "images: owner can insert"
  on public.images for insert to authenticated
  with check (user_id = (select auth.uid()));

-- Owners delete only their own images; the metadata row goes with it through the FK cascade.
create policy "images: owner can delete"
  on public.images for delete to authenticated
  using (user_id = (select auth.uid()));

-- No update policy: an image row never changes after ingest.

-- image_metadata --------------------------------------------------------------------------------

-- Owners read the analysis state of their own images.
create policy "image_metadata: owner can select"
  on public.image_metadata for select to authenticated
  using (user_id = (select auth.uid()));

-- Owners can only queue a fresh 'pending' analysis, and only for an image they own.
create policy "image_metadata: owner can queue analysis"
  on public.image_metadata for insert to authenticated
  with check (
    user_id = (select auth.uid())
    and ai_processing_status = 'pending'
    and exists (
      select 1
      from public.images i
      where i.id = image_metadata.image_id
        and i.user_id = (select auth.uid())
    )
  );

-- No update or delete policies: the worker writes results with the secret key, reanalysis goes
-- through the request_reanalysis RPC (#4), and rows are removed by the cascade from images.

-- Grants ----------------------------------------------------------------------------------------

-- Supabase grants every Data API role full access to new public tables by default. Replace that
-- with exactly what each role needs: anon gets nothing, and authenticated loses UPDATE and
-- TRUNCATE (TRUNCATE ignores RLS). Explicit grants also keep the API working on projects that
-- no longer auto-expose new tables.
revoke all on table public.images, public.image_metadata, public.image_details
  from anon, authenticated;
revoke all on sequence public.images_id_seq, public.image_metadata_id_seq
  from anon, authenticated;

grant select, insert, delete on table public.images to authenticated;
grant select, insert on table public.image_metadata to authenticated;
grant select on table public.image_details to authenticated;

grant select, insert, update, delete on table public.images, public.image_metadata
  to service_role;
grant select on table public.image_details to service_role;
