"""SharingService against an in-memory ShareRepo: no network and no Supabase."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from gallery.domain.errors import NotFoundError, ShareRefusedError
from gallery.domain.models import GalleryShare, ShareDirection, ShareStatus
from gallery.services.sharing import SharingService

FRIEND_ID = UUID("0d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a")
KNOWN_ADDRESSES = {"friend@example.test": FRIEND_ID}


def make_share(
    share_id: int, direction: ShareDirection, status: ShareStatus = ShareStatus.PENDING
) -> GalleryShare:
    return GalleryShare(
        id=share_id,
        direction=direction,
        other_user_id=FRIEND_ID,
        email="friend@example.test",
        status=status,
        created_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        accepted_at=None,
    )


class FakeShareRepo:
    """Answers like the adapter: refusals raise, a missing share is False."""

    def __init__(self, *shares: GalleryShare) -> None:
        self.shares = {share.id: share for share in shares}
        self.created: list[str] = []

    async def list(self) -> list[GalleryShare]:
        return sorted(self.shares.values(), key=lambda share: share.id, reverse=True)

    async def create(self, email: str) -> GalleryShare:
        self.created.append(email)
        if email not in KNOWN_ADDRESSES:
            raise ShareRefusedError("no_account", "No account uses this e-mail address.")
        share = make_share(len(self.shares) + 1, ShareDirection.OUTGOING)
        self.shares[share.id] = share
        return share

    async def accept(self, share_id: int) -> bool:
        share = self.shares.get(share_id)
        if share is None or share.direction is not ShareDirection.INCOMING:
            return False
        self.shares[share_id] = make_share(share_id, share.direction, ShareStatus.ACCEPTED)
        return True

    async def delete(self, share_id: int) -> bool:
        return self.shares.pop(share_id, None) is not None


async def test_list_splits_the_shares_by_direction_keeping_their_order() -> None:
    repo = FakeShareRepo(
        make_share(1, ShareDirection.OUTGOING),
        make_share(2, ShareDirection.INCOMING),
        make_share(3, ShareDirection.OUTGOING),
    )

    outgoing, incoming = await SharingService(repo).list()

    assert [share.id for share in outgoing] == [3, 1]
    assert [share.id for share in incoming] == [2]


async def test_share_sends_the_address_trimmed_and_lowercased() -> None:
    repo = FakeShareRepo()

    share = await SharingService(repo).share("  Friend@Example.TEST ")

    assert repo.created == ["friend@example.test"]
    assert share.direction is ShareDirection.OUTGOING
    assert share.status is ShareStatus.PENDING


async def test_share_passes_a_refusal_on() -> None:
    with pytest.raises(ShareRefusedError) as raised:
        await SharingService(FakeShareRepo()).share("nobody@example.test")

    assert raised.value.code == "no_account"


async def test_accept_accepts_a_share_made_to_the_caller() -> None:
    repo = FakeShareRepo(make_share(4, ShareDirection.INCOMING))

    await SharingService(repo).accept(4)

    assert repo.shares[4].status is ShareStatus.ACCEPTED


async def test_accepting_a_share_the_caller_did_not_receive_is_not_found() -> None:
    repo = FakeShareRepo(make_share(4, ShareDirection.OUTGOING))

    with pytest.raises(NotFoundError):
        await SharingService(repo).accept(4)


async def test_remove_ends_a_share() -> None:
    repo = FakeShareRepo(make_share(4, ShareDirection.INCOMING))

    await SharingService(repo).remove(4)

    assert repo.shares == {}


async def test_removing_a_missing_share_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await SharingService(FakeShareRepo()).remove(4)
