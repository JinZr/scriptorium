from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from typing import Iterable, Iterator

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

INPUT_PATTERN = re.compile(r"\\(?:input(?![A-Za-z@])\s*(?:\{([^{}]+)\}|([^\\\s{}%]+))|include\s*\{([^{}]+)\})")
BIB_PATTERN = re.compile(r"\\bibliography\s*\{([^}]+)\}")
ADDBIB_PATTERN = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^}]+)\}")
GRAPHICS_PATTERN = re.compile(r"\\includegraphics\*?(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}")
GRAPHICSPATH_PATTERN = re.compile(r"\\graphicspath(?![A-Za-z@])\s*(\{(?:\s*\{[^{}]*\}\s*)*\})?")
GRAPHICS_EXTENSIONS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps")
FONT_INPUT_EXTENSIONS = frozenset({".tfm", ".vf", ".pfb", ".pfa", ".otf", ".ttf", ".ttc"})
BUILD_INPUT_EXTENSIONS = FONT_INPUT_EXTENSIONS | frozenset(
    {".cls", ".sty", ".bst", ".clo", ".def", ".cfg", ".fd", ".enc", ".map", ".bbx", ".cbx", ".lbx"}
)
REVIEW_INPUT_EXTENSIONS = frozenset(
    {".tex", ".ltx", ".bib", ".bbl", ".txt", ".csv", ".tsv", ".dat", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"}
)
GENERATED_INPUT_EXTENSIONS = frozenset({".aux", ".toc", ".out", ".lof", ".lot", ".nav", ".snm", ".vrb", ".bcf"})
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
class CompilerInput:
    path: str
    digest: str
    kind: str


@dataclass(frozen=True)
class TexmfRoot:
    path: Path
    distribution: bool


@dataclass(frozen=True)
class BuildDerivation:
    path: str
    digest: str
    tool: str
    inputs: tuple[str, ...]


@dataclass(frozen=True)
class BuildResult:
    pdf_path: Path
    log: str
    compiler_inputs: tuple[CompilerInput, ...] | None = None


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
        # Pause each parent at an input so child graphicspath declarations take effect in order.
        pending = [iter([Path(main)])]
        graphics_paths: list[Path] = []
        bibliography_fallback = Path(main).with_suffix(".bbl")
        included: set[Path] = set()
        while pending:
            try:
                relative = next(pending[-1])
            except StopIteration:
                pending.pop()
                continue
            relative = self._normalized_relative(root, relative)
            if relative in included:
                continue
            path = root / relative
            if not path.is_file():
                raise InfrastructureError(f"Referenced manuscript file is missing: {relative}")
            included.add(relative)
            if path.suffix.lower() != ".tex":
                continue
            pending.append(self._source_dependencies(root, relative, graphics_paths, bibliography_fallback))
        sources = []
        for relative in sorted(included):
            data = (root / relative).read_bytes()
            line_count = len(data.decode("utf-8", errors="replace").splitlines())
            sources.append(SourceFile(relative.as_posix(), sha256(data).hexdigest(), line_count))
        return tuple(sources)

    def _source_dependencies(
        self, root: Path, relative: Path, graphics_paths: list[Path], bibliography_fallback: Path
    ) -> Iterator[Path]:
        text = self._dependency_text((root / relative).read_text(encoding="utf-8"))
        commands = sorted(
            (match.start(), kind, match)
            for kind, pattern in (
                ("input", INPUT_PATTERN),
                ("bibliography", BIB_PATTERN),
                ("addbibresource", ADDBIB_PATTERN),
                ("graphics", GRAPHICS_PATTERN),
                ("graphicspath", GRAPHICSPATH_PATTERN),
            )
            for match in pattern.finditer(text)
        )
        for _, kind, match in commands:
            raw = next((group for group in match.groups() if group is not None), "")
            if kind == "graphicspath":
                if not raw or "\\" in raw:
                    raise InfrastructureError(f"Only literal \\graphicspath declarations are supported: {relative}")
                graphics_paths[:] = [
                    self._normalized_relative(root, Path(directory))
                    for directory in re.findall(r"\{([^{}]*)\}", raw[1:-1])
                ]
                continue
            for name in raw.split(",") if kind == "bibliography" else [raw]:
                dependency = Path(name.strip())
                if not dependency.suffix and kind in {"input", "bibliography"}:
                    dependency = dependency.with_suffix(".tex" if kind == "input" else ".bib")
                yield self._resolve_dependency(
                    root,
                    relative.parent,
                    dependency,
                    GRAPHICS_EXTENSIONS if kind == "graphics" else ("",),
                    search_paths=tuple(graphics_paths) if kind == "graphics" else (),
                    fallback=bibliography_fallback if kind in {"bibliography", "addbibresource"} else None,
                )

    def build(self, workspace: Path, manuscript: ManuscriptConfig) -> BuildResult:
        workspace = workspace.resolve()
        for path in workspace.rglob("*"):
            if path.is_symlink():
                self._normalized_relative(workspace, path.relative_to(workspace))
        main = Path(manuscript.main)
        main_input = self._normalized_relative(workspace, main)
        originals = {
            path.relative_to(workspace).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in workspace.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        for relative in originals:
            if Path(relative).suffix.lower() in GENERATED_INPUT_EXTENSIONS or relative.lower().endswith(".run.xml"):
                (workspace / relative).unlink()
        # A successful no-op must never certify copied recorder/PDF evidence.
        for suffix in (".fls", ".fdb_latexmk", ".pdf", ".xdv", ".log"):
            (workspace / main.with_suffix(suffix)).unlink(missing_ok=True)
        engine_option = {
            "pdflatex": "-pdf",
            "xelatex": "-xelatex",
            "lualatex": "-lualatex",
        }[manuscript.engine]
        command = [
            "latexmk",
            "-norc",
            engine_option,
            "-g",
            "-recorder",
            f"-outdir={main.parent}",
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
        texmf_roots = self._texmf_roots()
        inputs, outputs = self._read_recorder(workspace, main, texmf_roots)
        output_suffix = ".xdv" if manuscript.engine == "xelatex" else ".pdf"
        engine_output = self._normalized_relative(workspace, main.with_suffix(output_suffix))
        if main_input not in inputs or engine_output not in outputs:
            raise InfrastructureError("LaTeX recorder does not identify the main input and engine output")
        helper_inputs, derivations = self._helper_evidence(workspace, main, texmf_roots, inputs, originals)
        inputs.update(helper_inputs)
        compiler_inputs = self._compiler_inputs(
            workspace, originals, inputs, outputs, tuple(Path(item.path) for item in derivations)
        )
        evidence = {
            "compiler_inputs": [asdict(item) for item in compiler_inputs],
            "derived_outputs": [asdict(item) for item in derivations],
            "inputs": [path.as_posix() for path in sorted(inputs)],
            "outputs": [path.as_posix() for path in sorted(outputs)],
        }
        log += "\nCompiler input evidence: " + json.dumps(evidence, sort_keys=True)
        return BuildResult(pdf_path=pdf_path, log=log, compiler_inputs=compiler_inputs)

    @staticmethod
    def _texmf_roots() -> tuple[TexmfRoot, ...]:
        roots = []
        for expression, distribution in (
            ("{$TEXMFDIST,$TEXMFMAIN}", True),
            ("{$TEXMF,$TEXMFCNF,$TEXMFCACHE}", False),
        ):
            try:
                result = subprocess.run(
                    ["kpsewhich", f"--expand-path={expression}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                raise InfrastructureError("kpsewhich is required to identify external TeX installation inputs") from exc
            paths = tuple(
                Path(value).resolve()
                for raw in result.stdout.strip().split(os.pathsep)
                if (value := raw.removeprefix("!!")) and Path(value).is_absolute()
            )
            if result.returncode or not paths or Path("/") in paths:
                raise InfrastructureError("Cannot identify external TeX installation roots")
            roots.extend(TexmfRoot(path, distribution) for path in paths)
        return tuple(roots)

    @classmethod
    def _read_recorder(
        cls, workspace: Path, main: Path, texmf_roots: tuple[TexmfRoot, ...] = ()
    ) -> tuple[set[Path], set[Path]]:
        recorder = workspace / main.with_suffix(".fls")
        try:
            lines = recorder.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise InfrastructureError(f"Cannot read fresh LaTeX recorder: {recorder.name}") from exc
        current = None
        records: dict[str, set[Path]] = {"INPUT": set(), "OUTPUT": set()}
        for line in lines:
            kind, separator, value = line.partition(" ")
            if not separator or not value or kind not in {"PWD", "INPUT", "OUTPUT"}:
                raise InfrastructureError(f"Malformed LaTeX recorder line: {line!r}")
            if kind == "PWD":
                current = Path(value)
                if not current.is_absolute() or current.resolve() != workspace:
                    raise InfrastructureError("LaTeX recorder PWD does not match the build workspace")
                continue
            if current is None:
                raise InfrastructureError("LaTeX recorder is missing PWD before file records")
            relative = cls._recorded_path(workspace, current / value, texmf_roots, kind)
            if relative is not None:
                records[kind].add(relative)
        return records["INPUT"], records["OUTPUT"]

    @classmethod
    def _recorded_path(cls, workspace: Path, path: Path, texmf_roots: tuple[TexmfRoot, ...], kind: str) -> Path | None:
        if ".codex" in path.parts or path.name == "AGENTS.md":
            raise InfrastructureError(f"Manuscript dependency is not allowed in an agent bundle: {path}")
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            resource = (
                path.suffix.lower() in BUILD_INPUT_EXTENSIONS | {".lua", ".luc", ".fmt", ".cnf"}
                or path.name.lower().endswith((".lua.gz", ".luc.gz"))
                or (kind == "OUTPUT" and path.name == "m_t_x_t_e_s_t.tmp")
            )
            system_file = any(
                path.resolve().is_relative_to(root.path) and (root.distribution or resource) for root in texmf_roots
            )
            external_font = kind == "INPUT" and path.suffix.lower() in FONT_INPUT_EXTENSIONS
            if not (system_file or external_font):
                raise InfrastructureError(f"LaTeX recorder file is outside the snapshot and TeX installation: {path}")
            return None
        return cls._normalized_relative(workspace, relative)

    @staticmethod
    def _latexmk_rules(workspace: Path, main: Path) -> Iterator[tuple[str, str, str, set[str]]]:
        try:
            text = (workspace / main.with_suffix(".fdb_latexmk")).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InfrastructureError("Cannot read fresh latexmk helper dependency evidence") from exc
        blocks = re.split(r'(?=^\[")', text, flags=re.MULTILINE)
        if blocks[0].strip() != "# Fdb version 4":
            raise InfrastructureError("Unsupported or malformed latexmk dependency database")
        primary_seen = False
        for block in blocks[1:]:
            header, *lines = block.splitlines()
            match = re.fullmatch(r'\["([^"\n]+)"\] (\S+) "([^"\n]*)" "([^"\n]*)" "[^"\n]*" \S+ (-?\d+)', header)
            if not match:
                raise InfrastructureError("Malformed latexmk rule header")
            name, run_time, source, target, result = match.groups()
            if name in {"pdflatex", "xelatex", "lualatex"}:
                primary_seen = True
            if not name.startswith(("bibtex ", "biber ", "cusdep eps pdf ")):
                continue
            try:
                ran = float(run_time) > 0
            except ValueError as exc:
                raise InfrastructureError("Malformed latexmk helper timestamp") from exc
            if not ran:
                continue
            if result != "0":
                raise InfrastructureError(f"Bibliography/conversion helper did not succeed: {name}")
            inputs, outputs = ManuscriptManager._latexmk_rule_files(lines)
            if target not in outputs:
                raise InfrastructureError(f"Missing helper output evidence: {target}")
            yield name, source, target, inputs
        if not primary_seen:
            raise InfrastructureError("Latexmk dependency evidence has no primary engine rule")

    @staticmethod
    def _latexmk_rule_files(lines: list[str]) -> tuple[set[str], set[str]]:
        section = "inputs"
        inputs: set[str] = set()
        outputs: set[str] = set()
        for raw in lines:
            line = raw.strip()
            if line in {"(generated)", "(rewritten before read)"}:
                section = line
                continue
            if not line:
                continue
            pattern = r'"([^"\n]+)" \S+ \S+ \S+ "[^"\n]*"' if section == "inputs" else r'"([^"\n]+)"'
            match = re.fullmatch(pattern, line)
            if not match:
                raise InfrastructureError("Malformed latexmk helper file record")
            if section == "inputs":
                inputs.add(match.group(1))
            elif section == "(generated)":
                outputs.add(match.group(1))
        return inputs, outputs

    @classmethod
    def _helper_evidence(
        cls,
        workspace: Path,
        main: Path,
        texmf_roots: tuple[TexmfRoot, ...],
        compiler_inputs: set[Path],
        originals: dict[str, str],
    ) -> tuple[set[Path], tuple[BuildDerivation, ...]]:
        inputs: set[Path] = set()
        derivations = []
        for name, source, target, files in cls._latexmk_rules(workspace, main):
            local = {
                relative
                for file in files
                if (relative := cls._recorded_path(workspace, workspace / file, texmf_roots, "INPUT")) is not None
            }
            target = cls._normalized_relative(workspace, Path(target))
            if name.startswith("cusdep eps pdf "):
                source = cls._normalized_relative(workspace, Path(source))
                if source not in local or source.suffix.lower() != ".eps" or target.suffix.lower() != ".pdf":
                    raise InfrastructureError("Invalid EPS helper derivation")
                if target.as_posix() in originals:
                    raise InfrastructureError(f"Converted graphic must not be a pre-existing snapshot input: {target}")
            elif target.suffix.lower() != ".bbl":
                raise InfrastructureError("Invalid bibliography helper output")
            inputs.update(local)
            derivations.append(cls._derivation(workspace, target, name, local))
        for source, target in cls._eps_conversions(workspace, main):
            if target not in compiler_inputs:
                continue
            if source.as_posix() not in originals or target.as_posix() in originals:
                raise InfrastructureError(f"EPS conversion lacks a frozen source or fresh output: {target}")
            inputs.add(source)
            derivations.append(cls._derivation(workspace, target, "epstopdf", {source}))
        return inputs, tuple(derivations)

    @staticmethod
    def _derivation(workspace: Path, target: Path, tool: str, inputs: set[Path]) -> BuildDerivation:
        try:
            digest = sha256((workspace / target).read_bytes()).hexdigest()
        except OSError as exc:
            raise InfrastructureError(f"Cannot read helper output: {target}") from exc
        return BuildDerivation(target.as_posix(), digest, tool, tuple(path.as_posix() for path in sorted(inputs)))

    @classmethod
    def _eps_conversions(cls, workspace: Path, main: Path) -> Iterator[tuple[Path, Path]]:
        try:
            text = (workspace / main.with_suffix(".log")).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise InfrastructureError("Cannot read fresh LaTeX conversion log") from exc
        blocks = re.split(r"Package epstopdf Info: Source file: <", text.replace("\n", ""))[1:]
        for block in blocks:
            source, _, details = block.partition(">")
            output = re.search(r"\(epstopdf\)\s*Output file: <([^>]+)>", details)
            command = re.search(r"\(epstopdf\)\s*Command: <([^>]+)>", details)
            if not output or not command:
                raise InfrastructureError("Incomplete EPS conversion evidence")
            try:
                args = shlex.split(command.group(1))
            except ValueError as exc:
                raise InfrastructureError("Malformed EPS conversion command") from exc
            if len(args) != 3 or args[0] not in {"epstopdf", "repstopdf"}:
                raise InfrastructureError("Unsupported EPS converter invocation")
            target = output.group(1)
            if args[1] != f"--outfile={target}" or args[2] != source:
                raise InfrastructureError("EPS conversion command does not match its declared input/output")
            source_path = cls._normalized_relative(workspace, Path(source))
            target_path = cls._normalized_relative(workspace, Path(target))
            if source_path.suffix.lower() != ".eps" or target_path.suffix.lower() != ".pdf":
                raise InfrastructureError("Invalid EPS conversion file types")
            yield source_path, target_path

    @staticmethod
    def _compiler_inputs(
        workspace: Path,
        originals: dict[str, str],
        inputs: set[Path],
        outputs: set[Path],
        derived_outputs: tuple[Path, ...] = (),
    ) -> tuple[CompilerInput, ...]:
        evidence = []
        for relative in sorted(inputs):
            name = relative.as_posix()
            suffix = relative.suffix.lower()
            if relative in derived_outputs:
                continue
            if (suffix in GENERATED_INPUT_EXTENSIONS or name.lower().endswith(".run.xml")) and relative in outputs:
                continue
            if name not in originals:
                raise InfrastructureError(f"Compiler input has no snapshot source: {name}")
            if suffix not in BUILD_INPUT_EXTENSIONS | REVIEW_INPUT_EXTENSIONS:
                raise InfrastructureError(f"Unclassified repository-local compiler input: {name}")
            if not (workspace / relative).is_file():
                raise InfrastructureError(f"Compiler input is no longer readable: {name}")
            if relative in outputs or sha256((workspace / relative).read_bytes()).hexdigest() != originals[name]:
                raise InfrastructureError(f"Compiler modified a snapshot input: {name}")
            kind = "build" if suffix in BUILD_INPUT_EXTENSIONS else "review"
            evidence.append(CompilerInput(name, originals[name], kind))
        return tuple(evidence)

    @staticmethod
    def validate_build_sources(build: BuildResult, sources: tuple[SourceFile, ...]) -> None:
        # Non-LaTeX builders need not claim compiler-recorder evidence.
        if build.compiler_inputs is None:
            return
        frozen = {source.path: source.digest for source in sources}
        for item in build.compiler_inputs:
            if item.kind == "review" and frozen.get(item.path) != item.digest:
                raise InfrastructureError(
                    f"Compiler input is missing or differs from the frozen review sources: {item.path}. "
                    "Start a new run with a complete supported source closure; do not modify frozen bundles."
                )

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
        manifest_path = destination / "manifest.json"
        manifest_temporary = destination / ".manifest.json.tmp"
        manifest_temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_temporary.replace(manifest_path)
        source_map_path = destination / "source-map.json"
        source_map_temporary = destination / ".source-map.json.tmp"
        source_map_temporary.write_text(
            json.dumps(anchor_map.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # This final rename is the completion marker consumed by verification resume.
        source_map_temporary.replace(source_map_path)
        return ManuscriptBundle(destination, sources, page_count, anchor_map)

    def apply_edits(self, snapshot: Path, patched: Path, edits: Iterable[ExactEdit]) -> tuple[str, tuple[str, ...]]:
        if patched.exists():
            shutil.rmtree(patched)
        shutil.copytree(snapshot, patched, symlinks=True)
        edits_by_path: dict[str, list[ExactEdit]] = {}
        for edit in edits:
            edits_by_path.setdefault(edit.path, []).append(edit)
        changed_paths: list[str] = []
        for relative, path_edits in sorted(edits_by_path.items()):
            source_path = snapshot / relative
            target_path = patched / self._normalized_relative(patched.resolve(), Path(relative))
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
        search_paths: tuple[Path, ...] = (),
    ) -> Path:
        candidates: list[Path] = []
        for suffix in suffixes:
            for parent in (Path(), *search_paths, base):
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
    def _dependency_text(text: str) -> str:
        # Consume control symbols in pairs so a line break cannot start a command.
        token_pattern = re.compile(r"%[^\n]*|\\(?:[A-Za-z@]+\*?|[^\n])")
        parts = []
        position = 0
        while match := token_pattern.search(text, position):
            parts.append(text[position : match.start()])
            token = match.group()
            end = match.end()
            replacement = token
            if token.startswith("%") or not token[1].isalpha():
                replacement = " "
            elif token in {r"\verb", r"\verb*"}:
                literal = re.match(r"([^\n])[^\n]*?\1", text[end:])
                if literal:
                    end += literal.end()
                    replacement = " "
            elif token == r"\begin":
                environment = re.match(r"\s*\{(verbatim\*?)\}", text[end:])
                if environment:
                    terminator = rf"\end{{{environment.group(1)}}}"
                    closing = text.find(terminator, end + environment.end())
                    end = len(text) if closing == -1 else closing + len(terminator)
                    replacement = " "
            parts.append(replacement)
            position = end
        parts.append(text[position:])
        return "".join(parts)
