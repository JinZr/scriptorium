from hashlib import sha256
import io
import json
from pathlib import Path
import tarfile

import pytest

from egs.peerreviewbench.prepare import (
    REQUIRED_UPSTREAM_FILES,
    BenchmarkError,
    DatasetClient,
    _validate_blob,
    prepare_paper,
    prepare_upstream,
    safe_cache_directory,
    safe_relative_path,
    select_rows,
    validate_prepared_paper,
)

DATASET = {
    "id": "prometheus-eval/peerreview-bench",
    "revision": "locked-revision",
    "blob_config": "submitted_papers",
    "split": "eval",
}


def _file_ref(path: str, content: bytes, *, is_text: bool = True) -> dict[str, object]:
    return {
        "path": path,
        "content_hash": sha256(content).hexdigest(),
        "size_bytes": len(content),
        "is_text": is_text,
    }


def _paper_row(paper_id: int = 7) -> tuple[dict[str, object], dict[str, bytes]]:
    contents = {
        "preprint.md": b"# A benchmark paper\n",
        "figures/result.png": b"\x89PNG\r\n\x1a\nfixture",
        "supplement/code.py": b"print('fixture')\n",
    }
    return (
        {
            "paper_id": paper_id,
            "paper_title": "A benchmark paper",
            "file_refs": [
                _file_ref(path, content, is_text=not path.endswith(".png")) for path, content in contents.items()
            ],
        },
        contents,
    )


class FakeDatasetClient:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.calls = 0

    def iter_blob_batches(
        self,
        refs: list[dict[str, object]],
        config: str,
        split: str,
    ):
        assert config == DATASET["blob_config"]
        assert split == DATASET["split"]
        self.calls += 1
        yield {str(ref["content_hash"]): self.contents[str(ref["path"])] for ref in refs}


def _write_upstream_archive(path: Path, commit: str) -> bytes:
    with tarfile.open(path, "w:gz") as archive:
        for relative in REQUIRED_UPSTREAM_FILES:
            content = f"{relative}\n".encode()
            info = tarfile.TarInfo(f"cmu-paper-reviewer-{commit}/{relative}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path.read_bytes()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/absolute/preprint.md",
        "../preprint.md",
        "figures/../../preprint.md",
        r"figures\result.png",
    ],
)
def test_safe_relative_path_rejects_unsafe_dataset_paths(raw: str) -> None:
    with pytest.raises(BenchmarkError, match="Unsafe dataset path"):
        safe_relative_path(raw)


def test_safe_relative_path_accepts_nested_manuscript_path() -> None:
    assert safe_relative_path("supplement/code.py") == Path("supplement/code.py")


@pytest.mark.parametrize("path", [("downloads",), ("upstream", "locked-commit"), ("dataset", "locked-revision")])
def test_safe_cache_directory_rejects_symlinked_intermediate(tmp_path: Path, path: tuple[str, ...]) -> None:
    cache_root = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache_root.mkdir()
    outside.mkdir()
    (cache_root / path[0]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="must not be a symlink"):
        safe_cache_directory(cache_root, *path)


