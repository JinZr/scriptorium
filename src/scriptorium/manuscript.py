from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
import difflib
import errno
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

from .config import ManuscriptConfig
from .errors import ConfigurationError, InfrastructureError, StateError
from .schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    CompiledPdfAnchor,
    CompiledPdfDocumentRecord,
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
TABLE_ENVIRONMENTS = (
    "table",
    "table*",
    "tabular",
    "tabular*",
    "tabularx",
    "tabulary",
    "longtable",
    "sidewaystable",
)
EQUATION_ENVIRONMENTS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "alignat",
    "alignat*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "flalign",
    "flalign*",
    "eqnarray",
    "eqnarray*",
    "displaymath",
)
# Mandatory brace groups per siunitx command: numbers first, then the unit.
QUANTITY_COMMAND_ARGUMENTS = {
    "num": 1,
    "numlist": 1,
    "numproduct": 1,
    "SI": 2,
    "qty": 2,
    "SIlist": 2,
    "qtylist": 2,
    "SIproduct": 2,
    "qtyproduct": 2,
    "numrange": 2,
    "SIrange": 3,
    "qtyrange": 3,
    "ang": 1,
    "complexnum": 1,
    "complexqty": 2,
}
QUANTITY_COMMANDS = tuple(QUANTITY_COMMAND_ARGUMENTS)
NAVIGATION_COMMAND_PATTERN = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph|"
    r"label|ref|eqref|pageref|autoref|cref|Cref|cite|citep|citet|autocite|parencite|textcite|"
    r"caption|includegraphics)\*?(?![A-Za-z@])(?:\s*\[[^\]]*\]){0,2}\s*\{"
)
NAVIGATION_ENVIRONMENT_PATTERN = re.compile(
    r"\\begin\s*\{("
    + "|".join(re.escape(name) for name in sorted({*TABLE_ENVIRONMENTS, *EQUATION_ENVIRONMENTS}, key=len, reverse=True))
    + r")\}"
)
QUANTITY_COMMAND_PATTERN = re.compile(r"\\(" + "|".join(QUANTITY_COMMANDS) + r")(?![A-Za-z@])(?:\s*\[[^\]]*\])?\s*\{")
# Arguments of these commands are labels, paths, layout or colour settings rather than reported values.
NON_QUANTITY_ARGUMENT_PATTERN = re.compile(
    r"\\(label|ref|eqref|pageref|autoref|cref|Cref|cite[a-zA-Z]*|[a-z]*cite|includegraphics|input|include|url|href|"
    r"vspace|hspace|vskip|hskip|setlength|addtolength|resizebox|scalebox|rule|setcounter|addtocounter|linespread|"
    r"fontsize|usepackage|documentclass|bibliographystyle|bibliography|addbibresource|graphicspath|newcommand|"
    r"renewcommand|providecommand|def|gdef|edef|xdef|definecolor|color|colorbox|begin|end|multicolumn|multirow|"
    r"cline|specialrule|cmidrule|hypersetup|geometry|tag|kern|mskip|mkern|SetKw[A-Za-z]*)\*?(?![A-Za-z@])"
)
# Only the leading brace groups of these commands are settings; later groups hold typeset content.
NON_QUANTITY_GROUP_LIMITS = {
    "multicolumn": 2,
    "multirow": 2,
    "color": 1,
    "colorbox": 1,
    "begin": 1,
    "end": 1,
    "resizebox": 2,
    "scalebox": 1,
    "href": 1,
    "newcommand": 2,
    "renewcommand": 2,
    "providecommand": 2,
    "def": 2,
    "gdef": 2,
    "edef": 2,
    "xdef": 2,
    "tag": 1,
    "hskip": 0,
    "vskip": 0,
    "kern": 0,
    "mskip": 0,
    "mkern": 0,
    "setlength": 2,
    "addtolength": 2,
    "setcounter": 2,
    "addtocounter": 2,
    "definecolor": 3,
    "rule": 2,
    "fontsize": 2,
}
DEFINITION_COMMANDS = frozenset({"newcommand", "renewcommand", "providecommand", "def", "gdef", "edef", "xdef"})
# An unbraced defined name, as in \def\arraystretch{1.5}, with any parameter text before the body.
DEFINITION_NAME_PATTERN = re.compile(r"[ \t]*(?:\n[ \t]*)?\\(?:[A-Za-z@]+|.)(?:[^{}\n]*?(?=\{))?")
# Primitive skips and kerns take an unbraced dimension, as in \hskip 1.5cm plus 1fil.
SKIP_COMMANDS = frozenset({"hskip", "vskip", "kern", "mskip", "mkern"})
TEX_UNIT = r"(?:true\s*)?(?:pt|pc|in|bp|cm|mm|dd|cc|sp|em|ex|mu|px)"
TEX_DIMENSION = r"[-+]?\s*(?:\d+(?:[.,]\d*)?|[.,]\d+)\s*"
SKIP_DIMENSION_PATTERN = re.compile(
    r"\s*" + TEX_DIMENSION + TEX_UNIT + r"(?:\s*(?:plus|minus)\s*" + TEX_DIMENSION + r"(?:fil{1,3}|" + TEX_UNIT + "))*"
)
# A row break's optional spacing, as in \\[1.5mm], is a layout length rather than a reported value.
ROW_SPACING_PATTERN = re.compile(r"(?<!\\)\\\\\*?[ \t]*\[[^\]\n]*\]")
# Mandatory groups an environment takes after its name; other environments may open their body with a group.
ENVIRONMENT_ARGUMENT_GROUPS = {
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "tabulary": 2,
    "longtable": 1,
    "array": 1,
    "alignat": 1,
    "alignat*": 1,
    "minipage": 1,
    "multicols": 1,
    "subfigure": 1,
    "subtable": 1,
    "wrapfigure": 2,
    "wraptable": 2,
}
# TeX skips spaces and one line break before an argument; a blank line ends the search.
ARGUMENT_SPACE = r"[ \t]*(?:\n[ \t]*)?"
ENVIRONMENT_NAME_PATTERN = re.compile(ARGUMENT_SPACE + r"\{([^{}]*)\}")
# A number may directly follow a control word such as \approx; blank the word so the number is seen with its sign.
CONTROL_WORD_BEFORE_NUMBER_PATTERN = re.compile(
    r"\\(?!(?:pm|mp|times|cdot)(?![A-Za-z]))[A-Za-z]+(?=[-+\u2212]?(?:\d|\.\d))"
)
QUANTITY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:/\\@])(?:(?<!-)[-+\u2212]\s*)?"
    r"(?:\d{1,3}(?:(?:,|\{,\})\d{3})+(?:\.\d+)?|\d+\.\d+|\.\d+|\d+)(?!\d|\.\d)"
    r"(?P<uncertainty>\(\d+(?:\.\d+)?\))?"
    r"(?P<exponent>[eE][-+\u2212]?\d+)?"
    r"(?P<pm>\s*(?:\\pm|\u00b1|\+/-)\s*(?:\d+\.\d+|\.\d+|\d+))?"
    r"(?P<times>\s*(?:\\times|\u00d7|\\cdot)\s*10\s*\^\s*(?:\{\s*[-+\u2212]?\s*\d+\s*\}|[-+\u2212]?\d))?"
    r"(?P<percent>\s*\\%)?"
)
UNIT_POWER = r"(?:\^\s*(?:\{\s*[-+\u2212]?\s*\d+\s*\}|[-+\u2212]?\d))?"
UNIT_SPACE = r"(?:~|\\[,;: ]|[ \t])?[ \t]?"
UNIT_ARGUMENT = r"\s*\{(?:[^{}\n]|\{[^{}\n]{1,8}\}){1,24}\}" + UNIT_POWER
UNIT_WORD = r"[A-Za-zÅµμ]{1,10}" + UNIT_POWER
UNIT_SYNTAX = (
    r"(?:\\(?:mathrm|text|textrm|rm|mbox|unit|si)" + UNIT_ARGUMENT + "|" + UNIT_WORD + "(?:/" + UNIT_WORD + ")?)"
)
# Past closing math ($, $$, \) or \]), only a tie or thin space, or a math-mode or siunitx unit command, continues
# the value; a word after a plain space is prose, as in "$p=0.05$ threshold". A unit never starts on the next line.
QUANTITY_UNIT_PATTERN = re.compile(
    r"(?:(?:\$\$?|\\[)\]])(?:(?:~|\\[,;:])[ \t]?"
    + UNIT_SYNTAX
    + "|"
    + UNIT_SPACE
    + r"\\(?:mathrm|unit|si)"
    + UNIT_ARGUMENT
    + ")|"
    + UNIT_SPACE
    + UNIT_SYNTAX
    + r")(?![A-Za-z])"
)
# Numbers named by a version, software release, or numbered reference are not reported values.
NON_QUANTITY_CONTEXT_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:versions?|release|ver\.|v\.|python|cuda|pytorch|tensorflow|numpy|scipy|ubuntu|"
    r"tables?|tab\.|figures?|figs?\.|sections?|secs?\.|eqs?\.|equations?|chapters?|appendix|algorithms?|"
    r"theorems?|lemmas?|definitions?|corollar(?:y|ies)|propositions?|remarks?|\\S)(?:\s|~|\\ |\()*$",
    re.IGNORECASE,
)
QUANTITY_UNIT_STOPWORDS = frozenset(
    "a an and are as at be but by for from has in into is it of on or per than that the to was were which with".split()
)
LENGTH_FOLLOWER_PATTERN = re.compile(
    r"\s*(?:\\(?:text|line|column|paper)(?:width|height)|\\hsize|\\vsize|\\baselineskip|\\parindent|"
    r"pt|em|ex|bp|sp|pc|dd|cc)(?![A-Za-z])"
)
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


