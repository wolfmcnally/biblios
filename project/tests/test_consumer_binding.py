"""Consumer storage admission and lifetime at the real handle boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import biblios.binding as binding_module
from biblios.binding import bind_shelf
from biblios.control import ConsumerError
from biblios.curation import CreateDirectory, CurationRequest
from tests.consumer_fixtures import consumer_fixture, identifier, inventory


@pytest.mark.parametrize("state", ("pending", "wrong-epoch", "malformed"))
def test_unfinished_disposition_refuses_new_binding_without_storage_effects(
    tmp_path: Path,
    state: str,
) -> None:
    fixture = consumer_fixture(tmp_path)
    file = fixture.binding.palace_root / "consumer-excision.json"
    file.write_text(
        "not JSON"
        if state == "malformed"
        else json.dumps(
            {
                "state": "pending" if state == "pending" else "complete",
                "epoch_id": identifier(),
            }
        )
    )
    before = inventory(fixture.binding.root)
    try:
        with pytest.raises(ConsumerError) as refused:
            bind_shelf(fixture.binding, fixture.handle)
        assert (
            refused.value.code,
            refused.value.effect,
            fixture.handle.closed,
            inventory(fixture.binding.root),
        ) == (
            "derived-recovery-required",
            "recovery-required",
            False,
            before,
        ), "unfinished disposition admitted a reader or mutated storage"
    finally:
        fixture.close()


def test_borrowed_and_owned_lifetimes_include_refused_admission(
    tmp_path: Path, monkeypatch
) -> None:
    """A wrapper may not close a borrow, leak an owned refusal, or open a second writer."""

    def forbidden(*args, **kwargs):
        pytest.fail("binding opened a hidden namespace writer")

    monkeypatch.setattr(binding_module, "open_namespace", forbidden)
    results = []
    for owned in (False, True):
        for invalid in (False, True):
            fixture = consumer_fixture(tmp_path / f"{owned}-{invalid}")
            declared = replace(
                fixture.binding,
                ownership="owned" if owned else "borrowed",
                shelf_id=identifier() if invalid else fixture.binding.shelf_id,
            )
            try:
                if invalid:
                    with pytest.raises(ConsumerError, match="binding-mismatch"):
                        bind_shelf(declared, fixture.handle)
                else:
                    bound = bind_shelf(declared, fixture.handle)
                    bound.files()
                    bound.close()
                    with pytest.raises(ConsumerError, match="closed"):
                        bound.files()
                results.append((owned, invalid, fixture.handle.closed))
                if not owned:
                    results.append(
                        fixture.handle.info().namespace_id == fixture.binding.namespace_id
                    )
            finally:
                fixture.close()
    assert results == [
        (False, False, False),
        True,
        (False, True, False),
        True,
        (True, False, True),
        (True, True, True),
    ], "consumer handle lifetime violated borrow/own contract"


def test_invalid_domain_or_identity_refuses_before_any_storage_effect(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"not admitted")
    link = fixture.binding.root / "escape"
    link.symlink_to(outside, target_is_directory=True)
    declarations = (
        replace(fixture.binding, namespace_id=identifier()),
        replace(
            fixture.binding,
            expected_cas=replace(fixture.binding.expected_cas, epoch_id=identifier()),
        ),
        replace(fixture.binding, scratch_root=link),
        replace(fixture.binding, mirror_root=fixture.binding.castle_root),
        replace(fixture.binding, root=tmp_path / "missing-source-marker"),
    )
    before = inventory(tmp_path)
    errors = []
    try:
        for declared in declarations:
            with pytest.raises(ConsumerError) as refused:
                bind_shelf(declared, fixture.handle)
            errors.append(refused.value.effect)
        assert (errors, inventory(tmp_path)) == (["none"] * 5, before), (
            "invalid consumer binding touched admitted or outside storage"
        )
    finally:
        fixture.close()


def test_remote_vector_selector_refuses_before_client_construction(
    tmp_path: Path, monkeypatch
) -> None:
    import palace.index.s3vectors as vectors

    fixture = consumer_fixture(tmp_path)
    selector = fixture.binding.palace_root / "meta" / "vector-backend.toml"
    selector.write_text('backend = "s3vectors"\nprofile = "source-marker"\n')
    before = inventory(fixture.binding.root)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("constructed")
        pytest.fail("consumer constructed a remote vector backend")

    monkeypatch.setattr(vectors, "resolve_vector_backend", forbidden)
    try:
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.files()
        assert (
            refused.value.code,
            refused.value.effect,
            calls,
            inventory(fixture.binding.root),
        ) == (
            "backend-not-admitted",
            "none",
            [],
            before,
        ), "remote backend was admitted or refusal had storage/provider effects"
    finally:
        fixture.close()


def test_dynamic_readonly_read_and_refused_write_create_no_sidecars(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    stored = fixture.bound.curate(fixture.request(data=b"source-marker"))
    entry_id = stored.mutation.affected_entry_ids[0]
    fixture.bound.close()
    fixture.handle.close()
    before = inventory(fixture.binding.root)
    try:
        reader = fixture.reopen(read_only=True)
        found = reader.download(entry_id)
        status = reader.status()
        with pytest.raises(ConsumerError) as refused:
            reader.curate(
                CurationRequest(
                    identifier(), reader.view.revision, CreateDirectory(fixture.root_id, "refused")
                )
            )
        reader.close()
        # Ordinary status is the maintained counters: the live-file count is unknown
        # (None, never a guessed number) until Castle's constant-cost count is adopted.
        assert (found, status.files, refused.value.code, inventory(fixture.binding.root)) == (
            b"source-marker",
            None,
            "read-only",
            before,
        ), "dynamic read-only operations wrote or created a sidecar"
    finally:
        fixture.close()
