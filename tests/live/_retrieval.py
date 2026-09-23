from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import secrets

import fitz


@dataclass(frozen=True)
class RetrievalCase:
    prompt: str
    expected: dict[str, str]
    markers: tuple[str, ...]
    image: str | None = None

    @property
    def schema(self):
        return {
            "type": "object",
            "properties": {key: {"type": "string"} for key in self.expected},
            "required": list(self.expected),
            "additionalProperties": False,
        }


def make_fixture(workspace: Path) -> tuple[RetrievalCase, RetrievalCase]:
    workspace.mkdir()
    sources = workspace / "sources"
    sources.mkdir()
    values = {key: secrets.token_hex(8) for key in ("discovery", "tail", "linked", "appendix")}
    visual = "".join(secrets.choice("23456789") for _ in range(8))
    selected = secrets.token_hex(6)
    chosen = secrets.randbelow(8)
    for index in range(8):
        text = f"Archive note {index}. No release entry here.\n"
        if index == chosen:
            text += f"Release seal: {values['discovery']}\n"
        (sources / f"note-{index}.txt").write_text(text, encoding="utf-8")
    lines = [f"Record {index:04d}: routine observation with no audit value.\n" for index in range(3000)]
    lines[2400] = f"Final audit seal: {values['tail']}\n"
    (sources / "records.txt").write_text("".join(lines), encoding="utf-8")
    (sources / "selection.txt").write_text(f"Selected specimen: {selected}\n", encoding="utf-8")
    entries = [f"{secrets.token_hex(6)}: {secrets.token_hex(8)}\n" for _ in range(12)]
    entries.insert(secrets.randbelow(13), f"{selected}: {values['linked']}\n")
    (sources / "registry.txt").write_text("".join(entries), encoding="utf-8")
    (sources / "appendix.txt").write_text(f"Follow-up seal: {values['appendix']}\n", encoding="utf-8")
    pages = workspace / "pages"
    pages.mkdir()
    with fitz.open() as document:
        page = document.new_page(width=480, height=180)
        page.insert_text((30, 65), "Visual seal", fontsize=24)
        page.insert_text((30, 125), visual, fontsize=40)
        (pages / "plate.png").write_bytes(page.get_pixmap().tobytes("png"))
    manifest = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file())
    (workspace / "manifest.json").write_text(json.dumps({"files": manifest}), encoding="utf-8")
    first = RetrievalCase(
        "Inspect this frozen bundle using read/search/image tools. Return four fields: "
        "discovery = the Release seal in one of the archive notes (discover which file); "
        "tail = the Final audit seal near the end of sources/records.txt; "
        "linked = the registry value for the specimen selected in sources/selection.txt "
        "(consult sources/registry.txt); visual = the eight digits visible in pages/plate.png. "
        "Open the PNG as an image. Follow up truncated reads with further reads/searches. "
        "Do not modify files or inspect anything outside the bundle. Leave appendix.txt for the follow-up.",
        {"discovery": values["discovery"], "tail": values["tail"], "linked": values["linked"], "visual": visual},
        (values["discovery"], values["tail"], selected, values["linked"]),
        "pages/plate.png",
    )
    resumed = RetrievalCase(
        "Continue in the same bundle. Now read sources/appendix.txt with a tool and return its "
        "Follow-up seal in the appendix field. Do not modify files or inspect outside the bundle.",
        {"appendix": values["appendix"]},
        (values["appendix"],),
    )
    return first, resumed


def directory_digest(path: Path) -> str:
    digest = sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix().encode()
        if item.is_symlink():
            kind, content = b"link", str(item.readlink()).encode()
        elif item.is_file():
            kind, content = b"file", item.read_bytes()
        else:
            kind, content = b"dir", b""
        for value in (kind, relative, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()