MAX_PAGE_VIEW_PIXELS = 40_000_000


def _pymupdf():
    # PyMuPDF is only needed to build or render PDFs; retrieval and submission calls skip its import cost.
    import pymupdf

    return pymupdf


def render_page_view(
    pdf_path: Path, page: int, scale: float, crop: tuple[float, float, float, float] | None, destination: Path
) -> None:
    """Render one 1-based page of a frozen PDF at a scale and optional fractional crop, atomically."""
    fitz = _pymupdf()
    document = fitz.open(pdf_path)
    try:
        selected = document[page - 1]
        bounds = selected.rect
        clip = None
        if crop is not None:
            x0, y0, x1, y1 = crop
            clip = fitz.Rect(
                bounds.x0 + x0 * bounds.width,
                bounds.y0 + y0 * bounds.height,
                bounds.x0 + x1 * bounds.width,
                bounds.y0 + y1 * bounds.height,
            )
        area = clip if clip is not None else bounds
        # Scale is bounded, but an oversized page could still make a huge image.
        if area.width * area.height * scale * scale > MAX_PAGE_VIEW_PIXELS:
            limit = MAX_PAGE_VIEW_PIXELS / 1_000_000
            raise ConfigurationError(
                f"page view would exceed {limit:g} megapixels; lower --scale or crop a smaller region"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
        selected.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False).save(temporary)
        temporary.replace(destination)
    finally:
        document.close()


