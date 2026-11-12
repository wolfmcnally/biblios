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


class NamespaceFormatError(BibliosError):
    """Castle refused to open the shelf's namespace as a format this release does not open.

    A shelf written with Castle 0.3 keeps its namespace in Castle's v1 format, which
    Castle 0.4 opens only after the operator migrates it explicitly, once, with a
    backup, through ``castle.namespace_migrate_v1.migrate_namespace_v1(namespace_root,
    cas_root, session, backup)``. Biblios never migrates and never alters the store:
    the migrator verifies the exact v1 shape and refuses anything else, so a namespace
    that is not v1 but damaged is left for Castle's explicit diagnosis.
    """

    MIGRATION = "castle.namespace_migrate_v1.migrate_namespace_v1"

    def __init__(self, namespace_root: object) -> None:
        self.namespace_root = namespace_root
        super().__init__(
            f"the Castle namespace at {namespace_root} is not in a format this release "
            "opens (namespace-format-unsupported). A shelf written with Castle 0.3 is "
            "opened only after the operator migrates it explicitly with "
            f"{self.MIGRATION}(namespace_root, cas_root, session, backup); Biblios never "
            "migrates it, and the migrator refuses a namespace that is not exactly v1."
        )


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
