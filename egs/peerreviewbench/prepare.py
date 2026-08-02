#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import time
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "benchmark.lock.toml"
DEFAULT_CACHE_ROOT = HERE / ".cache"
DATASETS_SERVER = "https://datasets-server.huggingface.co"
HUGGING_FACE = "https://huggingface.co"
REQUIRED_UPSTREAM_FILES = (
    "LICENSE",
    "peerreview_bench/load_data.py",
    "peerreview_bench/evaluation/evaluate_recall.py",
    "peerreview_bench/evaluation/evaluate_precision.py",
    "peerreview_bench/evaluation/parse_review.py",
    "peerreview_bench/evaluation/build_rubric.py",
    "peerreview_bench/evaluation/judges/model_config.py",
    "peerreview_bench/evaluation/judges/precision_prompts.py",
    "peerreview_bench/evaluation/judges/similarity_llm.py",
    "peerreview_bench/evaluation/judges/similarity_prompts.py",
)


class BenchmarkError(RuntimeError):
    pass


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def safe_relative_path(raw: str) -> Path:
    if not raw or "\\" in raw:
        raise BenchmarkError(f"Unsafe dataset path: {raw!r}")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise BenchmarkError(f"Unsafe dataset path: {raw!r}")
    return Path(*relative.parts)