def page_text(pdf_path: Path, page: int) -> str:
    """Return the PDF text layer of one 1-based page; it is a reading aid, not manuscript evidence."""
    fitz = _pymupdf()
    document = fitz.open(pdf_path)
    try:
        return document[page - 1].get_text("text")
    finally:
        document.close()


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
    documents: tuple[CompiledPdfDocumentRecord, ...] = ()


@dataclass(frozen=True)
class ManuscriptBundle:
    workspace: Path
    sources: tuple[SourceFile, ...]
    pdf_pages: int
    anchor_map: EvidenceAnchorMap | None = None


NAVIGATION_INDEXED_COMMANDS = frozenset(
    {
        "part",
        "chapter",
        "section",
        "subsection",
        "subsubsection",
        "paragraph",
        "subparagraph",
        "label",
        "ref",
        "eqref",
        "pageref",
        "autoref",
        "cref",
        "Cref",
        "cite",
        "citep",
        "citet",
        "autocite",
        "parencite",
        "textcite",
        "caption",
        "includegraphics",
        *TABLE_ENVIRONMENTS,
        *EQUATION_ENVIRONMENTS,
        *QUANTITY_COMMANDS,
        "quantity",
    }
)


def _balanced_group_end(text: str, position: int) -> int | None:
    """Return the index just past the brace closing a group whose opening brace ends at position."""
    depth = 1
    end = position
    while end < len(text) and depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return end if depth == 0 else None


