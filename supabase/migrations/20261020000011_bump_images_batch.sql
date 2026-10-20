-- Batch "bump to top" for re-uploads. A drop of already-uploaded images used to bump them one
-- request at a time, so one slow database call stalled the whole drop. Additive: a new function,
-- nothing else changes, and bump_image keeps working for the running app.

-- bump_images -----------------------------------------------------------------------------------

-- Moves the caller's own images to the top of their gallery in one statement, and keeps the order
-- of the array: the LAST id ends up on top, as if the images had been bumped one after another.
-- now() is frozen inside a statement, so each image gets now() plus its position in microseconds.
-- An id that is missing or belongs to someone else is skipped, which reads the same as missing, and
-- an id given twice takes its last position. Returns the ids it bumped, in array order, so the
-- API can tell which of the requested images moved.
create function public.bump_images(p_image_ids bigint[])
returns setof bigint
language plpgsql
security definer
set search_path = ''
as $$
begin
  return query
  with requested as (
    select r.image_id, max(r.ord) as ord
    from unnest(p_image_ids) with ordinality as r(image_id, ord)
    group by r.image_id
  ),
  bumped as (
    update public.images i
    set last_uploaded_at = now() + requested.ord * interval '1 microsecond'
    from requested
    where i.id = requested.image_id
      and i.user_id = (select auth.uid())
    returning i.id, requested.ord
  )
  select bumped.id from bumped order by bumped.ord;
end;
$$;

-- Authenticated only, like bump_image: the function reads auth.uid(), which is null for the worker.
revoke execute on function public.bump_images(bigint[]) from public, anon, service_role;
grant execute on function public.bump_images(bigint[]) to authenticated;
