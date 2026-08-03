from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Iterable

import fitz

from .config import ManuscriptConfig
from .errors import InfrastructureError, StateError
from .schemas import (
    CompiledPdfAnchor,
    CompiledPdfPageRecord,
    EvidenceAnchorContract,
    EvidenceAnchorMap,
    ExactEdit,
    SourceAnchorRecord,
    evidence_anchor_contract_digest,
)

INPUT_PATTERN = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
BIB_PATTERN = re.compile(r"\\bibliography\s*\{([^}]+)\}")
ADDBIB_PATTERN = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^}]+)\}")
GRAPHICS_PATTERN = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]+)\}")
GRAPHICS_EXTENSIONS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps")
NON_TEXT_ANCHOR_EXTENSIONS = frozenset(
    {
        ".bmp",
        ".eps",
        ".gif",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".png",
        ".ps",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)


@dataclass(frozen=True)
class FrozenRevision:
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True)
class SourceFile:
    path: str
    digest: str
    lines: int


@dataclass(frozen=True)
class BuildResult:
    pdf_path: Path
    log: str


@dataclass(frozen=True)
class ManuscriptBundle:
    workspace: Path
    sources: tuple[SourceFile, ...]
    pdf_pages: int
    anchor_map: EvidenceAnchorMap | None = None


