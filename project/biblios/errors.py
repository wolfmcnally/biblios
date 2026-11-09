"""Errors raised by the Biblios library."""

from __future__ import annotations

from typing import Any


class BibliosError(Exception):
    """Base class for every Biblios refusal."""


class DuplicateContentError(BibliosError):
    """``add`` found the same bytes already held by live entries of the shelf.

    This is the report the brief asks for: the caller decides whether a second entry
    pointing at the same bytes is wanted and, if so, calls ``add`` again with
    ``allow_duplicate=True``.
    """

    def __init__(self, entry_ids: tuple[str, ...]) -> None:
        self.entry_ids = entry_ids
        super().__init__(f"content already held by live entries: {', '.join(entry_ids)}")


class UndeclaredEmbedderError(BibliosError):
    """The shelf's Palace store has no declared embedder selector.

    A store without a selector silently resolves to Palace's default local identity, so
    Biblios refuses to build or update before the selector exists.
    """


class IndexLagError(BibliosError):
    """After reconcile, the index does not name exactly the live converted entries."""


class MixedIdentityError(BibliosError):
    """A stack named shelves of more than one embedding identity.

    ``identities`` maps each member's shelf id to the identity Palace resolves for it,
    so the message names every member and the odd ones out.
    """

    def __init__(self, identities: dict[str, Any]) -> None:
        self.identities = dict(identities)
        counts: dict[Any, int] = {}
        for identity in self.identities.values():
            counts[identity] = counts.get(identity, 0) + 1
        majority = max(counts, key=lambda identity: counts[identity])
        odd = sorted(s for s, i in self.identities.items() if i != majority)
        described = "; ".join(
            f"{shelf_id}: {identity.provider}/{identity.model}/{identity.dim}"
            for shelf_id, identity in sorted(self.identities.items())
        )
        super().__init__(
            f"stack members declare different embedding identities ({described}); "
            f"odd ones out: {', '.join(odd)}"
        )


class SealedShelfError(BibliosError):
    """A write was attempted on a sealed shelf, which is read-only until it is unsealed."""
