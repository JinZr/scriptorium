from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from typing import BinaryIO

from scriptorium.domain import Artifact


class ArtifactError(RuntimeError):
    pass


class ArtifactNotFoundError(ArtifactError):
    pass


class ArtifactCorruptionError(ArtifactError):
    pass


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.sha256_root = self.root / "sha256"
        self.sha256_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def digest_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def path_for(self, digest: str) -> Path:
        self._validate_digest(digest)
        return self.sha256_root / digest[:2] / digest[2:]

    def put_bytes(
        self,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> Artifact:
        digest = self.digest_bytes(data)
        target = self.path_for(digest)
        if target.exists():
            self._verify_target(target, digest, len(data))
            return self._artifact(target, digest, len(data), media_type)

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._temporary_path(target.parent)
        try:
            with temporary_path.open("wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self._artifact(target, digest, len(data), media_type)

    def put_text(
        self,
        text: str,
        media_type: str = "text/plain; charset=utf-8",
    ) -> Artifact:
        return self.put_bytes(text.encode("utf-8"), media_type=media_type)

    def put_file(
        self,
        source_path: str | Path,
        media_type: str = "application/octet-stream",
    ) -> Artifact:
        source = Path(source_path)
        digest = self.digest_file(source)
        size = source.stat().st_size
        target = self.path_for(digest)
        if target.exists():
            self._verify_target(target, digest, size)
            return self._artifact(target, digest, size, media_type)

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._temporary_path(target.parent)
        try:
            with source.open("rb") as input_file, temporary_path.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            self._verify_target(temporary_path, digest, size)
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self._artifact(target, digest, size, media_type)

    def get_bytes(self, digest: str) -> bytes:
        target = self.path_for(digest)
        if not target.is_file():
            raise ArtifactNotFoundError(f"artifact not found: {digest}")
        data = target.read_bytes()
        if self.digest_bytes(data) != digest:
            raise ArtifactCorruptionError(f"artifact digest mismatch: {digest}")
        return data

    def open(self, digest: str) -> BinaryIO:
        target = self.path_for(digest)
        if not target.is_file():
            raise ArtifactNotFoundError(f"artifact not found: {digest}")
        if not self.verify(digest):
            raise ArtifactCorruptionError(f"artifact digest mismatch: {digest}")
        return target.open("rb")

    def verify(self, digest: str) -> bool:
        target = self.path_for(digest)
        return target.is_file() and self.digest_file(target) == digest

    def materialize(self, digest: str, destination: str | Path) -> Path:
        source = self.path_for(digest)
        if not source.is_file():
            raise ArtifactNotFoundError(f"artifact not found: {digest}")
        if not self.verify(digest):
            raise ArtifactCorruptionError(f"artifact digest mismatch: {digest}")
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._temporary_path(destination_path.parent)
        try:
            with source.open("rb") as input_file, temporary_path.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_path, destination_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination_path

    def _artifact(self, path: Path, digest: str, size: int, media_type: str) -> Artifact:
        return Artifact(
            digest=digest,
            relative_path=str(path.relative_to(self.root)),
            size=size,
            media_type=media_type,
        )

    @staticmethod
    def _temporary_path(directory: Path) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=".artifact-", dir=directory)
        os.close(descriptor)
        return Path(name)

    @classmethod
    def _verify_target(cls, target: Path, digest: str, expected_size: int) -> None:
        if target.stat().st_size != expected_size or cls.digest_file(target) != digest:
            raise ArtifactCorruptionError(f"existing artifact is corrupt: {digest}")

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
