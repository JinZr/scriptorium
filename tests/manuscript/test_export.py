import os

import pytest

from scriptorium.errors import ConfigurationError, StateError
from scriptorium.manuscript import export_files, export_root


def _read(name):
    return f"bytes of {name}".encode()


def test_files_are_written_read_only_in_a_new_directory(tmp_path):
    destination = tmp_path / "new" / "export"
    root = export_files(destination, ["a.txt", "sources/deep/b.tex"], _read, lambda: None)
    assert root == destination.resolve()
    assert (root / "sources/deep/b.tex").read_bytes() == b"bytes of sources/deep/b.tex"
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in root.rglob("*") if path.is_file())
    assert not [path for path in root.rglob(".export-*")]


def test_an_empty_existing_directory_is_accepted_and_kept_on_failure(tmp_path):
    destination = tmp_path / "empty"
    destination.mkdir()
    export_files(destination, ["a.txt"], _read, lambda: None)
    assert (destination / "a.txt").exists()
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RuntimeError):
        export_files(other, ["a.txt"], _read, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert other.is_dir() and list(other.iterdir()) == []


def test_nonempty_file_and_symlink_destinations_are_refused(tmp_path):
    used = tmp_path / "used"
    used.mkdir()
    (used / "keep.txt").write_text("keep", encoding="utf-8")
    plain_file = tmp_path / "file"
    plain_file.write_text("x", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    link = tmp_path / "link"
    os.symlink(empty, link)
    for destination in (used, plain_file, link):
        with pytest.raises(ConfigurationError):
            export_root(destination)
        with pytest.raises(ConfigurationError):
            export_files(destination, ["a.txt"], _read, lambda: None)
    assert (used / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize(
    "name", ["../escape.txt", "sources/../../escape.txt", "/etc/escape.txt", "", ".", "a//b", "a\\b", "./a.txt"]
)
def test_paths_that_could_leave_the_directory_are_refused_without_writing(tmp_path, name):
    destination = tmp_path / "export"
    with pytest.raises(StateError, match="safe relative path"):
        export_files(destination, ["first.txt", name], _read, lambda: None)
    assert not destination.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_a_symlinked_parent_cannot_redirect_a_write_outside(tmp_path, monkeypatch):
    destination = tmp_path / "export"
    outside = tmp_path / "outside"
    outside.mkdir()

    def read(name):
        # A hostile race: after the first file the directory is replaced by a link that leaves the export.
        if name == "second/file.txt":
            os.symlink(outside, destination / "second")
        return _read(name)

    with pytest.raises(StateError, match="escapes"):
        export_files(destination, ["first.txt", "second/file.txt"], read, lambda: None)
    assert list(outside.iterdir()) == []


def test_a_colliding_name_is_refused_and_everything_is_removed(tmp_path):
    destination = tmp_path / "export"
    with pytest.raises(StateError, match="collides"):
        export_files(destination, ["a.txt", "dir/b.txt", "a.txt"], _read, lambda: None)
    assert not destination.exists()


def test_a_failure_part_way_removes_every_file_and_created_directory(tmp_path, monkeypatch):
    destination = tmp_path / "export"
    real_replace = os.replace
    calls = []

    def failing_replace(source, target):
        calls.append(target)
        if len(calls) == 3:
            raise OSError("disk full")
        real_replace(source, target)

    monkeypatch.setattr("scriptorium.manuscript.os.replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        export_files(destination, ["a.txt", "dir/b.txt", "dir/c.txt"], _read, lambda: None)
    assert len(calls) == 3
    assert not destination.exists()
    assert not list(tmp_path.glob("**/.export-*"))


def test_a_failure_in_the_first_file_of_a_new_subdirectory_leaves_nothing(tmp_path, monkeypatch):
    destination = tmp_path / "export"

    def failing_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr("scriptorium.manuscript.os.replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        export_files(destination, ["sources/deep/a.txt"], _read, lambda: None)
    assert not destination.exists()


def test_a_failing_record_step_rolls_the_export_back(tmp_path):
    destination = tmp_path / "export"

    def record():
        assert (destination / "a.txt").exists()
        raise RuntimeError("cannot record")

    with pytest.raises(RuntimeError, match="cannot record"):
        export_files(destination, ["a.txt"], _read, record)
    assert not destination.exists()
