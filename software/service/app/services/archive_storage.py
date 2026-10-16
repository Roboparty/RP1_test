"""Crash-safe local evidence storage. User filenames never participate in paths."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
from uuid import UUID

from app.core.errors import ApiError


def sha256_file(path: Path, progress=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        chunks = 0
        while chunk := handle.read(1024**2):
            digest.update(chunk)
            chunks += 1
            if progress is not None and chunks%64==0:
                progress()
    return digest.hexdigest()


def fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArchiveStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("ARCHIVE_ROOT must be absolute")
        if self.root.is_symlink():
            raise ValueError("ARCHIVE_ROOT cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.root = self.root.resolve()

    def directory(self, execution_uuid: UUID | str) -> Path:
        path = self.root / str(UUID(str(execution_uuid)))
        if path.is_symlink():
            raise ApiError(409, "archive_storage_conflict", "归档目录不可是符号链接。")
        path.mkdir(mode=0o750, exist_ok=True)
        return path

    def path(self, execution_uuid: UUID | str, name: str) -> Path:
        # Internal names only; request filenames remain display-only metadata.
        if name not in {"upload.part", "raw.csv", "telemetry.parquet", "manifest.json", ".upload.lock", ".processing.lock"} and not (
            name.startswith("pyramid-") and name.endswith(".parquet") and name[8:-8].isdigit()
        ):
            raise ValueError("unsupported internal archive path")
        path = self.directory(execution_uuid) / name
        if path.is_symlink():
            raise ApiError(409, "archive_storage_conflict", "归档文件不可是符号链接。")
        return path

    @contextmanager
    def lock(self, execution_uuid: UUID | str, processing: bool = False):
        name = ".processing.lock" if processing else ".upload.lock"
        descriptor = os.open(self.path(execution_uuid, name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append(self, execution_uuid, received: int, size: int, offset: int, body: bytes, complete: bool = False) -> int:
        if offset < 0 or not body or offset + len(body) > size:
            raise ApiError(422, "invalid_chunk", "上传块超出声明文件范围或为空。")
        if complete:
            raw = self.path(execution_uuid, "raw.csv")
            if not raw.exists():
                raw = self.path(execution_uuid, "upload.part")
            with raw.open("rb") as handle:
                handle.seek(offset)
                if offset + len(body) <= received and handle.read(len(body)) == body:
                    return received
            raise ApiError(409, "immutable_archive", "已校验归档的原始 CSV 不可更改。")
        target = self.path(execution_uuid, "upload.part")
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        with os.fdopen(descriptor, "r+b") as handle:
            physical = os.fstat(handle.fileno()).st_size
            if physical < received:
                raise ApiError(409, "archive_storage_incomplete", "中央临时文件短于上传回执，请从备份恢复或重新归档。")
            # A crash after fsync but before DB commit leaves a harmless extra tail.
            if physical > received:
                handle.truncate(received)
            if offset < received:
                handle.seek(offset)
                if offset + len(body) <= received and handle.read(len(body)) == body:
                    return received
                raise ApiError(409, "chunk_conflict", "重试块内容不同，已保存数据未被覆盖。")
            if offset != received:
                raise ApiError(409, "offset_conflict", f"需要从中央已接收位置 {received} 继续上传。")
            handle.seek(offset)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return received + len(body)

    def finalize(self, execution_uuid, size: int, digest: str, progress=None) -> bool:
        raw = self.path(execution_uuid, "raw.csv")
        part = self.path(execution_uuid, "upload.part")
        # Also handles rename succeeding immediately before a lost DB commit.
        source = raw if raw.exists() else part
        if not source.is_file() or source.stat().st_size != size:
            raise ApiError(409, "incomplete_upload", "原始文件尚未完整接收。")
        if sha256_file(source,progress) != digest:
            if source == raw:
                raise ApiError(409, "archive_corrupt", "已归档原始文件哈希不符，必须从一致备份恢复。")
            # Keep the failed transfer until the worker's CORRUPT/received=0
            # receipt commits. A crash before that commit can safely recheck
            # these same bytes; append() removes the tail after DB reset.
            return False
        if source != raw:
            os.chmod(source, 0o440)
            os.replace(source, raw)
            fsync_directory(raw.parent)
        return True
