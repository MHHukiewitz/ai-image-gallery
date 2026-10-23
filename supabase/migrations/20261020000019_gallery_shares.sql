-- Gallery sharing (docs/architecture.md section 5, "Sharing"). A user shares their whole gallery
-- with another account, named by its e-mail address. The recipient sees the request the next time
-- they sign in, and once they accept it the sharer's images, analyses and stored files become
-- readable to them through the same RLS that keeps them from everyone else.
--
-- Read access only: a recipient can't delete, bump or re-analyze a shared image, because those
-- policies and RPCs still check user_id = auth.uid(). Additive: the new policies widen reads for
-- recipients of an accepted share, and nothing changes for anyone else.

create table public.gallery_shares (
  id bigint generated always as identity primary key,
  owner_id uuid not null references auth.users (id) on delete cascade,
  recipient_id uuid not null references auth.users (id) on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'accepted')),
  created_at timestamptz not null default now(),
  accepted_at timestamptz,
  constraint gallery_shares_not_with_self check (owner_id <> recipient_id),
  constraint gallery_shares_owner_id_recipient_id_key unique (owner_id, recipient_id)
);

-- The read policies below find the caller's accepted shares by recipient on every gallery read.
create index gallery_shares_recipient_id_status_idx
  on public.gallery_shares (recipient_id, status);

alter table public.gallery_shares enable row level security;

-- Both sides of a share see it: the owner lists whom they share with, the recipient their requests.
create policy "gallery_shares: owner and recipient can select"
  on public.gallery_shares for select to authenticated
  using (owner_id = (select auth.uid()) or recipient_id = (select auth.uid()));

-- Either side ends a share: the owner revokes it, the recipient declines or leaves it.
create policy "gallery_shares: owner and recipient can delete"
  on public.gallery_shares for delete to authenticated
  using (owner_id = (select auth.uid()) or recipient_id = (select auth.uid()));

-- No insert or update policy. A share is created by e-mail, which needs auth.users, so
-- share_gallery does it; and only the recipient may accept, which accept_gallery_share checks.

revoke all on table public.gallery_shares from anon, authenticated;
revoke all on sequence public.gallery_shares_id_seq from anon, authenticated;
grant select, delete on table public.gallery_shares to authenticated;

-- Reads for recipients ---------------------------------------------------------------------------

-- Each policy below is permissive, so Postgres ORs it with the owner's policy on the same table.
-- The subquery reads gallery_shares under its own select policy, which never looks at images, so
-- the policies don't recurse.

create policy "images: recipients of a shared gallery can select"
  on public.images for select to authenticated
  using (
    user_id in (
      select s.owner_id
      from public.gallery_shares s
      where s.recipient_id = (select auth.uid())
        and s.status = 'accepted'
    )
  );

-- Also what lets Realtime send a recipient the analysis updates of a shared gallery.
create policy "image_metadata: recipients of a shared gallery can select"
  on public.image_metadata for select to authenticated
  using (
    user_id in (
      select s.owner_id
      from public.gallery_shares s
      where s.recipient_id = (select auth.uid())
        and s.status = 'accepted'
    )
  );

-- Lets the API sign URLs for a shared gallery's files as the recipient. Insert and delete stay
-- limited to the owner's own folder.
create policy "images bucket: recipients of a shared gallery can select"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'images'
    and (storage.foldername(name))[1] in (
      select s.owner_id::text
      from public.gallery_shares s
      where s.recipient_id = (select auth.uid())
        and s.status = 'accepted'
    )
  );

-- share_gallery ----------------------------------------------------------------------------------

-- Asks the account that uses p_email to accept the caller's gallery. Only existing accounts can be
-- asked: e-mail confirmation is off, so a request held for an address until someone registers it
-- would go to whoever registers it first. Sign-up already tells anyone whether an address has an
-- account, so saying so here reveals nothing new.
--
-- Refusals raise PT<status>, which PostgREST answers with that HTTP status, keeping the code in the
-- error body for the API to map: PT404 no account uses the address, PT409 the caller already
-- shares with it, PT422 it is the caller's own address. Returns the new share in the shape
-- list_gallery_shares lists it.
create function public.share_gallery(p_email text)
returns table (
  id bigint,
  direction text,
  other_user_id uuid,
  email text,
  status text,
  created_at timestamptz,
  accepted_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller uuid := (select auth.uid());
  recipient uuid;
  recipient_email text;
  created public.gallery_shares;
begin
  if caller is null then
    raise exception 'Not signed in.' using errcode = '42501';
  end if;

  -- Auth stores addresses in lowercase, so this matches its unique index.
  select u.id, u.email
  into recipient, recipient_email
  from auth.users u
  where u.email = lower(btrim(p_email))
    and u.deleted_at is null;

  if recipient is null then
    raise exception 'No account uses this e-mail address.' using errcode = 'PT404';
  end if;
  if recipient = caller then
    raise exception 'That is your own e-mail address.' using errcode = 'PT422';
  end if;

  insert into public.gallery_shares as s (owner_id, recipient_id)
  values (caller, recipient)
  on conflict on constraint gallery_shares_owner_id_recipient_id_key do nothing
  returning s.* into created;

  if created.id is null then
    raise exception 'Your gallery is already shared with this address.' using errcode = 'PT409';
  end if;

  return query
  select created.id, 'outgoing', recipient, recipient_email, created.status, created.created_at,
    created.accepted_at;
end;
$$;

-- Authenticated only: the function reads auth.uid(), which is null for the worker.
revoke execute on function public.share_gallery(text) from public, anon, service_role;
grant execute on function public.share_gallery(text) to authenticated;

-- accept_gallery_share ---------------------------------------------------------------------------

-- Accepts a share made to the caller. Returns false when there is no such share, or it was made to
-- someone else. Accepting one that is already accepted returns true and keeps the first time, so a
-- retried request is harmless.
create function public.accept_gallery_share(p_share_id bigint)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.gallery_shares s
  set status = 'accepted',
      accepted_at = coalesce(s.accepted_at, now())
  where s.id = p_share_id
    and s.recipient_id = (select auth.uid());

  return found;
end;
$$;

revoke execute on function public.accept_gallery_share(bigint) from public, anon, service_role;
grant execute on function public.accept_gallery_share(bigint) to authenticated;

-- list_gallery_shares ----------------------------------------------------------------------------

-- The caller's shares in both directions, newest first, each with the other side's id and current
-- e-mail address. Security definer because only it can read auth.users; it reads nothing beyond
-- the addresses of the accounts the caller shares with.
create function public.list_gallery_shares()
returns table (
  id bigint,
  direction text,
  other_user_id uuid,
  email text,
  status text,
  created_at timestamptz,
  accepted_at timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select
    s.id,
    case when s.owner_id = (select auth.uid()) then 'outgoing' else 'incoming' end,
    u.id,
    u.email::text,
    s.status,
    s.created_at,
    s.accepted_at
  from public.gallery_shares s
  join auth.users u
    on u.id = case when s.owner_id = (select auth.uid()) then s.recipient_id else s.owner_id end
  where s.owner_id = (select auth.uid())
     or s.recipient_id = (select auth.uid())
  order by s.created_at desc, s.id desc
$$;

revoke execute on function public.list_gallery_shares() from public, anon, service_role;
grant execute on function public.list_gallery_shares() to authenticated;
