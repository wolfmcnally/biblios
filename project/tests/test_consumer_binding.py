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


def _rebuilt(tmp_path: Path):
    """Two entries indexed, then the whole derived store deleted while the mirrors and
    Castle survive, and the shelf bound again (the lost-store rebuild)."""
    import shutil

    from biblios.consumer import BoundShelf
    from biblios.mirrors import digest
    from tests.consumer_fixtures import lane_pipeline

    fixture = consumer_fixture(tmp_path)
    pipeline = lane_pipeline(fixture, [])
    ids = [
        fixture.bound.curate(
            fixture.request(f"n{index}.txt", f"lighthouse note {index}".encode())
        ).mutation.affected_entry_ids[0]
        for index in range(2)
    ]
    for entry_id in ids:
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
    mirrors = {
        entry_id: fixture.bound._shelf.mirror_path(entry_id).read_bytes() for entry_id in ids
    }
    shutil.rmtree(fixture.binding.palace_root)
    fixture.bound = BoundShelf(fixture.binding, fixture.bound._shelf)
    calls: list = []
    return (
        fixture,
        lane_pipeline(fixture, calls),
        calls,
        ids,
        mirrors,
        {entry_id: digest(data) for entry_id, data in mirrors.items()},
    )


def test_a_lost_derived_store_is_rebuilt_from_vouched_mirrors_without_conversion(
    tmp_path: Path,
) -> None:
    """Failure mode (observed by a consuming service): after the derived store is lost,
    every surviving mirror reads as user-owned and no route can index it again without
    deleting or rewriting the user's files."""
    from biblios.testing import HashEmbedder, OverlapReranker

    fixture, pipeline, calls, ids, mirrors, vouched = _rebuilt(tmp_path)
    try:
        root = fixture.binding.mirror_root

        def identities() -> dict:
            return {
                entry_id: (
                    fixture.bound._shelf.mirror_path(entry_id).stat().st_ino,
                    fixture.bound._shelf.mirror_path(entry_id).stat().st_mtime_ns,
                )
                for entry_id in ids
            }

        listing, before = sorted(path.name for path in root.iterdir()), identities()
        refused = fixture.bound.derive(pipeline, ids[0], operation_id=identifier())
        assert refused.outcome(ids[0]).outcome == "isolated:mirror-user-owned"
        for entry_id in ids:
            assert fixture.bound.grant_first_ingestion(
                entry_id,
                expected_cas_id=fixture.bound.resolve(entry_id).cas_id,
                operation_id=identifier(),
                expected_mirror_sha256=vouched[entry_id],
            )
            result = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
            assert result.outcome(entry_id).outcome == "searchable"
        assert calls == [], "an adopted mirror was converted"
        assert {
            entry_id: fixture.bound._shelf.mirror_path(entry_id).read_bytes() for entry_id in ids
        } == mirrors
        found = fixture.bound.search(
            "lighthouse", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        assert {hit.entry_id for hit in found.hits} == set(ids)
        # Nothing was renamed, retained or rewritten: same names, inodes and mtimes.
        assert (sorted(path.name for path in root.iterdir()), identities()) == (listing, before)
        # The grant was consumed at acceptance: a second one finds a converted record.
        with pytest.raises(ConsumerError) as again:
            fixture.bound.grant_first_ingestion(
                ids[0],
                expected_cas_id=fixture.bound.resolve(ids[0]).cas_id,
                operation_id=identifier(),
                expected_mirror_sha256=vouched[ids[0]],
            )
        assert again.value.code == "converted-before"
    finally:
        fixture.close()


def test_a_vouched_digest_adopts_only_exactly_those_bytes(tmp_path: Path) -> None:
    """Failure mode: a rebuild adopts a mirror the user changed, or one whose digest the
    caller never vouched for, or creates one where none exists."""
    fixture, pipeline, calls, ids, mirrors, vouched = _rebuilt(tmp_path)
    try:
        edited, missing = ids
        path = fixture.bound._shelf.mirror_path
        cas = {entry_id: fixture.bound.resolve(entry_id).cas_id for entry_id in ids}
        # The first call after the loss runs the preservation import, as a rebuild does.
        fixture.bound.derive(pipeline, edited, operation_id=identifier())
        with pytest.raises(ConsumerError) as wrong:
            fixture.bound.grant_first_ingestion(
                edited,
                expected_cas_id=cas[edited],
                operation_id=identifier(),
                expected_mirror_sha256="0" * 64,
            )
        assert wrong.value.code == "mirror-user-owned"
        assert fixture.bound.grant_first_ingestion(
            edited,
            expected_cas_id=cas[edited],
            operation_id=identifier(),
            expected_mirror_sha256=vouched[edited],
        )
        path(edited).write_bytes(mirrors[edited] + b"\nthe user's later edit\n")
        result = fixture.bound.derive(pipeline, edited, operation_id=identifier())
        assert result.outcome(edited).outcome == "isolated:mirror-user-owned"
        assert path(edited).read_bytes().endswith(b"the user's later edit\n")
        path(missing).unlink()
        with pytest.raises(ConsumerError) as absent:
            fixture.bound.grant_first_ingestion(
                missing,
                expected_cas_id=cas[missing],
                operation_id=identifier(),
                expected_mirror_sha256=vouched[missing],
            )
        assert absent.value.code == "mirror-missing" and not path(missing).exists()
        assert calls == []
    finally:
        fixture.close()


def test_adoption_runs_the_callers_creation_admission_and_a_re_vouch_replaces_the_digest(
    tmp_path: Path,
) -> None:
    """Failure mode: adoption publishes without the caller's write-grade creation
    admission, or a vouch made stale by a later edit can never be replaced, leaving the
    entry unindexable without touching the user's file."""
    from biblios.core import StageRefused
    from biblios.mirrors import digest

    fixture, pipeline, calls, ids, mirrors, vouched = _rebuilt(tmp_path)
    try:
        entry_id = ids[0]
        path = fixture.bound._shelf.mirror_path(entry_id)
        cas = fixture.bound.resolve(entry_id).cas_id
        fixture.bound.derive(pipeline, ids[1], operation_id=identifier())  # the import
        assert fixture.bound.grant_first_ingestion(
            entry_id,
            expected_cas_id=cas,
            operation_id=identifier(),
            expected_mirror_sha256=vouched[entry_id],
        )
        seen = []

        class Refusing:
            def authorize_creation(self, creation, entry):
                seen.append(creation.kind)
                raise StageRefused("caller-refused")

        refused = fixture.bound.derive(
            pipeline, entry_id, operation_id=identifier(), authority=Refusing()
        )
        assert (refused.outcome(entry_id).outcome, refused.outcome(entry_id).code) == (
            "unrecorded",
            "caller-refused",
        )
        assert seen == ["adopt-existing"] and path.read_bytes() == mirrors[entry_id]
        edited = mirrors[entry_id] + b"\nthe user's edit before the rebuild finished\n"
        path.write_bytes(edited)
        assert fixture.bound.grant_first_ingestion(
            entry_id,
            expected_cas_id=cas,
            operation_id=identifier(),
            expected_mirror_sha256=digest(edited),
        )
        result = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        assert result.outcome(entry_id).outcome == "searchable"
        assert path.read_bytes() == edited and calls == []
    finally:
        fixture.close()
