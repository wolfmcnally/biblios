"""Shared fixtures for the biblios test suite."""

from __future__ import annotations

import re
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@pytest.fixture(scope="session", autouse=True)
def guard_network_egress() -> Iterator[str]:
    """Fail any test that opens a real network connection to a nonlocal host.

    Tests are hermetic: a test that silently reaches the network passes or
    fails with the weather. Local sockets (loopback, unix-domain) stay allowed
    so servers under test still work. The guard is itself exercised by
    test_hermeticity.py — a guard whose failure mode is silence needs a test
    proving it is armed.
    """
    sentinel = "network egress guard active"
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> None:
        if self.family == getattr(socket, "AF_UNIX", object()):
            return original_connect(self, address)
        host = address[0] if isinstance(address, tuple) and address else None
        if isinstance(host, str) and host in _LOCAL_HOSTS:
            return original_connect(self, address)
        pytest.fail(f"test attempted real network egress: connect({address!r})")

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket.socket, "connect", guarded_connect)
    try:
        yield sentinel
    finally:
        patcher.undo()


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Path:
    """A per-test directory under pytest's own temporary root, never a synced folder.

    The repository's methodology suite redirects ``tmp_path`` into the checkout's ignored
    runtime directory so its qualifying runs persist; when both suites are collected in one
    session that override reaches these tests too. A shelf's Castle store refuses any object
    whose read-only mode a sync daemon has rewritten, so the product proofs keep their
    shelves outside the synced tree, where pytest puts them by default.
    """
    name = re.sub(r"[^A-Za-z0-9_-]", "_", request.node.name)[:40]
    return tmp_path_factory.mktemp(name, numbered=True)