def safe_cache_directory(cache_root: Path, *parts: str) -> Path:
    if cache_root.is_symlink():
        raise BenchmarkError(f"Benchmark cache root must not be a symlink: {cache_root}")
    if cache_root.exists() and not cache_root.is_dir():
        raise BenchmarkError(f"Benchmark cache root is not a directory: {cache_root}")
    candidate = cache_root.joinpath(*parts)
    try:
        relative = candidate.relative_to(cache_root)
    except ValueError as exc:
        raise BenchmarkError(f"Benchmark cache path escapes its root: {candidate}") from exc
    if ".." in relative.parts:
        raise BenchmarkError(f"Benchmark cache path escapes its root: {candidate}")
    current = cache_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise BenchmarkError(f"Benchmark cache directory must not be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise BenchmarkError(f"Benchmark cache path is not a directory: {current}")
    try:
        candidate.resolve(strict=False).relative_to(cache_root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise BenchmarkError(f"Benchmark cache path escapes its root: {candidate}") from exc
    return candidate


class DatasetClient:
    def __init__(self, dataset_id: str, revision: str) -> None:
        self.dataset_id = dataset_id
        self.revision = revision

    def current_revision(self) -> str:
        dataset_path = quote(self.dataset_id, safe="/")
        payload, _ = self._request_json(f"{HUGGING_FACE}/api/datasets/{dataset_path}/revision/main")
        revision = str(payload.get("sha", ""))
        if not revision:
            raise BenchmarkError("Hugging Face dataset metadata did not contain a revision")
        return revision

    def assert_current_revision(self) -> None:
        current = self.current_revision()
        if current != self.revision:
            raise BenchmarkError(
                f"Dataset HEAD drifted: expected {self.revision}, found {current}. "
                "Update benchmark.lock.toml and review the dataset before continuing."
            )

    def reviewer_rows(self, config: str, split: str) -> list[dict[str, Any]]:
        payload = self._dataset_request(
            "/rows",
            {
                "dataset": self.dataset_id,
                "config": config,
                "split": split,
                "offset": 0,
                "length": 100,
            },
        )
        rows = payload.get("rows", [])
        if payload.get("partial"):
            raise BenchmarkError("Hugging Face returned a partial reviewer dataset")
        if len(rows) != payload.get("num_rows_total"):
            raise BenchmarkError("The reviewer config no longer fits in one verified response")
        for entry in rows:
            if entry.get("truncated_cells"):
                raise BenchmarkError("Hugging Face truncated a reviewer row")
        return [dict(entry["row"]) for entry in rows]

    def iter_blob_batches(
        self,
        refs: Iterable[dict[str, Any]],
        config: str,
        split: str,
    ) -> Iterator[dict[str, bytes]]:
        unique: dict[str, dict[str, Any]] = {}
        for ref in refs:
            content_hash = str(ref.get("content_hash", ""))
            if len(content_hash) != 64:
                raise BenchmarkError(f"Invalid content hash in file_refs: {content_hash!r}")
            existing = unique.get(content_hash)
            identity = {
                "content_hash": content_hash,
                "size_bytes": int(ref.get("size_bytes", -1)),
                "is_text": bool(ref.get("is_text")),
            }
            if existing is not None and existing != identity:
                raise BenchmarkError(f"Inconsistent metadata for blob {content_hash}")
            unique[content_hash] = identity

        for batch in _blob_batches(unique.values()):
            predicates = [f'"content_hash"=\'{item["content_hash"]}\'' for item in batch]
            payload = self._dataset_request(
                "/filter",
                {
                    "dataset": self.dataset_id,
                    "config": config,
                    "split": split,
                    "where": " OR ".join(predicates),
                    "offset": 0,
                    "length": len(batch),
                },
            )
            if payload.get("partial"):
                raise BenchmarkError("Hugging Face returned a partial blob query")
            expected = {str(item["content_hash"]): item for item in batch}
            decoded: dict[str, bytes] = {}
            for entry in payload.get("rows", []):
                if entry.get("truncated_cells"):
                    raise BenchmarkError("Hugging Face truncated a submitted_papers blob")
                row = entry["row"]
                content_hash = str(row.get("content_hash", ""))
                if content_hash not in expected or content_hash in decoded:
                    raise BenchmarkError(f"Unexpected submitted_papers blob: {content_hash}")
                try:
                    content = base64.b64decode(row["content_bytes"], validate=True)
                except (KeyError, ValueError) as exc:
                    raise BenchmarkError(f"Invalid base64 content for blob {content_hash}") from exc
                metadata = expected[content_hash]
                _validate_blob(content, metadata)
                if int(row.get("size_bytes", -1)) != metadata["size_bytes"]:
                    raise BenchmarkError(f"Dataset size metadata disagrees for blob {content_hash}")
                if bool(row.get("is_text")) != metadata["is_text"]:
                    raise BenchmarkError(f"Dataset text metadata disagrees for blob {content_hash}")
                decoded[content_hash] = content
            missing = sorted(set(expected) - set(decoded))
            if missing:
                raise BenchmarkError(f"Missing submitted_papers blobs: {', '.join(missing)}")
            yield decoded

    def _dataset_request(self, endpoint: str, parameters: dict[str, Any]) -> dict[str, Any]:
        payload, headers = self._request_json(f"{DATASETS_SERVER}{endpoint}?{urlencode(parameters)}")
        response_revision = headers.get("x-revision", "")
        if response_revision != self.revision:
            raise BenchmarkError(
                f"Dataset viewer revision drifted: expected {self.revision}, found {response_revision or 'none'}"
            )
        return payload

    @staticmethod
    def _request_json(url: str) -> tuple[dict[str, Any], dict[str, str]]:
        request = Request(url, headers={"User-Agent": "scriptorium-peerreviewbench/1"})
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                with urlopen(request, timeout=180) as response:
                    payload = json.loads(response.read())
                    headers = {key.lower(): value for key, value in response.headers.items()}
                if isinstance(payload, dict) and "error" in payload:
                    raise BenchmarkError(str(payload["error"]))
                return payload, headers
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = BenchmarkError(f"HTTP {exc.code}: {body}")
                retryable = exc.code in {429, 500, 502, 503, 504}
            except (URLError, TimeoutError, json.JSONDecodeError, BenchmarkError) as exc:
                last_error = exc
                retryable = isinstance(exc, (URLError, TimeoutError)) or "index is loading" in str(exc)
            if not retryable or attempt == 4:
                break
            time.sleep(min(2**attempt, 8))
        raise BenchmarkError(f"Cannot read {url}: {last_error}") from last_error


def _blob_batches(
    metadata: Iterable[dict[str, Any]],
    max_count: int = 30,
    max_bytes: int = 8 * 1024 * 1024,
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    batch_bytes = 0
    for item in metadata:
        size = int(item["size_bytes"])
        if size < 0:
            raise BenchmarkError(f"Invalid blob size for {item['content_hash']}")
        if batch and (len(batch) >= max_count or batch_bytes + size > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += size
    if batch:
        yield batch


def _validate_blob(content: bytes, metadata: dict[str, Any]) -> None:
    content_hash = str(metadata["content_hash"])
    if len(content) != int(metadata["size_bytes"]):
        raise BenchmarkError(f"Blob size mismatch for {content_hash}")
    if sha256(content).hexdigest() != content_hash:
        raise BenchmarkError(f"Blob SHA256 mismatch for {content_hash}")


def expected_paper_manifest(row: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any]:
    paper_id = int(row["paper_id"])
    by_path: dict[str, dict[str, Any]] = {}
    for raw_ref in row.get("file_refs") or []:
        relative = safe_relative_path(str(raw_ref.get("path", ""))).as_posix()
        ref = {
            "path": relative,
            "content_hash": str(raw_ref.get("content_hash", "")),
            "size_bytes": int(raw_ref.get("size_bytes", -1)),
            "is_text": bool(raw_ref.get("is_text")),
        }
        existing = by_path.get(relative)
        if existing is not None and existing != ref:
            raise BenchmarkError(f"Conflicting file_refs for paper{paper_id}/{relative}")
        by_path[relative] = ref
    if "preprint.md" not in by_path:
        raise BenchmarkError(f"paper{paper_id} has no preprint.md")
    return {
        "dataset_id": dataset["id"],
        "dataset_revision": dataset["revision"],
        "paper_id": paper_id,
        "paper_title": str(row.get("paper_title", "")),
        "files": [by_path[path] for path in sorted(by_path)],
    }


def validate_prepared_paper(
    paper_dir: Path,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = paper_dir / "manifest.json"
    if paper_dir.is_symlink() or manifest_path.is_symlink():
        raise BenchmarkError(f"Prepared cache contains an unsafe symlink: {paper_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Invalid prepared-paper manifest: {manifest_path}") from exc
    if expected is not None and manifest != expected:
        raise BenchmarkError(f"Prepared cache manifest does not match: {paper_dir}")
    preprint = paper_dir / "preprint"
    if not preprint.is_dir() or preprint.is_symlink():
        raise BenchmarkError(f"Prepared preprint directory is missing or unsafe: {preprint}")
    expected_paths: set[str] = set()
    for ref in manifest.get("files", []):
        relative = safe_relative_path(str(ref.get("path", "")))
        expected_paths.add(relative.as_posix())
        path = preprint / relative
        if not path.is_file() or path.is_symlink():
            raise BenchmarkError(f"Prepared file is missing or unsafe: {path}")
        if path.stat().st_size != int(ref["size_bytes"]) or file_digest(path) != ref["content_hash"]:
            raise BenchmarkError(f"Prepared file failed verification: {path}")
    actual_paths = set()
    for path in preprint.rglob("*"):
        if path.is_symlink():
            raise BenchmarkError(f"Prepared cache contains an unsafe symlink: {path}")
        if path.is_file():
            actual_paths.add(path.relative_to(preprint).as_posix())
    if actual_paths != expected_paths:
        raise BenchmarkError(f"Prepared cache contains unexpected or missing files: {paper_dir}")
    if (paper_dir / "reviews").exists():
        raise BenchmarkError(f"Prepared cache must not contain review files: {paper_dir}")
    return manifest


def prepare_paper(
    row: dict[str, Any],
    dataset: dict[str, Any],
    client: DatasetClient,
    dataset_root: Path,
) -> tuple[Path, bool]:
    expected = expected_paper_manifest(row, dataset)
    target = dataset_root / f"paper{expected['paper_id']}"
    if target.exists():
        validate_prepared_paper(target, expected)
        return target, True

    temporary = dataset_root / f".paper{expected['paper_id']}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True)
    refs_by_hash: dict[str, list[dict[str, Any]]] = {}
    for ref in expected["files"]:
        refs_by_hash.setdefault(ref["content_hash"], []).append(ref)
    try:
        for blobs in client.iter_blob_batches(expected["files"], dataset["blob_config"], dataset["split"]):
            for content_hash, content in blobs.items():
                for ref in refs_by_hash[content_hash]:
                    output = temporary / "preprint" / safe_relative_path(ref["path"])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(content)
        (temporary / "manifest.json").write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        validate_prepared_paper(temporary, expected)
        temporary.replace(target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target, False


def prepare_upstream(upstream: dict[str, Any], cache_root: Path) -> Path:
    commit = str(upstream["commit"])
    downloads = safe_cache_directory(cache_root, "downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"cmu-paper-reviewer-{commit}.tar.gz"
    if archive.exists():
        actual_digest = file_digest(archive)
        if actual_digest != upstream["archive_sha256"]:
            raise BenchmarkError(f"Upstream archive hash mismatch: {archive}")
    else:
        temporary_archive = archive.with_name(f".{archive.name}.{uuid.uuid4().hex}.tmp")
        try:
            _download_file(str(upstream["archive_url"]), temporary_archive)
            actual_digest = file_digest(temporary_archive)
            if actual_digest != upstream["archive_sha256"]:
                raise BenchmarkError(
                    f"Upstream archive hash mismatch: expected {upstream['archive_sha256']}, found {actual_digest}"
                )
            temporary_archive.replace(archive)
        finally:
            temporary_archive.unlink(missing_ok=True)

    archive_files = _archive_file_manifest(archive)
    target = safe_cache_directory(cache_root, "upstream", commit)
    expected_manifest = {
        "repository": upstream["repository"],
        "commit": commit,
        "archive_sha256": upstream["archive_sha256"],
        "files": archive_files,
    }
    if target.exists():
        _validate_upstream(target, expected_manifest)
        return target

    temporary = target.parent / f".{commit}.{uuid.uuid4().hex}.tmp"
    extraction = temporary / "extracted"
    extraction.mkdir(parents=True)
    try:
        with tarfile.open(archive) as handle:
            members = handle.getmembers()
            for member in members:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise BenchmarkError(f"Unsafe path in upstream archive: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise BenchmarkError(f"Unsupported entry in upstream archive: {member.name}")
            if sys.version_info >= (3, 12):
                handle.extractall(extraction, filter="data")
            else:  # pragma: no cover - Python 3.10 and 3.11
                handle.extractall(extraction)
        roots = [path for path in extraction.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise BenchmarkError("Upstream archive did not contain exactly one source directory")
        source = roots[0]
        (source / ".scriptorium-benchmark.json").write_text(
            json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        _validate_upstream(target, expected_manifest)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return target


def _archive_file_manifest(archive: Path) -> list[dict[str, Any]]:
    files = []
    roots = set()
    seen = set()
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise BenchmarkError(f"Unsafe path in upstream archive: {member.name}")
            roots.add(path.parts[0])
            if member.issym() or member.islnk() or member.isdev():
                raise BenchmarkError(f"Unsupported entry in upstream archive: {member.name}")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) == 1:
                raise BenchmarkError(f"Unsupported entry in upstream archive: {member.name}")
            relative = safe_relative_path(PurePosixPath(*path.parts[1:]).as_posix()).as_posix()
            if relative in seen:
                raise BenchmarkError(f"Duplicate path in upstream archive: {relative}")
            seen.add(relative)
            source = handle.extractfile(member)
            if source is None:
                raise BenchmarkError(f"Cannot read upstream archive entry: {member.name}")
            digest = sha256()
            size = 0
            with source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if size != member.size:
                raise BenchmarkError(f"Upstream archive entry size mismatch: {member.name}")
            files.append({"path": relative, "size_bytes": size, "sha256": digest.hexdigest()})
    if len(roots) != 1:
        raise BenchmarkError("Upstream archive did not contain exactly one source directory")
    return sorted(files, key=lambda item: item["path"])


def _validate_upstream(path: Path, expected_manifest: dict[str, Any]) -> None:
    if path.is_symlink():
        raise BenchmarkError(f"Invalid upstream cache: {path}")
    marker = path / ".scriptorium-benchmark.json"
    if marker.is_symlink():
        raise BenchmarkError(f"Invalid upstream cache: {path}")
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Invalid upstream cache: {path}") from exc
    if manifest != expected_manifest:
        raise BenchmarkError(f"Upstream cache manifest does not match: {path}")
    expected_files = {item["path"]: item for item in expected_manifest["files"]}
    actual_files = {}
    for item in path.rglob("*"):
        if item.is_symlink():
            raise BenchmarkError(f"Upstream cache contains a symlink: {item}")
        if not item.is_file() or item == marker:
            continue
        relative = item.relative_to(path).as_posix()
        actual_files[relative] = item
    if set(actual_files) != set(expected_files):
        raise BenchmarkError(f"Upstream cache contains unexpected or missing files: {path}")
    for relative, expected in expected_files.items():
        item = actual_files[relative]
        if item.stat().st_size != expected["size_bytes"] or file_digest(item) != expected["sha256"]:
            raise BenchmarkError(f"Upstream cache file failed verification: {item}")
    for relative in REQUIRED_UPSTREAM_FILES:
        if not (path / relative).is_file():
            raise BenchmarkError(f"Upstream cache is missing {relative}")


def _download_file(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "scriptorium-peerreviewbench/1"})
    try:
        with urlopen(request, timeout=180) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, URLError, TimeoutError) as exc:
        raise BenchmarkError(f"Cannot download {url}: {exc}") from exc


def select_rows(
    rows: list[dict[str, Any]],
    *,
    all_papers: bool,
    paper_ids: list[int] | None,
    smoke_papers: int,
) -> list[dict[str, Any]]:
    by_id = {int(row["paper_id"]): row for row in rows}
    if all_papers:
        selected_ids = sorted(by_id)
    elif paper_ids:
        selected_ids = list(dict.fromkeys(paper_ids))
    else:
        selected_ids = sorted(by_id)[:smoke_papers]
    missing = [paper_id for paper_id in selected_ids if paper_id not in by_id]
    if missing:
        raise BenchmarkError(f"Unknown paper IDs: {', '.join(map(str, missing))}")
    return [by_id[paper_id] for paper_id in selected_ids]


def prepare(
    *,
    all_papers: bool = False,
    paper_ids: list[int] | None = None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    client: DatasetClient | None = None,
) -> dict[str, Any]:
    lock = load_lock()
    dataset = lock["dataset"]
    upstream_path = prepare_upstream(lock["upstream"], cache_root)
    client = client or DatasetClient(dataset["id"], dataset["revision"])
    client.assert_current_revision()
    rows = client.reviewer_rows(dataset["reviewer_config"], dataset["split"])
    if len(rows) != int(dataset["papers"]):
        raise BenchmarkError(f"Expected {dataset['papers']} reviewer papers, found {len(rows)}")
    selected = select_rows(
        rows,
        all_papers=all_papers,
        paper_ids=paper_ids,
        smoke_papers=int(lock["defaults"]["smoke_papers"]),
    )
    dataset_root = safe_cache_directory(cache_root, "dataset", str(dataset["revision"]))
    dataset_root.mkdir(parents=True, exist_ok=True)
    prepared = []
    for index, row in enumerate(selected, 1):
        paper_id = int(row["paper_id"])
        print(f"[{index}/{len(selected)}] preparing paper{paper_id}", flush=True)
        path, reused = prepare_paper(row, dataset, client, dataset_root)
        prepared.append({"paper_id": paper_id, "path": str(path), "reused": reused})
    client.assert_current_revision()
    return {
        "dataset_id": dataset["id"],
        "dataset_revision": dataset["revision"],
        "upstream_path": str(upstream_path),
        "paper_ids": [item["paper_id"] for item in prepared],
        "papers": prepared,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare pinned PeerReviewBench Markdown workspaces")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Prepare all locked benchmark papers")
    selection.add_argument("--paper-id", action="append", type=int, dest="paper_ids", help="Prepare one paper ID")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare(all_papers=args.all, paper_ids=args.paper_ids)
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
