from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scriptorium.domain import digest_json


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def contained_file(root, name):
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
        raise ValueError(f"Unsafe input path: {name}")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or unsafe input file: {name}")
    return path


def file_records(root):
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in collected material: {path}")
        if path.is_file():
            data = path.read_bytes()
            records.append(
                {"path": path.relative_to(root).as_posix(), "digest": sha256(data).hexdigest(), "size": len(data)}
            )
    return records


def seal(root):
    files = file_records(root)
    value = {"digest": digest_json(files), "files": files}
    write_json(root / "seal.json", value)
    return value["digest"]


def verify_seal(root):
    expected = read_json(root / "seal.json")
    actual = [item for item in file_records(root) if item["path"] != "seal.json"]
    if actual != expected["files"] or digest_json(actual) != expected["digest"]:
        raise ValueError(f"Collection has changed: {root}")
    return expected["digest"]


@contextmanager
def publication(output, inputs=()):
    output = output.resolve()
    if any(output.is_relative_to(root.resolve()) for root in inputs):
        raise ValueError("Output must be outside sealed input directories")
    if output.exists():
        raise ValueError(f"Output already exists; choose a new directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".external-review-", dir=output.parent) as temporary:
        stage = Path(temporary) / "export"
        stage.mkdir()
        yield stage
        seal(stage)
        if output.exists():
            raise ValueError(f"Output already exists: {output}")
        stage.rename(output)