class ManuscriptManager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()

    def resolve_revision(self, revision: str) -> FrozenRevision:
        commit = self._git_object_id(self._git("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"))
        # Some Apple Git releases echo --end-of-options as a revision unless --verify is present.
        tree = self._git_object_id(self._git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"))
        return FrozenRevision(commit, tree)

    @staticmethod
    def _git_object_id(output: str) -> str:
        lines = output.splitlines()
        if len(lines) != 1 or re.fullmatch(r"[0-9a-f]+", lines[0]) is None:
            raise InfrastructureError("git returned an invalid object ID")
        return lines[0]

    def create_snapshot(self, revision: FrozenRevision, destination: Path) -> None:
        if destination.exists() and any(destination.iterdir()):
            raise StateError(f"Snapshot destination is not empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "-C", str(self.repo), "archive", "--format=tar", revision.commit_sha],
            check=False,
            capture_output=True,
        )
        if archive.returncode != 0:
            raise InfrastructureError(archive.stderr.decode("utf-8", errors="replace").strip())
        archive_path = destination.parent / f".{destination.name}.tar"
        archive_path.write_bytes(archive.stdout)
        try:
            with tarfile.open(archive_path) as handle:
                for member in handle.getmembers():
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise InfrastructureError(f"Unsafe path in Git archive: {member.name}")
                if sys.version_info >= (3, 12):
                    handle.extractall(destination, filter="data")
                else:  # pragma: no cover - Python 3.10 and 3.11 compatibility
                    handle.extractall(destination)
        finally:
            archive_path.unlink(missing_ok=True)

    def scan_sources(self, snapshot: Path, main: str) -> tuple[SourceFile, ...]:
        root = snapshot.resolve()
        pending = [Path(main)]
        bibliography_fallback = Path(main).with_suffix(".bbl")
        included: set[Path] = set()
        while pending:
            relative = pending.pop()
            relative = self._normalized_relative(root, relative)
            if relative in included:
                continue
            path = root / relative
            if not path.is_file():
                raise InfrastructureError(f"Referenced manuscript file is missing: {relative}")
            included.add(relative)
            if path.suffix.lower() != ".tex":
                continue
            text = self._strip_comments(path.read_text(encoding="utf-8"))
            base = relative.parent
            for raw in INPUT_PATTERN.findall(text):
                dependency = Path(raw.strip())
                if not dependency.suffix:
                    dependency = dependency.with_suffix(".tex")
                pending.append(self._resolve_dependency(root, base, dependency))
            for raw_group in BIB_PATTERN.findall(text):
                for raw in raw_group.split(","):
                    dependency = Path(raw.strip())
                    if not dependency.suffix:
                        dependency = dependency.with_suffix(".bib")
                    pending.append(
                        self._resolve_dependency(
                            root,
                            base,
                            dependency,
                            fallback=bibliography_fallback,
                        )
                    )
            for raw in ADDBIB_PATTERN.findall(text):
                dependency = Path(raw.strip())
                pending.append(
                    self._resolve_dependency(
                        root,
                        base,
                        dependency,
                        fallback=bibliography_fallback,
                    )
                )
            for raw in GRAPHICS_PATTERN.findall(text):
                pending.append(
                    self._resolve_dependency(
                        root,
                        base,
                        Path(raw.strip()),
                        GRAPHICS_EXTENSIONS,
                    )
                )
        sources = []
        for relative in sorted(included):
            data = (root / relative).read_bytes()
            line_count = len(data.decode("utf-8", errors="replace").splitlines())
            sources.append(SourceFile(relative.as_posix(), sha256(data).hexdigest(), line_count))
        return tuple(sources)

    def build(self, workspace: Path, manuscript: ManuscriptConfig) -> BuildResult:
        engine_option = {
            "pdflatex": "-pdf",
            "xelatex": "-xelatex",
            "lualatex": "-lualatex",
        }[manuscript.engine]
        command = [
            "latexmk",
            "-norc",
            engine_option,
            "-interaction=nonstopmode",
            "-halt-on-error",
            manuscript.main,
        ]
        try:
            result = subprocess.run(command, cwd=workspace, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise InfrastructureError("latexmk is required but was not found") from exc
        log = f"{result.stdout}\n{result.stderr}".strip()
        if result.returncode != 0:
            raise InfrastructureError(f"LaTeX build failed:\n{log}")
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        if not pdf_path.is_file():
            raise InfrastructureError(f"LaTeX build did not create {pdf_path.name}")
        return BuildResult(pdf_path=pdf_path, log=log)

    def create_bundle(
        self,
        snapshot: Path,
        destination: Path,
        revision: FrozenRevision,
        sources: tuple[SourceFile, ...],
        pdf_path: Path,
        anchor_contract: EvidenceAnchorContract,
    ) -> ManuscriptBundle:
        if destination.exists():
            shutil.rmtree(destination)
        source_root = destination / "sources"
        pages_root = destination / "pages"
        source_root.mkdir(parents=True)
        pages_root.mkdir()
        for source in sources:
            target = source_root / source.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / source.path, target)
        bundle_pdf = destination / "manuscript.pdf"
        shutil.copy2(pdf_path, bundle_pdf)
        document = fitz.open(bundle_pdf)
        try:
            for index, page in enumerate(document):
                page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(
                    pages_root / f"page-{index + 1:04d}.png"
                )
            page_count = document.page_count
        finally:
            document.close()
        manifest = {
            "commit_sha": revision.commit_sha,
            "tree_sha": revision.tree_sha,
            "sources": [asdict(source) for source in sources],
            "pdf_digest": sha256(bundle_pdf.read_bytes()).hexdigest(),
            "pdf_pages": page_count,
            "pages": [
                {
                    "path": page_path.relative_to(destination).as_posix(),
                    "digest": sha256(page_path.read_bytes()).hexdigest(),
                }
                for page_path in sorted(pages_root.glob("page-*.png"))
            ],
        }
        anchor_sources = []
        for source in sources:
            data = (snapshot / source.path).read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            text_anchorable = text is not None and Path(source.path).suffix.lower() not in NON_TEXT_ANCHOR_EXTENSIONS
            anchor_sources.append(
                SourceAnchorRecord(
                    source_path=source.path,
                    # Workspace paths are read locations; persisted anchors stay relative to the frozen source map.
                    read_path=(Path("sources") / source.path).as_posix(),
                    source_digest=source.digest,
                    line_count=len(text.splitlines()) if text_anchorable else None,
                    text_anchorable=text_anchorable,
                )
            )
        anchor_map = EvidenceAnchorMap(
            contract_digest=evidence_anchor_contract_digest(anchor_contract),
            sources=anchor_sources,
            compiled_pdf=CompiledPdfAnchor(
                source_path=anchor_contract.pdf_page.source_path,
                read_path=anchor_contract.pdf_page.source_path,
                page_count=page_count,
                pages=[
                    CompiledPdfPageRecord(
                        page=index,
                        read_path=page["path"],
                        page_digest=page["digest"],
                    )
                    for index, page in enumerate(manifest["pages"], start=1)
                ],
            ),
        )
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (destination / "source-map.json").write_text(
            json.dumps(anchor_map.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return ManuscriptBundle(destination, sources, page_count, anchor_map)

    def apply_edits(self, snapshot: Path, patched: Path, edits: Iterable[ExactEdit]) -> tuple[str, tuple[str, ...]]:
        if patched.exists():
            shutil.rmtree(patched)
        shutil.copytree(snapshot, patched)
        edits_by_path: dict[str, list[ExactEdit]] = {}
        for edit in edits:
            edits_by_path.setdefault(edit.path, []).append(edit)
        changed_paths: list[str] = []
        for relative, path_edits in sorted(edits_by_path.items()):
            source_path = snapshot / relative
            target_path = patched / relative
            if not source_path.is_file():
                raise StateError(f"Patch path is not in the frozen manuscript: {relative}")
            source_bytes = source_path.read_bytes()
            if sha256(source_bytes).hexdigest() != path_edits[0].source_digest:
                raise StateError(f"Source digest does not match for {relative}")
            ordered = sorted(path_edits, key=lambda item: (item.start_line, item.end_line))
            for previous, current in zip(ordered, ordered[1:]):
                if current.start_line <= previous.end_line:
                    raise StateError(f"Overlapping edits for {relative}")
            text = source_bytes.decode("utf-8")
            lines = text.splitlines(keepends=True)
            for edit in reversed(ordered):
                start = sum(len(line) for line in lines[: edit.start_line - 1])
                end = sum(len(line) for line in lines[: edit.end_line])
                existing = text[start:end]
                replacement = edit.after
                if existing != edit.before:
                    if existing.rstrip("\r\n") != edit.before:
                        raise StateError(f"Exact replacement mismatch in {relative}:{edit.start_line}-{edit.end_line}")
                    replacement += existing[len(existing.rstrip("\r\n")) :]
                text = f"{text[:start]}{replacement}{text[end:]}"
                lines = text.splitlines(keepends=True)
            target_path.write_text(text, encoding="utf-8")
            changed_paths.append(relative)
        return self.diff(snapshot, patched, changed_paths), tuple(changed_paths)

    def diff(self, before: Path, after: Path, paths: Iterable[str]) -> str:
        chunks: list[str] = []
        for relative in sorted(paths):
            old = (before / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            new = (after / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            chunks.extend(
                difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="\n")
            )
        return "".join(chunks)

    def apply_to_worktree(self, snapshot: Path, patched: Path, paths: Iterable[str]) -> tuple[str, ...]:
        changed = tuple(sorted(paths))
        for relative in changed:
            current = self.repo / relative
            base = snapshot / relative
            if (
                not current.is_file()
                or sha256(current.read_bytes()).hexdigest() != sha256(base.read_bytes()).hexdigest()
            ):
                raise StateError(f"Patch is stale because {relative} no longer matches the frozen revision")
        for relative in changed:
            current = self.repo / relative
            temporary = current.with_name(f".{current.name}.scriptorium.tmp")
            temporary.write_bytes((patched / relative).read_bytes())
            shutil.copymode(current, temporary)
            temporary.replace(current)
        return changed

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError("git is required but was not found") from exc
        if result.returncode != 0:
            raise InfrastructureError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout

    @staticmethod
    def _normalized_relative(root: Path, relative: Path) -> Path:
        candidate = (root / relative).resolve()
        try:
            normalized = candidate.relative_to(root)
        except ValueError as exc:
            raise InfrastructureError(f"Manuscript dependency leaves the snapshot: {relative}") from exc
        if ".codex" in normalized.parts or normalized.name == "AGENTS.md":
            raise InfrastructureError(f"Manuscript dependency is not allowed in an agent bundle: {relative}")
        return normalized

    @classmethod
    def _resolve_dependency(
        cls,
        root: Path,
        base: Path,
        dependency: Path,
        suffixes: tuple[str, ...] = ("",),
        *,
        fallback: Path | None = None,
    ) -> Path:
        candidates: list[Path] = []
        for parent in (base, Path()):
            for suffix in suffixes:
                candidate = Path(f"{parent / dependency}{suffix}")
                if candidate not in candidates:
                    candidates.append(candidate)
        for candidate in candidates:
            normalized = cls._normalized_relative(root, candidate)
            if (root / normalized).is_file():
                return normalized
        if fallback is not None:
            normalized = cls._normalized_relative(root, fallback)
            if (root / normalized).is_file():
                return normalized
        raise InfrastructureError(f"Referenced manuscript file is missing: {dependency}")

    @staticmethod
    def _strip_comments(text: str) -> str:
        stripped = []
        for line in text.splitlines():
            consecutive_backslashes = 0
            for index, character in enumerate(line):
                if character == "\\":
                    consecutive_backslashes += 1
                    continue
                if character == "%" and consecutive_backslashes % 2 == 0:
                    line = line[:index]
                    break
                consecutive_backslashes = 0
            stripped.append(line)
        return "\n".join(stripped)
