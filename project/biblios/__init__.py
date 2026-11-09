"""Biblios: standalone, portable knowledge bases ("shelves") searched alone or in a stack.

The Python library is the contract. Every shelf and stack operation is a library
call; the CLI is a thin layer over it, and an MCP server follows later.
"""

from biblios.config import Settings, load_settings
from biblios.entries import ENTRY_SCHEMA, Entry, EntryMetadata, EntryMetadataError
from biblios.errors import (
    BibliosError,
    DuplicateContentError,
    IndexLagError,
    MixedIdentityError,
    SealedShelfError,
    UndeclaredEmbedderError,
)
from biblios.progress import EntryProgress, IndexStatus, index_status
from biblios.reconcile import ReconcileReport
from biblios.seal import SealRecord, seal, unseal, verify_seal
from biblios.search import Hit, Page, SearchResult
from biblios.shelf import SHELF_SCHEMA, Mutation, Shelf, ShelfManifest, init_shelf, open_shelf
from biblios.stack import Stack, StackResult, open_stack
from biblios.verify import VerifyReport

__version__ = "0.1.0"

__all__ = [
    "ENTRY_SCHEMA",
    "SHELF_SCHEMA",
    "BibliosError",
    "DuplicateContentError",
    "Entry",
    "EntryMetadata",
    "EntryMetadataError",
    "Hit",
    "IndexLagError",
    "EntryProgress",
    "IndexStatus",
    "MixedIdentityError",
    "Stack",
    "StackResult",
    "Mutation",
    "Page",
    "ReconcileReport",
    "SealRecord",
    "SealedShelfError",
    "SearchResult",
    "Settings",
    "Shelf",
    "ShelfManifest",
    "UndeclaredEmbedderError",
    "VerifyReport",
    "__version__",
    "index_status",
    "init_shelf",
    "load_settings",
    "open_shelf",
    "open_stack",
    "seal",
    "unseal",
    "verify_seal",
]
