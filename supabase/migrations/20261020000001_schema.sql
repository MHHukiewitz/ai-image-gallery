-- Core tables for the gallery (docs/architecture.md section 5).
-- The challenge's table and column names are kept; columns are only added.
-- RLS, grants and policies live in 20261020000002_rls.sql.

create table public.images (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  filename varchar(255) not null,  -- display only, sanitized by the API
  original_path text not null,     -- {user_id}/{uuid}/original.jpg|png
  thumbnail_path text not null,    -- {user_id}/{uuid}/thumb.webp
  mime_type text not null,
  size_bytes int not null,
  width int not null,
  height int not null,
  content_sha256 char(64) not null,
  uploaded_at timestamptz not null default now()
);

-- One row per image. It is also the analysis queue: the worker claims rows by ai_processing_status.
create table public.image_metadata (
  id bigint generated always as identity primary key,
  image_id bigint not null unique references public.images (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  description text,
  tags text[] not null default '{}',
  colors varchar(7)[] not null default '{}',
  ai_processing_status varchar(20) not null default 'pending'
    check (ai_processing_status in ('pending', 'processing', 'done', 'failed')),
  attempts int not null default 0,
  next_attempt_at timestamptz,
  claimed_at timestamptz,
  last_error text,
  analyzer_id text,
  search_vector tsvector,  -- filled by #5's trigger: tags (weight A) + description (B)
  colors_lab float8[],     -- filled by #5's trigger: CIE Lab of colors, 3 values per color, flattened
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Keyset pagination of a user's gallery, newest first.
create index images_user_id_uploaded_at_id_idx
  on public.images (user_id, uploaded_at desc, id desc);

-- RLS filters every metadata read by user_id.
create index image_metadata_user_id_idx
  on public.image_metadata (user_id);

-- The worker's claim query only ever scans pending rows that are due.
create index image_metadata_pending_next_attempt_at_idx
  on public.image_metadata (next_attempt_at)
  where ai_processing_status = 'pending';

create index image_metadata_tags_idx
  on public.image_metadata using gin (tags);

create index image_metadata_search_vector_idx
  on public.image_metadata using gin (search_vector);

create function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger image_metadata_set_updated_at
  before update on public.image_metadata
  for each row execute function public.set_updated_at();

-- One row per image with its analysis state, in the shape the API serves. security_invoker makes
-- the view run with the caller's privileges, so the RLS on both tables still applies through it.
-- The inner join hides an image whose metadata insert never happened (a half-finished upload).
create view public.image_details
with (security_invoker = true)
as
select
  i.id,
  i.user_id,
  i.filename,
  i.original_path,
  i.thumbnail_path,
  i.mime_type,
  i.size_bytes,
  i.width,
  i.height,
  i.content_sha256,
  i.uploaded_at,
  m.description,
  m.tags,
  m.colors,
  m.ai_processing_status,
  m.last_error
from public.images i
join public.image_metadata m on m.image_id = i.id;