def test_safe_cache_directory_rejects_path_escape(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"

    with pytest.raises(BenchmarkError, match="escapes its root"):
        safe_cache_directory(cache_root, "dataset", "..", "..", "outside")


def test_validate_blob_checks_size_and_sha256() -> None:
    content = b"verified blob"
    metadata = _file_ref("preprint.md", content)

    _validate_blob(content, metadata)

    with pytest.raises(BenchmarkError, match="size mismatch"):
        _validate_blob(content, {**metadata, "size_bytes": len(content) + 1})
    with pytest.raises(BenchmarkError, match="SHA256 mismatch"):
        _validate_blob(content, {**metadata, "content_hash": "0" * 64})


def test_dataset_client_fails_closed_when_head_revision_drifts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DatasetClient(DATASET["id"], DATASET["revision"])
    monkeypatch.setattr(client, "current_revision", lambda: "different-revision")

    with pytest.raises(BenchmarkError, match="Dataset HEAD drifted"):
        client.assert_current_revision()


def test_dataset_client_rejects_missing_blob_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _file_ref("preprint.md", b"paper")
    second = _file_ref("figure.png", b"figure", is_text=False)
    client = DatasetClient(DATASET["id"], DATASET["revision"])
    monkeypatch.setattr(
        client,
        "_dataset_request",
        lambda endpoint, parameters: {
            "partial": False,
            "rows": [
                {
                    "row": {
                        "content_hash": first["content_hash"],
                        "content_bytes": "cGFwZXI=",
                        "size_bytes": first["size_bytes"],
                        "is_text": first["is_text"],
                    }
                }
            ],
        },
    )

    with pytest.raises(BenchmarkError, match=f"Missing submitted_papers blobs: {second['content_hash']}"):
        list(client.iter_blob_batches([first, second], DATASET["blob_config"], DATASET["split"]))


def test_prepare_paper_is_atomic_and_reuses_verified_cache(tmp_path: Path) -> None:
    row, contents = _paper_row()
    client = FakeDatasetClient(contents)

    paper_dir, reused = prepare_paper(row, DATASET, client, tmp_path)

    assert reused is False
    assert client.calls == 1
    assert (paper_dir / "preprint" / "preprint.md").read_bytes() == contents["preprint.md"]
    assert (paper_dir / "preprint" / "figures" / "result.png").read_bytes() == contents["figures/result.png"]
    validate_prepared_paper(paper_dir)

    cached_dir, reused = prepare_paper(row, DATASET, client, tmp_path)

    assert cached_dir == paper_dir
    assert reused is True
    assert client.calls == 1


def test_prepare_paper_ignores_gold_review_and_rubric_row_fields(tmp_path: Path) -> None:
    row, contents = _paper_row()
    row["human_reviews"] = [{"text": "gold review must not be copied"}]
    row["rubric"] = [{"text": "gold rubric must not be copied"}]

    paper_dir, _ = prepare_paper(row, DATASET, FakeDatasetClient(contents), tmp_path)
    manifest = validate_prepared_paper(paper_dir)

    assert {item["path"] for item in manifest["files"]} == set(contents)
    assert not any("gold review" in path.read_text(errors="ignore") for path in paper_dir.rglob("*") if path.is_file())


def test_prepare_paper_removes_partial_directory_after_blob_failure(tmp_path: Path) -> None:
    row, contents = _paper_row()
    contents["preprint.md"] = b"tampered"

    with pytest.raises(BenchmarkError, match="failed verification"):
        prepare_paper(row, DATASET, FakeDatasetClient(contents), tmp_path)

    assert not (tmp_path / "paper7").exists()
    assert not list(tmp_path.glob(".paper7.*.tmp"))


@pytest.mark.parametrize("leak", ["reviews/human.json", "preprint/unexpected.txt"])
def test_prepared_cache_rejects_review_leakage_and_extra_files(
    tmp_path: Path,
    leak: str,
) -> None:
    row, contents = _paper_row()
    paper_dir, _ = prepare_paper(row, DATASET, FakeDatasetClient(contents), tmp_path)
    leaked_file = paper_dir / leak
    leaked_file.parent.mkdir(parents=True, exist_ok=True)
    leaked_file.write_text("must not be present", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="review files|unexpected or missing files"):
        validate_prepared_paper(paper_dir)


def test_prepared_cache_rejects_symlinked_content(tmp_path: Path) -> None:
    row, contents = _paper_row()
    paper_dir, _ = prepare_paper(row, DATASET, FakeDatasetClient(contents), tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (paper_dir / "preprint" / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="unsafe symlink"):
        validate_prepared_paper(paper_dir)


def test_prepare_upstream_rejects_mismatched_cached_archive(tmp_path: Path) -> None:
    commit = "a" * 40
    archive = tmp_path / "downloads" / f"cmu-paper-reviewer-{commit}.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"not the locked archive")
    upstream = {
        "repository": "https://example.invalid/repository",
        "commit": commit,
        "archive_url": "https://example.invalid/archive.tar.gz",
        "archive_sha256": "0" * 64,
    }

    with pytest.raises(BenchmarkError, match="archive hash mismatch"):
        prepare_upstream(upstream, tmp_path)


def test_prepare_upstream_rejects_mismatched_cache_manifest(tmp_path: Path) -> None:
    commit = "b" * 40
    archive = tmp_path / "downloads" / f"cmu-paper-reviewer-{commit}.tar.gz"
    archive.parent.mkdir()
    archive_content = _write_upstream_archive(archive, commit)
    upstream = {
        "repository": "https://example.invalid/repository",
        "commit": commit,
        "archive_url": "https://example.invalid/archive.tar.gz",
        "archive_sha256": sha256(archive_content).hexdigest(),
    }
    cached = tmp_path / "upstream" / commit
    cached.mkdir(parents=True)
    (cached / ".scriptorium-benchmark.json").write_text(
        json.dumps({"commit": "different"}),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="cache manifest does not match"):
        prepare_upstream(upstream, tmp_path)


def test_prepare_upstream_rejects_tampered_extracted_source(tmp_path: Path) -> None:
    commit = "c" * 40
    archive = tmp_path / "downloads" / f"cmu-paper-reviewer-{commit}.tar.gz"
    archive.parent.mkdir()
    archive_content = _write_upstream_archive(archive, commit)
    upstream = {
        "repository": "https://example.invalid/repository",
        "commit": commit,
        "archive_url": "https://example.invalid/archive.tar.gz",
        "archive_sha256": sha256(archive_content).hexdigest(),
    }
    cached = prepare_upstream(upstream, tmp_path)
    (cached / REQUIRED_UPSTREAM_FILES[0]).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="file failed verification"):
        prepare_upstream(upstream, tmp_path)


def test_select_rows_supports_smoke_explicit_and_all_modes() -> None:
    rows = [{"paper_id": paper_id} for paper_id in (9, 2, 5)]

    assert [row["paper_id"] for row in select_rows(rows, all_papers=False, paper_ids=None, smoke_papers=2)] == [2, 5]
    assert [row["paper_id"] for row in select_rows(rows, all_papers=False, paper_ids=[9, 2, 9], smoke_papers=2)] == [
        9,
        2,
    ]
    assert [row["paper_id"] for row in select_rows(rows, all_papers=True, paper_ids=None, smoke_papers=2)] == [2, 5, 9]

    with pytest.raises(BenchmarkError, match="Unknown paper IDs: 3"):
        select_rows(rows, all_papers=False, paper_ids=[3], smoke_papers=2)