def _unbraced_argument_end(text: str, name: str, position: int) -> int:
    """Return the end of a definition's unbraced name or a primitive skip's dimension, or position if none."""
    pattern = DEFINITION_NAME_PATTERN if name in DEFINITION_COMMANDS else SKIP_DIMENSION_PATTERN
    argument = pattern.match(text, position) if name in DEFINITION_COMMANDS | SKIP_COMMANDS else None
    return argument.end() if argument else position


def _command_argument_spans(text: str) -> Iterator[tuple[int, int]]:
    for match in NON_QUANTITY_ARGUMENT_PATTERN.finditer(text):
        start = match.end()
        end = _unbraced_argument_end(text, match.group(1), start)
        braces = NON_QUANTITY_GROUP_LIMITS.get(match.group(1))
        if end > start and match.group(1) in DEFINITION_COMMANDS:
            braces -= 1
        if match.group(1) == "begin" and (name := ENVIRONMENT_NAME_PATTERN.match(text, end)):
            braces = 1 + ENVIRONMENT_ARGUMENT_GROUPS.get(name.group(1).strip(), 0)
        # Optional bracket arguments after the last setting group, as in \scalebox{x}[y], are settings too.
        # A required group may start on the next line; trailing groups and brackets stay on the line.
        while (
            group := re.match((ARGUMENT_SPACE if end == start or braces else r"[ \t]*") + r"([\[{])", text[end:])
        ) and (braces is None or braces > 0 or group[1] == "["):
            opening = end + group.end()
            if group.group(1) == "{":
                closing = _balanced_group_end(text, opening)
                braces = None if braces is None else braces - 1
            else:
                bracket = text.find("]", opening)
                closing = None if bracket == -1 else bracket + 1
            if closing is None:
                break
            end = closing
        if end > start:
            yield start, end


def _row_spacing_spans(text: str, masked: str) -> list[tuple[int, int]]:
    # Masking blanks control symbols such as \\, so find row breaks in the text and keep those outside comments.
    return [match.span() for match in ROW_SPACING_PATTERN.finditer(text) if masked[match.end() - 1] == "]"]


def _environment_body_start(text: str, name: str, position: int) -> int:
    """Return where an environment's body starts, past the placement options and mandatory arguments it takes."""
    groups = ENVIRONMENT_ARGUMENT_GROUPS.get(name, 0)
    options = name in TABLE_ENVIRONMENTS
    while (group := re.match(ARGUMENT_SPACE + r"([\[{])", text[position:])) and (
        groups if group[1] == "{" else options
    ):
        opening = position + group.end()
        if group[1] == "{":
            closing = _balanced_group_end(text, opening)
            groups -= 1
        else:
            bracket = text.find("]", opening)
            closing = None if bracket == -1 else bracket + 1
        if closing is None:
            break
        position = closing
    return position


def _document_body(masked: str) -> tuple[int, int]:
    """Return the typeset span: after \\begin{document}, if present, and before \\end{document}, where TeX stops."""
    document = re.search(r"\\begin\s*\{document\}", masked)
    floor = document.end() if document else 0
    closing = re.compile(r"\\end\s*\{document\}").search(masked, floor)
    return floor, closing.start() if closing else len(masked)


def _walk_document(
    path: str,
    state: str,
    events: dict[str, list[tuple[int, str, str]]],
    lengths: dict[str, int],
    bounds: dict[str, tuple[int, int] | None],
    memo: dict[tuple[str, str], str],
) -> str:
    """Process one source from a document state (before, body, after) and return the state it leaves."""
    if (path, state) in memo:
        return memo[(path, state)]
    memo[(path, state)] = state  # An input cycle leaves the state unchanged.
    entered = state
    floor = 0 if state == "body" else None
    ceiling = lengths[path]
    for position, kind, child in events[path]:
        previous = state
        if kind == "input":
            state = _walk_document(child, state, events, lengths, bounds, memo)
        elif kind == "end" or state == "before":
            state = "after" if kind == "end" else "body"
        if previous != "body" and state == "body":
            floor = position
        elif previous == "body" and state != "body":
            ceiling = position
        if state == "after":
            break
    if floor is not None and floor < ceiling:
        known = bounds.get(path)
        bounds[path] = (min(known[0], floor), max(known[1], ceiling)) if known else (floor, ceiling)
    memo[(path, entered)] = state
    return state


