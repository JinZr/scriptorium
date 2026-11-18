from dataclasses import replace
import hashlib

import pytest

from scriptorium.artifacts import ArtifactCorruptionError, ArtifactNotFoundError, ArtifactStore


def test_content_addressed_put_is_idempotent(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text("manuscript")
    second = store.put_text("manuscript")

    assert first.digest == hashlib.sha256(b"manuscript").hexdigest()
    assert first.relative_path == second.relative_path
    assert store.get_bytes(first.digest) == b"manuscript"
    assert store.verify(first.digest)


def test_put_file_and_atomic_materialize(tmp_path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fixture")
    store = ArtifactStore(tmp_path / "artifacts")

    artifact = store.put_file(source, media_type="application/pdf")
    destination = tmp_path / "run" / "manuscript.pdf"

    assert store.materialize(artifact.digest, destination) == destination
    assert destination.read_bytes() == source.read_bytes()
    assert not list(destination.parent.glob(".artifact-*"))


def test_corrupt_existing_artifact_is_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"original")
    store.path_for(artifact.digest).write_bytes(b"corrupt!")

    with pytest.raises(ArtifactCorruptionError):
        store.put_bytes(b"original")
    with pytest.raises(ArtifactCorruptionError):
        store.get_bytes(artifact.digest)


def test_unknown_and_invalid_digest_are_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    unknown = "0" * 64

    with pytest.raises(ArtifactNotFoundError):
        store.get_bytes(unknown)
    with pytest.raises(ValueError):
        store.path_for("../not-a-digest")


def test_the_first_metadata_record_of_a_digest_is_kept(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("evidence", "text/plain")

    assert store.read_record(artifact.digest) is None
    first = store.record(artifact)
    later = store.record(replace(artifact, media_type="application/json", created_at="later"))

    assert first == later == artifact
    assert store.read_record(artifact.digest) == artifact
    assert not list(store.record_path(artifact.digest).parent.glob(".artifact-*"))
    store.record_path(artifact.digest).write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactCorruptionError, match="artifact record is corrupt"):
        store.read_record(artifact.digest)


def test_a_metadata_record_is_durable_before_it_is_returned(tmp_path, monkeypatch) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("evidence", "text/plain")
    synced = []
    monkeypatch.setattr(ArtifactStore, "_sync_directory", staticmethod(synced.append))

    store.record(artifact)
    store.record(artifact)

    assert synced == [store.record_path(artifact.digest).parent] * 2
