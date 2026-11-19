"""Byte snapshots for isolated goal packages and their merge workspace."""

import os
import re
from hashlib import sha256
from pathlib import Path

from .goal_checks import (
    MAX_ENTRIES,
    MAX_WORKSPACE_BYTES,
    _fold,
    _open_root,
    _path,
    _snapshot,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _layout(paths):
    """Validate complete path prefixes, including aliases of implicit directories."""
    occupied = {}
    directories = set()
    for path in paths:
        _path(path)
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            directory = length < len(parts)
            key = _fold(prefix)
            previous = occupied.setdefault(key, (prefix, directory))
            if previous != (prefix, directory):
                raise ValueError("file paths have conflicting or aliased prefixes")
            if directory:
                directories.add(prefix)
    if len(occupied) > MAX_ENTRIES:
        raise ValueError("package exceeds its entry limit")
    return sorted(directories, key=lambda path: (path.count("/"), path))


def _manifest(files):
    return {path: sha256(data).hexdigest() for path, data in sorted(files.items())}


def _validate_manifest(manifest):
    if type(manifest) is not dict:
        raise ValueError("expected a SHA-256 manifest")
    _layout(manifest)
    if any(type(value) is not str or not _SHA256.fullmatch(value)
           for value in manifest.values()):
        raise ValueError("manifest contains an invalid SHA-256 digest")


def _read(source):
    files, occupied = _snapshot(source)
    for path, directory in occupied.items():
        _path(path)
        if not directory and path not in files:
            raise ValueError("source contains a non-regular, linked, or oversized file")
    return files


def _target(target, sources):
    target = target.absolute()
    if not target.name or ".." in target.parts:
        raise ValueError("invalid package target")
    key = _fold(str(target))
    for source in sources:
        source_key = _fold(str(source.absolute())).rstrip("/")
        if key == source_key or key.startswith(source_key + "/"):
            raise ValueError("package target must be outside its source trees")
    return target


def _copy(files, target):
    directories = _layout(files)
    if sum(map(len, files.values())) > MAX_WORKSPACE_BYTES:
        raise ValueError("merged workspace exceeds its size limit")

    parent = _open_root(target.parent)
    try:
        # 独占创建，避免覆盖现有目录或跟随符号链接。
        os.mkdir(target.name, mode=0o700, dir_fd=parent)
        created = []
        try:
            fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent)
            try:
                for path in directories:
                    os.mkdir(path, mode=0o700, dir_fd=fd)
                    created.append((path, os.rmdir))
                for path, data in files.items():
                    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    created.append((path, os.unlink))
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(data)
            except BaseException:
                for path, remove in reversed(created):
                    remove(path, dir_fd=fd)
                raise
            finally:
                os.close(fd)
        except BaseException:
            os.rmdir(target.name, dir_fd=parent)
            raise
    finally:
        os.close(parent)


def seed(base: Path, target: Path,
         inputs: list[tuple[Path, dict[str, str]]]) -> dict[str, str]:
    """Copy the base and overlay exactly the verified files in each input, in order."""
    target = _target(target, [base, *(source for source, _ in inputs)])
    files = _read(base)
    for source, expected in inputs:
        _validate_manifest(expected)
        accepted = _read(source)
        if _manifest(accepted) != expected:
            raise ValueError("accepted output snapshot does not match its manifest")
        files.update(accepted)
        _layout(files)
        if sum(map(len, files.values())) > MAX_WORKSPACE_BYTES:
            raise ValueError("merged workspace exceeds its size limit")
    _copy(files, target)
    return _manifest(files)


def freeze_package(candidate: Path, target: Path, baseline: dict[str, str],
                   outputs: list[str]) -> dict[str, str]:
    """Reject changes outside the declared outputs and copy only those outputs."""
    target = _target(target, [candidate])
    _validate_manifest(baseline)
    if type(outputs) is not list:
        raise ValueError("expected a list of output paths")
    _layout(outputs)
    allowed = set(outputs)
    if len(allowed) != len(outputs):
        raise ValueError("duplicate output path")
    _layout([*baseline, *outputs])
    files = _read(candidate)
    actual = _manifest(files)
    if not allowed.issubset(actual):
        raise ValueError("package is missing a declared output")
    if ({path: value for path, value in actual.items() if path not in allowed}
            != {path: value for path, value in baseline.items() if path not in allowed}):
        raise ValueError("package changed files outside its declared outputs")
    _copy({path: files[path] for path in outputs}, target)
    return {path: actual[path] for path in sorted(allowed)}