def _input_closure(path: str, events: dict[str, list[tuple[int, str, str]]]) -> set[str]:
    tree = {path}
    pending = [path]
    while pending:
        for _, kind, child in events[pending.pop()]:
            if kind == "input" and child not in tree:
                tree.add(child)
                pending.append(child)
    return tree


def _merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for left, right in sorted(spans):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


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

    def scan_project_sources(self, snapshot: Path, manuscript: ManuscriptConfig) -> tuple[SourceFile, ...]:
        roots = [self._normalized_relative(snapshot.resolve(), Path(entry)) for entry in manuscript.entrypoints]
        if len(set(roots)) != len(roots):
            raise InfrastructureError("Review document entrypoints must resolve to distinct sources")
        sources = {
            source.path: source
            for entrypoint in manuscript.entrypoints
            for source in self.scan_sources(snapshot, entrypoint)
        }
        return tuple(sources[path] for path in sorted(sources))

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

    def create_navigation(
        self, snapshot: Path, sources: tuple[SourceFile, ...], entrypoints: tuple[str, ...] = ()
    ) -> str:
        entries = []
        texts = {
            source.path: (snapshot / source.path).read_text(encoding="utf-8")
            for source in sorted(sources, key=lambda item: item.path)
            if Path(source.path).suffix.lower() in {".tex", ".ltx"}
        }
        masks = {path: self._dependency_text(text, preserve_positions=True) for path, text in texts.items()}
        bodies = self._document_bodies(snapshot, masks, entrypoints)
        for path, text in texts.items():
            spans = self._navigation_spans(text, masks[path], bodies[path])
            breaks = [match.start() for match in re.finditer("\n", text)]
            for command, start, end, value_start, value_end in spans:
                value = text[value_start:value_end]
                entry = {
                    "command": command,
                    "source_path": path,
                    "start_line": bisect_left(breaks, start) + 1,
                    "end_line": bisect_left(breaks, end - 1) + 1,
                    "value": value.strip(),
                }
                if command == "includegraphics":
                    candidates = self._navigation_graphics(value.strip(), sources)
                    entry["candidate_paths"] = candidates
                    entry["target_path"] = candidates[0] if len(candidates) == 1 else None
                entries.append(entry)
        return (
            json.dumps(
                {
                    "sources": [asdict(source) for source in sorted(sources, key=lambda item: item.path)],
                    "commands": sorted(NAVIGATION_INDEXED_COMMANDS),
                    "entries": entries,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

    def _document_bodies(
        self, snapshot: Path, masks: dict[str, str], entrypoints: tuple[str, ...] = ()
    ) -> dict[str, tuple[int, int] | None]:
        """Follow inputs in processing order and bound each source by the document body it reaches, if any."""
        root = snapshot.resolve()
        events: dict[str, list[tuple[int, str, str]]] = {}
        for path, masked in masks.items():
            items = [
                (match.end() if match[1] == "begin" else match.start(), match[1], "")
                for match in re.finditer(r"\\(begin|end)\s*\{document\}", masked)
            ]
            for match in INPUT_PATTERN.finditer(masked):
                dependency = Path(next(group for group in match.groups() if group is not None).strip())
                try:
                    child = self._resolve_dependency(
                        root, Path(path).parent, dependency.with_suffix(dependency.suffix or ".tex")
                    ).as_posix()
                except InfrastructureError:
                    continue
                if child in masks:
                    items.append((match.start(), "input", child))
            events[path] = sorted(items)
        included = {child for items in events.values() for _, kind, child in items if kind == "input"}
        lengths = {path: len(masked) for path, masked in masks.items()}
        bounds: dict[str, tuple[int, int] | None] = {}
        reached: set[str] = set()
        # A configured entrypoint is typeset on its own even when another source also inputs it.
        configured = {self._normalized_relative(root, Path(entry)).as_posix() for entry in entrypoints}
        for path in sorted((set(masks) - included) | (configured & set(masks))):
            tree = _input_closure(path, events)
            reached |= tree
            # A source tree without \begin{document} is a fragment that is typeset as a whole.
            opens = any(kind == "begin" for member in tree for _, kind, _ in events[member])
            _walk_document(path, "before" if opens else "body", events, lengths, bounds, {})
        return {path: bounds.get(path) if path in reached else _document_body(masked) for path, masked in masks.items()}

    @classmethod
    def _navigation_spans(
        cls, text: str, masked: str, body: tuple[int, int] | None
    ) -> list[tuple[str, int, int, int, int]]:
        """Return (command, start, end, value_start, value_end) in source order; quantities only within body."""
        spans = [
            (command, start, end, end - 1 - len(value), end - 1)
            for command, start, end, value in cls._navigation_commands(masked)
        ]
        spans.extend(cls._navigation_environments(masked))
        if body is not None:
            floor, ceiling = body
            quantity_commands = [span for span in cls._quantity_commands(masked) if floor <= span[1] < ceiling]
            spans.extend(quantity_commands)
            covered = [(start, end) for _, start, end, _, _ in quantity_commands]
            spans.extend(cls._navigation_quantities(text, masked, covered, floor, ceiling))
        return sorted(spans, key=lambda span: span[1])

    @staticmethod
    def _navigation_commands(text: str) -> Iterator[tuple[str, int, int, str]]:
        for match in NAVIGATION_COMMAND_PATTERN.finditer(text):
            end = _balanced_group_end(text, match.end())
            if end is not None:
                yield match.group(1), match.start(), end, text[match.end() : end - 1]

    @staticmethod
    def _navigation_environments(text: str) -> Iterator[tuple[str, int, int, int, int]]:
        for match in NAVIGATION_ENVIRONMENT_PATTERN.finditer(text):
            name = match.group(1)
            boundary = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
            depth = 1
            for other in boundary.finditer(text, match.end()):
                depth += 1 if other.group(1) == "begin" else -1
                if depth == 0:
                    yield name, match.start(), other.end(), _environment_body_start(
                        text, name, match.end()
                    ), other.start()
                    break

    @staticmethod
    def _quantity_commands(text: str) -> Iterator[tuple[str, int, int, int, int]]:
        for match in QUANTITY_COMMAND_PATTERN.finditer(text):
            end = _balanced_group_end(text, match.end())
            if end is None:
                continue
            for _ in range(QUANTITY_COMMAND_ARGUMENTS[match.group(1)] - 1):
                following = re.match(r"\s*\{", text[end:])
                group_end = _balanced_group_end(text, end + following.end()) if following else None
                if group_end is None:
                    break
                end = group_end
            yield match.group(1), match.start(), end, match.end() - 1, end

    @staticmethod
    def _navigation_quantities(text: str, masked: str, skipped: list[tuple[int, int]], floor: int, ceiling: int):
        # Literal numeric reports: decimals, uncertainties, exponents, plus-minus or percentages, with an adjacent unit.
        excluded = _merged_spans([*skipped, *_command_argument_spans(masked), *_row_spacing_spans(text, masked)])
        lefts = [left for left, _ in excluded]
        scanned = CONTROL_WORD_BEFORE_NUMBER_PATTERN.sub(lambda word: " " * len(word[0]), text)
        for match in QUANTITY_PATTERN.finditer(scanned, floor, ceiling):
            start, end = match.span()
            nearest = bisect_right(lefts, start) - 1
            if masked[start] != text[start] or (nearest >= 0 and start < excluded[nearest][1]):
                continue
            if LENGTH_FOLLOWER_PATTERN.match(text, end) or NON_QUANTITY_CONTEXT_PATTERN.search(
                text, max(floor, start - 24), start
            ):
                continue
            core = match.group(0)
            if not (
                "." in core or any(match.group(name) for name in ("uncertainty", "exponent", "pm", "times", "percent"))
            ):
                continue
            unit = QUANTITY_UNIT_PATTERN.match(text, end)
            word = unit.group(0).strip("$)]~\\,;: \t\r\n") if unit else ""
            # One-letter symbols are case-sensitive: "1.5 A" reports amperes, while "1.5 a" continues the prose.
            if (
                not match.group("percent")
                and unit
                and (word if len(word) == 1 else word.casefold()) not in (QUANTITY_UNIT_STOPWORDS)
            ):
                end = unit.end()
            yield "quantity", start, end, start, end

    @staticmethod
    def _navigation_graphics(value: str, sources: tuple[SourceFile, ...]) -> list[str]:
        if not value or "\\" in value or Path(value).is_absolute() or ".." in Path(value).parts:
            return []
        names = {value + suffix for suffix in GRAPHICS_EXTENSIONS}
        # Only offer frozen candidates; graphicspath state and TeX expansion are not inferred here.
        return sorted(
            source.path
            for source in sources
            if any(source.path == name or source.path.endswith("/" + name) for name in names)
        )

    def build_project(self, snapshot: Path, destination: Path, manuscript: ManuscriptConfig) -> BuildResult:
        if destination.exists():
            shutil.rmtree(destination)
        builds = []
        documents = []
        fitz = _pymupdf()
        with fitz.open() as combined:
            for index, entrypoint in enumerate(manuscript.entrypoints):
                workspace = destination / f"document-{index + 1}"
                shutil.copytree(snapshot, workspace, symlinks=True)
                try:
                    build = self.build(workspace, ManuscriptConfig(entrypoint, manuscript.engine))
                    with fitz.open(build.pdf_path) as document:
                        documents.append(
                            CompiledPdfDocumentRecord(
                                entrypoint=self._normalized_relative(snapshot.resolve(), Path(entrypoint)).as_posix(),
                                start_page=combined.page_count + 1,
                                page_count=document.page_count,
                            )
                        )
                        combined.insert_pdf(document)
                except (InfrastructureError, RuntimeError, ValueError) as exc:
                    raise InfrastructureError(f"Cannot compile review document {entrypoint}: {exc}") from exc
                builds.append(build)
            pdf_path = destination / "manuscript.pdf"
            if len(builds) == 1:
                shutil.copy2(builds[0].pdf_path, pdf_path)
            else:
                combined.save(pdf_path)
        return BuildResult(
            pdf_path=pdf_path,
            log="\n\n".join(f"Document {item.entrypoint}:\n{build.log}" for item, build in zip(documents, builds)),
            compiler_inputs=tuple(item for build in builds for item in (build.compiler_inputs or ())),
            documents=tuple(documents),
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
        navigation: str | None = None,
        documents: tuple[CompiledPdfDocumentRecord, ...] = (),
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
        fitz = _pymupdf()
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
            "documents": [item.model_dump(mode="json") for item in documents],
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
                    line_count=len(anchor_contract.split_lines(text)) if text_anchorable else None,
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
                documents=list(documents),
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
        if navigation is not None:
            (destination / "navigation.json").write_text(navigation, encoding="utf-8")
            manifest["navigation_digest"] = sha256(navigation.encode("utf-8")).hexdigest()
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

    def apply_edits(
        self,
        snapshot: Path,
        patched: Path,
        edits: Iterable[ExactEdit],
        contract: EvidenceAnchorContract = DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    ) -> tuple[str, tuple[str, ...]]:
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
            lines = contract.split_lines(text, keepends=True)
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
                lines = contract.split_lines(text, keepends=True)
            target_path.write_text(text, encoding="utf-8")
            changed_paths.append(relative)
        return self.diff(snapshot, patched, changed_paths, contract), tuple(changed_paths)

    def diff(
        self,
        before: Path,
        after: Path,
        paths: Iterable[str],
        contract: EvidenceAnchorContract = DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    ) -> str:
        chunks: list[str] = []
        for relative in sorted(paths):
            # Resume recomputes this diff against the immutable artifact, so it follows the run's frozen line rule.
            old = contract.split_lines((before / relative).read_text(encoding="utf-8"), keepends=True)
            new = contract.split_lines((after / relative).read_text(encoding="utf-8"), keepends=True)
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
            if cls._is_existing_file(root / normalized):
                return normalized
        if fallback is not None:
            normalized = cls._normalized_relative(root, fallback)
            if cls._is_existing_file(root / normalized):
                return normalized
        raise InfrastructureError(f"Referenced manuscript file is missing: {dependency}")

    @staticmethod
    def _is_existing_file(path: Path) -> bool:
        # A candidate too long for the filesystem to name cannot exist; other access errors still surface.
        try:
            return path.is_file()
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:
                raise
            return False

    @staticmethod
    def _dependency_text(text: str, *, preserve_positions: bool = False) -> str:
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
            if preserve_positions and replacement == " ":
                replacement = "".join("\n" if char == "\n" else " " for char in text[match.start() : end])
            parts.append(replacement)
            position = end
        parts.append(text[position:])
        return "".join(parts)
