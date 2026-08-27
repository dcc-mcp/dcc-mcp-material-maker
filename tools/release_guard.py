"""Fail-closed release identity and distribution provenance checks.

The release workflow builds once, records an immutable local manifest and a
GitHub release snapshot, then replays these checks immediately before each
external publication mutation.
"""

from __future__ import annotations

import argparse
import dataclasses
import email.policy
import hashlib
import json
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import tarfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(
    r"^v(?P<version>0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:[-+][0-9A-Za-z.-]+)?$"
)
ARCHIVE_MAX_MEMBERS = 10_000
ARCHIVE_MAX_METADATA_SIZE = 1024 * 1024
ARCHIVE_MAX_MEMBER_SIZE = 16 * 1024 * 1024
ARCHIVE_MAX_TOTAL_SIZE = 64 * 1024 * 1024
ARCHIVE_MAX_COMPRESSION_RATIO = 100
ROLLBACK_MAX_ATTEMPTS = 3
APPROVED_REQUIRES_PYTHON = ">=3.9"
APPROVED_REQUIRES_DIST = frozenset(
    {
        "dcc-mcp-core<1.0.0,>=0.20.14",
        "build>=1.2; extra == 'dev'",
        "jsonschema<5,>=4.17; extra == 'dev'",
        "pytest>=8; extra == 'dev'",
        "pyyaml<7,>=6; extra == 'dev'",
        "ruff>=0.8; extra == 'dev'",
        "tomli<3,>=2; (python_version < '3.11') and extra == 'dev'",
        "twine>=7.0; (python_version >= '3.10') and extra == 'dev'",
    }
)


class ReleaseContractError(RuntimeError):
    """Raised when release identity or artifact provenance is not exact."""


@dataclasses.dataclass(frozen=True)
class ReleaseSnapshot:
    schema_version: int
    repository: str
    tag: str
    source_sha: str
    release_id: int
    release_node_id: str
    release_tag: str
    release_target: str
    draft: bool
    prerelease: bool
    immutable: bool
    created_at: str
    published_at: str
    release_url: str
    upload_url: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ReleaseSnapshot":
        expected = {field.name for field in dataclasses.fields(cls)}
        if set(payload) != expected:
            raise ReleaseContractError("snapshot schema mismatch")
        try:
            snapshot = cls(**payload)
        except TypeError as error:
            raise ReleaseContractError("snapshot schema mismatch") from error
        _validate_repository(snapshot.repository)
        _validate_tag(snapshot.tag)
        _validate_source_sha(snapshot.source_sha)
        string_fields = (
            snapshot.repository,
            snapshot.tag,
            snapshot.source_sha,
            snapshot.release_node_id,
            snapshot.release_tag,
            snapshot.release_target,
            snapshot.created_at,
            snapshot.published_at,
            snapshot.release_url,
            snapshot.upload_url,
        )
        if (
            type(snapshot.schema_version) is not int
            or snapshot.schema_version != 2
            or type(snapshot.release_id) is not int
            or snapshot.release_id <= 0
            or type(snapshot.draft) is not bool
            or type(snapshot.prerelease) is not bool
            or type(snapshot.immutable) is not bool
            or not snapshot.release_node_id
            or any(type(value) is not str for value in string_fields)
        ):
            raise ReleaseContractError("snapshot schema mismatch")
        return snapshot


@dataclasses.dataclass(frozen=True)
class ArtifactBinding:
    repository: str
    artifact_id: int
    artifact_digest: str
    source_sha: str
    run_id: int
    name: str


def _canonical_json(payload: object) -> bytes:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_repository(repository: str) -> None:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ReleaseContractError("invalid repository identity")


def _validate_tag(tag: str) -> str:
    if TAG_RE.fullmatch(tag) is None:
        raise ReleaseContractError("invalid release tag")
    return tag[1:]


def _validate_source_sha(source_sha: str) -> None:
    if not GIT_SHA_RE.fullmatch(source_sha):
        raise ReleaseContractError("invalid source SHA")


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse)


def _normalized_distribution_name(project: str) -> str:
    return re.sub(r"[-_.]+", "_", project)


def _canonical_project_name(project: str) -> str:
    return re.sub(r"[-_.]+", "-", project).lower()


def _validate_metadata(payload: bytes, *, project: str, version: str) -> None:
    if not payload or len(payload) > 1024 * 1024:
        raise ReleaseContractError("distribution metadata mismatch")
    try:
        message = BytesParser(policy=email.policy.compat32).parsebytes(payload)
    except Exception as error:
        raise ReleaseContractError("distribution metadata mismatch") from error
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    requires_python = message.get_all("Requires-Python", [])
    requires_dist = [str(requirement) for requirement in message.get_all("Requires-Dist", [])]
    if (
        len(names) != 1
        or len(versions) != 1
        or requires_python != [APPROVED_REQUIRES_PYTHON]
        or len(requires_dist) != len(APPROVED_REQUIRES_DIST)
        or set(requires_dist) != APPROVED_REQUIRES_DIST
        or _canonical_project_name(str(names[0])) != _canonical_project_name(project)
        or str(versions[0]) != version
    ):
        raise ReleaseContractError("distribution metadata mismatch")


_WINDOWS_FORBIDDEN_MEMBER_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_MEMBER_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def _canonical_archive_member_key(name: str) -> str:
    """Return one portable comparison key or reject an unsafe member path."""

    canonical = name[:-1] if name.endswith("/") else name
    if not canonical or name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise ReleaseContractError("distribution archive mismatch")

    key_parts: list[str] = []
    for part in canonical.split("/"):
        normalized = unicodedata.normalize("NFKC", part)
        key = unicodedata.normalize("NFKC", normalized.casefold())
        if (
            part in {"", ".", ".."}
            or normalized in {"", ".", ".."}
            or key in {"", ".", ".."}
            or any(separator in normalized or separator in key for separator in ("/", "\\"))
            or part.endswith((".", " "))
            or normalized.endswith((".", " "))
            or key.endswith((".", " "))
            or any(
                unicodedata.category(character).startswith("C")
                or character in _WINDOWS_FORBIDDEN_MEMBER_CHARACTERS
                for character in normalized
            )
            or key.split(".", 1)[0] in _WINDOWS_RESERVED_MEMBER_NAMES
        ):
            raise ReleaseContractError("distribution archive mismatch")
        key_parts.append(key)
    return "/".join(key_parts)


@dataclasses.dataclass(frozen=True)
class _ArchiveMember:
    path: str
    size: int
    compressed_size: int | None
    is_file: bool
    is_dir: bool
    source: object


def _validate_archive_members(
    members: Iterable[_ArchiveMember],
    *,
    archive_size: int,
    expected_metadata: str,
    metadata_basename: str,
    expected_root: str,
    require_single_root: bool,
    reserved_root_suffix: str | None = None,
) -> tuple[_ArchiveMember, list[_ArchiveMember]]:
    """Validate every archive member before returning the unique metadata member."""

    if archive_size <= 0:
        raise ReleaseContractError("distribution archive mismatch")
    member_types: dict[str, bool] = {}
    validated_members: list[_ArchiveMember] = []
    metadata_members: list[_ArchiveMember] = []
    total_size = 0
    member_count = 0
    for member in members:
        member_count += 1
        if member_count > ARCHIVE_MAX_MEMBERS:
            raise ReleaseContractError("distribution archive mismatch")
        member_key = _canonical_archive_member_key(member.path)
        canonical_path = member.path.rstrip("/")
        if member_key in member_types or member.is_file == member.is_dir:
            raise ReleaseContractError("distribution archive mismatch")
        member_types[member_key] = member.is_file
        validated_members.append(member)

        root = canonical_path.split("/", 1)[0]
        if require_single_root and root != expected_root:
            raise ReleaseContractError("distribution archive mismatch")
        root_key = member_key.split("/", 1)[0]
        if (
            reserved_root_suffix
            and root_key.endswith(unicodedata.normalize("NFKC", reserved_root_suffix).casefold())
            and root != expected_root
        ):
            raise ReleaseContractError("distribution archive mismatch")

        if member.size < 0 or member.size > ARCHIVE_MAX_MEMBER_SIZE:
            raise ReleaseContractError("distribution archive mismatch")
        if member.compressed_size is not None and (
            member.compressed_size < 0
            or (
                member.size > 0
                and (
                    member.compressed_size == 0
                    or member.size > member.compressed_size * ARCHIVE_MAX_COMPRESSION_RATIO
                )
            )
        ):
            raise ReleaseContractError("distribution archive mismatch")
        total_size += member.size
        if total_size > ARCHIVE_MAX_TOTAL_SIZE:
            raise ReleaseContractError("distribution archive mismatch")
        if canonical_path.rsplit("/", 1)[-1] == metadata_basename:
            metadata_members.append(member)

    file_keys = {key for key, is_file in member_types.items() if is_file}
    for member_key in member_types:
        parent = member_key
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in file_keys:
                raise ReleaseContractError("distribution archive mismatch")

    if member_count == 0 or total_size > archive_size * ARCHIVE_MAX_COMPRESSION_RATIO:
        raise ReleaseContractError("distribution archive mismatch")
    if (
        len(metadata_members) != 1
        or metadata_members[0].path != expected_metadata
        or not metadata_members[0].is_file
        or metadata_members[0].size > ARCHIVE_MAX_METADATA_SIZE
    ):
        raise ReleaseContractError("distribution archive mismatch")
    return metadata_members[0], validated_members


def _read_regular_member(handle: Any, *, expected_size: int, capture: bool) -> bytes:
    payload = bytearray()
    total = 0
    while True:
        chunk = handle.read(min(1024 * 1024, expected_size - total + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ReleaseContractError("distribution archive mismatch")
        total += len(chunk)
        if total > expected_size:
            raise ReleaseContractError("distribution archive mismatch")
        if capture:
            payload.extend(chunk)
    if total != expected_size:
        raise ReleaseContractError("distribution archive mismatch")
    return bytes(payload)


def _strict_utf8_archive_name(raw_name: bytes, expected_name: str | None = None) -> str:
    try:
        decoded = raw_name.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ReleaseContractError("distribution archive mismatch") from error
    _canonical_archive_member_key(decoded)
    if expected_name is not None and decoded != expected_name:
        raise ReleaseContractError("distribution archive mismatch")
    return decoded


def _wheel_end_record(handle: Any, archive_size: int) -> tuple[int, tuple[object, ...]]:
    tail_size = min(archive_size, 22 + 65_535)
    handle.seek(archive_size - tail_size)
    tail = handle.read(tail_size)
    search_end = len(tail)
    while search_end:
        offset = tail.rfind(b"PK\x05\x06", 0, search_end)
        if offset < 0:
            break
        if offset + 22 <= len(tail):
            fields = struct.unpack("<4s4H2IH", tail[offset : offset + 22])
            if offset + 22 + fields[7] == len(tail):
                return archive_size - tail_size + offset, fields
        search_end = offset
    raise ReleaseContractError("distribution archive mismatch")


def _preflight_wheel_raw_names(path: Path) -> list[tuple[str, int]]:
    archive_size = path.stat().st_size
    if archive_size < 22:
        raise ReleaseContractError("distribution archive mismatch")
    try:
        with path.open("rb") as handle:
            end_offset, end_fields = _wheel_end_record(handle, archive_size)
            disk_number, central_disk, disk_entries, total_entries = end_fields[1:5]
            central_size, central_offset = end_fields[5:7]
            if (
                disk_number != 0
                or central_disk != 0
                or disk_entries != total_entries
                or total_entries > ARCHIVE_MAX_MEMBERS
                or total_entries == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
                or central_offset + central_size != end_offset
            ):
                raise ReleaseContractError("distribution archive mismatch")

            handle.seek(central_offset)
            members: list[tuple[str, int]] = []
            for _ in range(total_entries):
                fixed = handle.read(46)
                if len(fixed) != 46:
                    raise ReleaseContractError("distribution archive mismatch")
                fields = struct.unpack("<4s6H3I5H2I", fixed)
                name_length, extra_length, comment_length = fields[10:13]
                raw_name = handle.read(name_length)
                extra = handle.read(extra_length)
                comment = handle.read(comment_length)
                if (
                    fields[0] != b"PK\x01\x02"
                    or fields[13] != 0
                    or fields[16] == 0xFFFFFFFF
                    or len(raw_name) != name_length
                    or len(extra) != extra_length
                    or len(comment) != comment_length
                ):
                    raise ReleaseContractError("distribution archive mismatch")
                members.append((_strict_utf8_archive_name(raw_name), fields[16]))
            if handle.tell() != central_offset + central_size:
                raise ReleaseContractError("distribution archive mismatch")

            for decoded_name, local_offset in members:
                handle.seek(local_offset)
                fixed = handle.read(30)
                if len(fixed) != 30:
                    raise ReleaseContractError("distribution archive mismatch")
                local_fields = struct.unpack("<4s5H3I2H", fixed)
                name_length, extra_length = local_fields[9:11]
                raw_name = handle.read(name_length)
                extra = handle.read(extra_length)
                if (
                    local_fields[0] != b"PK\x03\x04"
                    or len(raw_name) != name_length
                    or len(extra) != extra_length
                ):
                    raise ReleaseContractError("distribution archive mismatch")
                _strict_utf8_archive_name(raw_name, decoded_name)
            return members
    except (OSError, struct.error) as error:
        raise ReleaseContractError("distribution archive mismatch") from error


def _bind_wheel_raw_names(
    archive: zipfile.ZipFile, raw_members: list[tuple[str, int]]
) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) != len(raw_members):
        raise ReleaseContractError("distribution archive mismatch")
    for info, (raw_name, local_offset) in zip(infos, raw_members):
        if info.orig_filename != raw_name or info.header_offset != local_offset:
            raise ReleaseContractError("distribution archive mismatch")
    return infos


def _validate_wheel_local_header(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    handle = archive.fp
    if handle is None:
        raise ReleaseContractError("distribution archive mismatch")
    previous_offset = handle.tell()
    try:
        handle.seek(info.header_offset)
        fixed = handle.read(30)
        if len(fixed) != 30:
            raise ReleaseContractError("distribution archive mismatch")
        (
            signature,
            _extract_version,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            size,
            name_length,
            extra_length,
        ) = struct.unpack("<4s5H3I2H", fixed)
        local_name = handle.read(name_length)
        local_extra = handle.read(extra_length)
        if (
            signature != b"PK\x03\x04"
            or len(local_name) != name_length
            or len(local_extra) != extra_length
            or flags != info.flag_bits
            or compression != info.compress_type
        ):
            raise ReleaseContractError("distribution archive mismatch")
        _strict_utf8_archive_name(local_name, info.orig_filename)
        uses_data_descriptor = bool(flags & 0x08)
        if uses_data_descriptor:
            if (
                crc not in {0, info.CRC}
                or compressed_size not in {0, info.compress_size}
                or size not in {0, info.file_size}
            ):
                raise ReleaseContractError("distribution archive mismatch")
        elif (crc, compressed_size, size) != (info.CRC, info.compress_size, info.file_size):
            raise ReleaseContractError("distribution archive mismatch")
    except (OSError, struct.error) as error:
        raise ReleaseContractError("distribution archive mismatch") from error
    finally:
        handle.seek(previous_offset)


def _wheel_member(info: zipfile.ZipInfo) -> _ArchiveMember:
    mode_type = stat.S_IFMT(info.external_attr >> 16)
    is_dir = info.is_dir() and mode_type in {0, stat.S_IFDIR}
    is_file = not info.is_dir() and mode_type in {0, stat.S_IFREG}
    return _ArchiveMember(
        path=info.orig_filename,
        size=info.file_size,
        compressed_size=info.compress_size,
        is_file=is_file,
        is_dir=is_dir,
        source=info,
    )


def _validate_wheel_metadata(path: Path, *, project: str, version: str) -> None:
    expected_root = f"{_normalized_distribution_name(project)}-{version}.dist-info"
    expected_metadata = f"{expected_root}/METADATA"
    try:
        raw_members = _preflight_wheel_raw_names(path)
        with zipfile.ZipFile(path) as archive:
            infos = _bind_wheel_raw_names(archive, raw_members)
            metadata, members = _validate_archive_members(
                (_wheel_member(info) for info in infos),
                archive_size=path.stat().st_size,
                expected_metadata=expected_metadata,
                metadata_basename="METADATA",
                expected_root=expected_root,
                require_single_root=False,
                reserved_root_suffix=".dist-info",
            )
            payload: bytes | None = None
            for member in members:
                info = member.source
                if not isinstance(info, zipfile.ZipInfo):
                    raise ReleaseContractError("distribution archive mismatch")
                _validate_wheel_local_header(archive, info)
                if member.is_file:
                    with archive.open(info) as handle:
                        member_payload = _read_regular_member(
                            handle,
                            expected_size=member.size,
                            capture=member is metadata,
                        )
                    if member is metadata:
                        payload = member_payload
    except (OSError, KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as error:
        raise ReleaseContractError("distribution archive mismatch") from error
    if payload is None:
        raise ReleaseContractError("distribution archive mismatch")
    _validate_metadata(payload, project=project, version=version)


def _validate_sdist_metadata(path: Path, *, project: str, version: str) -> None:
    expected_root = f"{_normalized_distribution_name(project)}-{version}"
    expected_metadata = f"{expected_root}/PKG-INFO"
    try:
        with tarfile.open(path, "r:gz", encoding="utf-8", errors="strict") as archive:
            metadata, members = _validate_archive_members(
                (
                    _ArchiveMember(
                        path=member.name,
                        size=member.size,
                        compressed_size=None,
                        is_file=member.isfile(),
                        is_dir=member.isdir(),
                        source=member,
                    )
                    for member in archive
                ),
                archive_size=path.stat().st_size,
                expected_metadata=expected_metadata,
                metadata_basename="PKG-INFO",
                expected_root=expected_root,
                require_single_root=True,
            )
            payload: bytes | None = None
            for member in members:
                if not member.is_file:
                    continue
                handle = archive.extractfile(member.source)
                if handle is None:
                    raise ReleaseContractError("distribution archive mismatch")
                with handle:
                    member_payload = _read_regular_member(
                        handle,
                        expected_size=member.size,
                        capture=member is metadata,
                    )
                if member is metadata:
                    payload = member_payload
    except (OSError, EOFError, UnicodeError, tarfile.TarError) as error:
        raise ReleaseContractError("distribution archive mismatch") from error
    if payload is None:
        raise ReleaseContractError("distribution archive mismatch")
    _validate_metadata(payload, project=project, version=version)


def create_manifest(dist_dir: Path, *, project: str, version: str) -> dict[str, object]:
    """Create a canonical manifest for exactly one wheel and one sdist."""

    if not dist_dir.is_dir() or _is_link_or_reparse(dist_dir):
        raise ReleaseContractError("distribution directory is missing or unsafe")
    normalized = _normalized_distribution_name(project)
    entries = sorted(dist_dir.iterdir(), key=lambda item: item.name)
    if any(not entry.is_file() or _is_link_or_reparse(entry) for entry in entries):
        raise ReleaseContractError("unexpected distribution files")

    wheels = [entry for entry in entries if entry.suffix == ".whl"]
    sdists = [entry for entry in entries if entry.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseContractError("expected exactly one wheel and one sdist")
    expected_wheel_prefix = f"{normalized}-{version}-"
    expected_sdist = f"{normalized}-{version}.tar.gz"
    if not wheels[0].name.startswith(expected_wheel_prefix) or sdists[0].name != expected_sdist:
        raise ReleaseContractError("distribution identity does not match project version")
    if set(entries) != {wheels[0], sdists[0]}:
        raise ReleaseContractError("unexpected distribution files")

    _validate_wheel_metadata(wheels[0], project=project, version=version)
    _validate_sdist_metadata(sdists[0], project=project, version=version)

    artifacts = [
        {
            "filename": path.name,
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in entries
    ]
    return {
        "schema_version": 1,
        "project": project,
        "version": version,
        "artifacts": artifacts,
    }


def _write_json_exclusive(payload: object, path: Path) -> str:
    data = _canonical_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise ReleaseContractError(f"refusing to overwrite {path.name}") from error
    return _sha256_bytes(data)


def write_manifest(manifest: dict[str, object], path: Path) -> str:
    return _write_json_exclusive(manifest, path)


def _load_canonical_json(path: Path, expected_sha256: str, *, label: str) -> dict[str, object]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ReleaseContractError(f"invalid expected {label} SHA-256")
    if not path.is_file() or _is_link_or_reparse(path):
        raise ReleaseContractError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    if _sha256_bytes(raw) != expected_sha256:
        raise ReleaseContractError(f"{label} SHA-256 mismatch")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseContractError(f"invalid {label} JSON") from error
    if not isinstance(payload, dict) or raw != _canonical_json(payload):
        raise ReleaseContractError(f"noncanonical {label} JSON")
    return payload


def verify_manifest(manifest_path: Path, dist_dir: Path, expected_sha256: str) -> dict[str, object]:
    manifest = _load_canonical_json(manifest_path, expected_sha256, label="manifest")
    expected_keys = {"schema_version", "project", "version", "artifacts"}
    if set(manifest) != expected_keys or manifest.get("schema_version") != 1:
        raise ReleaseContractError("manifest schema mismatch")
    project = manifest.get("project")
    version = manifest.get("version")
    if not isinstance(project, str) or not isinstance(version, str):
        raise ReleaseContractError("manifest schema mismatch")
    expected_artifacts = _expected_assets(manifest)
    if not dist_dir.is_dir() or _is_link_or_reparse(dist_dir):
        raise ReleaseContractError("distribution directory is missing or unsafe")
    entries = sorted(dist_dir.iterdir(), key=lambda item: item.name)
    if {entry.name for entry in entries} != set(expected_artifacts):
        raise ReleaseContractError("artifact digest mismatch")
    for entry in entries:
        expected = expected_artifacts[entry.name]
        if (
            not entry.is_file()
            or _is_link_or_reparse(entry)
            or entry.stat().st_size != expected.get("size")
            or _sha256_file(entry) != expected.get("sha256")
        ):
            raise ReleaseContractError("artifact digest mismatch")
    rebuilt = create_manifest(dist_dir, project=project, version=version)
    if manifest != rebuilt:
        raise ReleaseContractError("artifact digest mismatch")
    return manifest


def _require_release_field(payload: dict[str, object], field: str, expected_type: type) -> Any:
    value = payload.get(field)
    if type(value) is not expected_type:
        raise ReleaseContractError(f"invalid release {field}")
    return value


def _validate_ref(ref_payload: dict[str, object], *, tag: str, source_sha: str) -> None:
    if ref_payload.get("ref") != f"refs/tags/{tag}":
        raise ReleaseContractError("tag identity drift")
    tag_object = ref_payload.get("object")
    if not isinstance(tag_object, dict) or tag_object.get("type") != "commit":
        raise ReleaseContractError("release requires a lightweight commit tag")
    if tag_object.get("sha") != source_sha:
        raise ReleaseContractError("tag source drift")


def _snapshot_from_release(
    *,
    repository: str,
    tag: str,
    source_sha: str,
    release_payload: dict[str, object],
) -> tuple[ReleaseSnapshot, list[object]]:
    release_id = _require_release_field(release_payload, "id", int)
    release_node_id = _require_release_field(release_payload, "node_id", str)
    release_tag = _require_release_field(release_payload, "tag_name", str)
    release_target = _require_release_field(release_payload, "target_commitish", str)
    draft = _require_release_field(release_payload, "draft", bool)
    prerelease = _require_release_field(release_payload, "prerelease", bool)
    immutable = _require_release_field(release_payload, "immutable", bool)
    created_at = _require_release_field(release_payload, "created_at", str)
    published_at = _require_release_field(release_payload, "published_at", str)
    release_url = _require_release_field(release_payload, "url", str)
    upload_url = _require_release_field(release_payload, "upload_url", str)
    assets = _require_release_field(release_payload, "assets", list)
    if release_id <= 0 or not release_node_id or release_tag != tag:
        raise ReleaseContractError("release identity drift")
    if release_target != source_sha:
        raise ReleaseContractError("release source drift")
    if draft or prerelease or immutable or not created_at or not published_at:
        raise ReleaseContractError("release state drift")
    expected_release_url = f"https://api.github.com/repos/{repository}/releases/{release_id}"
    expected_upload_url = (
        f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets{{?name,label}}"
    )
    if release_url != expected_release_url or upload_url != expected_upload_url:
        raise ReleaseContractError("release state drift")
    return (
        ReleaseSnapshot(
            schema_version=2,
            repository=repository,
            tag=tag,
            source_sha=source_sha,
            release_id=release_id,
            release_node_id=release_node_id,
            release_tag=release_tag,
            release_target=release_target,
            draft=draft,
            prerelease=prerelease,
            immutable=immutable,
            created_at=created_at,
            published_at=published_at,
            release_url=release_url,
            upload_url=upload_url,
        ),
        assets,
    )


def capture_snapshot(
    *,
    repository: str,
    tag: str,
    source_sha: str,
    ref_payload: dict[str, object],
    release_payload: dict[str, object],
) -> ReleaseSnapshot:
    _validate_repository(repository)
    _validate_tag(tag)
    _validate_source_sha(source_sha)
    _validate_ref(ref_payload, tag=tag, source_sha=source_sha)
    snapshot, assets = _snapshot_from_release(
        repository=repository,
        tag=tag,
        source_sha=source_sha,
        release_payload=release_payload,
    )
    if assets:
        raise ReleaseContractError("release assets must be empty before publication")
    return snapshot


def verify_snapshot(
    snapshot: ReleaseSnapshot,
    *,
    ref_payload: dict[str, object],
    release_payload: dict[str, object],
    expect_no_assets: bool,
) -> None:
    _validate_ref(ref_payload, tag=snapshot.tag, source_sha=snapshot.source_sha)
    current, assets = _snapshot_from_release(
        repository=snapshot.repository,
        tag=snapshot.tag,
        source_sha=snapshot.source_sha,
        release_payload=release_payload,
    )
    if current.release_id != snapshot.release_id:
        raise ReleaseContractError("release identity drift")
    if current != snapshot:
        raise ReleaseContractError("release state drift")
    if expect_no_assets and assets:
        raise ReleaseContractError("release assets must be empty before publication")


def verify_published_assets(
    snapshot: ReleaseSnapshot,
    manifest: dict[str, object],
    *,
    ref_payload: dict[str, object],
    release_payload: dict[str, object],
) -> None:
    verify_snapshot(
        snapshot,
        ref_payload=ref_payload,
        release_payload=release_payload,
        expect_no_assets=False,
    )
    artifacts = manifest.get("artifacts")
    assets = release_payload.get("assets")
    if not isinstance(artifacts, list) or not isinstance(assets, list):
        raise ReleaseContractError("published asset set mismatch")
    expected = {entry["filename"]: entry for entry in artifacts if isinstance(entry, dict)}
    actual = {asset.get("name"): asset for asset in assets if isinstance(asset, dict)}
    if (
        len(expected) != len(artifacts)
        or len(actual) != len(assets)
        or set(actual) != set(expected)
    ):
        raise ReleaseContractError("published asset set mismatch")
    for filename, entry in expected.items():
        asset = actual[filename]
        if (
            asset.get("state") != "uploaded"
            or asset.get("size") != entry.get("size")
            or asset.get("digest") != f"sha256:{entry.get('sha256')}"
        ):
            raise ReleaseContractError(f"published asset mismatch: {filename}")


def _expected_assets(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("manifest schema mismatch")
    expected: dict[str, dict[str, object]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"filename", "sha256", "size"}:
            raise ReleaseContractError("manifest schema mismatch")
        filename = artifact.get("filename")
        if not isinstance(filename, str) or filename in expected:
            raise ReleaseContractError("manifest schema mismatch")
        expected[filename] = artifact
    return expected


def _assert_transaction_assets(
    release_payload: dict[str, object], uploaded: list[dict[str, object]]
) -> None:
    assets = release_payload.get("assets")
    if not isinstance(assets, list) or assets != uploaded:
        raise ReleaseContractError("release asset set drift")


def _validate_uploaded_asset(
    asset: dict[str, object], expected: dict[str, object], ownership_marker: str
) -> dict[str, object]:
    if (
        type(asset.get("id")) is not int
        or asset["id"] <= 0
        or asset.get("name") != expected.get("filename")
        or asset.get("size") != expected.get("size")
        or asset.get("state") != "uploaded"
        or asset.get("digest") != f"sha256:{expected.get('sha256')}"
        or asset.get("label") != ownership_marker
    ):
        raise ReleaseContractError(f"uploaded asset mismatch: {expected.get('filename')}")
    return asset


def _record_uploaded_asset(asset: dict[str, object]) -> dict[str, object]:
    if type(asset.get("id")) is not int or asset["id"] <= 0:
        raise ReleaseContractError("uploaded asset identity is missing")
    return dict(asset)


_OWNED_ASSET_IDENTITY_FIELDS = ("id", "name", "label", "size", "state", "digest")


def _asset_identity_matches(current: object, recorded: dict[str, object]) -> bool:
    return isinstance(current, dict) and all(
        current.get(field) == recorded.get(field) for field in _OWNED_ASSET_IDENTITY_FIELDS
    )


def _asset_matches_expected(
    asset: object, expected: dict[str, object], ownership_marker: str
) -> bool:
    return (
        isinstance(asset, dict)
        and type(asset.get("id")) is int
        and asset["id"] > 0
        and asset.get("name") == expected.get("filename")
        and asset.get("size") == expected.get("size")
        and asset.get("state") == "uploaded"
        and asset.get("digest") == f"sha256:{expected.get('sha256')}"
        and asset.get("label") == ownership_marker
    )


def _ownership_marker(transaction_id: str, index: int, expected: dict[str, object]) -> str:
    digest = expected.get("sha256")
    if not re.fullmatch(r"[0-9a-f]{32}", transaction_id) or not SHA256_RE.fullmatch(str(digest)):
        raise ReleaseContractError("invalid asset ownership marker")
    return f"dcc-mcp-tx-{transaction_id}-{index}-{str(digest)[:12]}"


def _recapture_release_assets(snapshot: ReleaseSnapshot, github: Any) -> list[object]:
    ref_payload, release_payload = github.recapture_release(snapshot)
    verify_snapshot(
        snapshot,
        ref_payload=ref_payload,
        release_payload=release_payload,
        expect_no_assets=False,
    )
    assets = release_payload.get("assets")
    if not isinstance(assets, list):
        raise ReleaseContractError("release asset set drift")
    return assets


def _discover_pending_assets(
    snapshot: ReleaseSnapshot,
    github: Any,
    expected: dict[str, object],
    ownership_marker: str,
    known_ids: set[int],
) -> tuple[list[dict[str, object]], bool]:
    """Recover only one unambiguous marker-bound POST result."""

    recovered: dict[str, object] | None = None
    complete = True
    saw_successful_recapture = False
    for _ in range(ROLLBACK_MAX_ATTEMPTS):
        try:
            current_assets = _recapture_release_assets(snapshot, github)
        except Exception:
            continue
        saw_successful_recapture = True
        marker_assets = [
            asset
            for asset in current_assets
            if isinstance(asset, dict)
            and asset.get("id") not in known_ids
            and asset.get("label") == ownership_marker
        ]
        if len(marker_assets) > 1:
            complete = False
            continue
        if not marker_assets:
            continue
        asset = marker_assets[0]
        if not _asset_matches_expected(asset, expected, ownership_marker):
            complete = False
            continue
        recorded = _record_uploaded_asset(asset)
        if recovered is not None and not _asset_identity_matches(recorded, recovered):
            complete = False
            continue
        recovered = recorded
    discovery_complete = complete and saw_successful_recapture
    if not discovery_complete or recovered is None:
        return [], discovery_complete
    return [recovered], True


def _rollback_owned_asset(
    snapshot: ReleaseSnapshot, github: Any, recorded: dict[str, object]
) -> bool:
    """Delete one exact owned identity with bounded ambiguous-failure recovery."""

    for _ in range(ROLLBACK_MAX_ATTEMPTS):
        try:
            current_assets = _recapture_release_assets(snapshot, github)
        except Exception:
            continue
        matches = [
            current
            for current in current_assets
            if isinstance(current, dict) and current.get("id") == recorded["id"]
        ]
        if not matches:
            return True
        if len(matches) != 1 or not _asset_identity_matches(matches[0], recorded):
            return False
        try:
            github.delete_asset(snapshot.release_id, recorded["id"])
            after_delete = _recapture_release_assets(snapshot, github)
        except Exception:
            continue
        remaining = [
            current
            for current in after_delete
            if isinstance(current, dict) and current.get("id") == recorded["id"]
        ]
        if not remaining:
            return True
        if len(remaining) != 1 or not _asset_identity_matches(remaining[0], recorded):
            return False
    return False


def _rollback_transaction_assets(
    snapshot: ReleaseSnapshot,
    github: Any,
    uploaded: list[dict[str, object]],
    pending: tuple[dict[str, object], str] | None,
    ownership_markers: set[str],
) -> bool:
    """Recover every independently confirmed owned upload without touching contenders."""

    owned = list(uploaded)
    complete = True
    if pending is not None:
        pending_artifact, pending_marker = pending
        recovered, discovery_complete = _discover_pending_assets(
            snapshot,
            github,
            pending_artifact,
            pending_marker,
            {asset["id"] for asset in owned},
        )
        owned.extend(recovered)
        complete = discovery_complete

    for asset in reversed(owned):
        if not _rollback_owned_asset(snapshot, github, asset):
            complete = False

    try:
        remaining_assets = _recapture_release_assets(snapshot, github)
    except Exception:
        return False
    if any(
        isinstance(asset, dict) and asset.get("label") in ownership_markers
        for asset in remaining_assets
    ):
        complete = False
    return complete


def _parse_github_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ReleaseContractError(f"invalid artifact {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseContractError(f"invalid artifact {label}") from error
    if parsed.tzinfo is None:
        raise ReleaseContractError(f"invalid artifact {label}")
    return parsed.astimezone(timezone.utc)


def verify_artifact_metadata(
    metadata: dict[str, object],
    *,
    repository: str,
    artifact_id: int,
    artifact_digest: str,
    source_sha: str,
    run_id: int,
    name: str,
    now: datetime | None = None,
) -> None:
    """Verify one immutable Actions artifact against its server-side provenance."""

    _validate_repository(repository)
    _validate_source_sha(source_sha)
    if type(artifact_id) is not int or artifact_id <= 0 or type(run_id) is not int or run_id <= 0:
        raise ReleaseContractError("invalid artifact identity")
    if not SHA256_RE.fullmatch(artifact_digest):
        raise ReleaseContractError("invalid artifact digest")
    if metadata.get("id") != artifact_id or metadata.get("name") != name:
        raise ReleaseContractError("artifact identity drift")
    if type(metadata.get("size_in_bytes")) is not int or metadata["size_in_bytes"] <= 0:
        raise ReleaseContractError("artifact state drift")
    expected_url = f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}"
    if (
        metadata.get("url") != expected_url
        or metadata.get("archive_download_url") != f"{expected_url}/zip"
    ):
        raise ReleaseContractError("artifact repository drift")
    if metadata.get("expired") is not False:
        raise ReleaseContractError("artifact expired")
    if metadata.get("digest") != f"sha256:{artifact_digest}":
        raise ReleaseContractError("artifact digest drift")

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ReleaseContractError("invalid verification time")
    _parse_github_time(metadata.get("created_at"), label="created_at")
    _parse_github_time(metadata.get("updated_at"), label="updated_at")
    if _parse_github_time(metadata.get("expires_at"), label="expires_at") <= current_time:
        raise ReleaseContractError("artifact expired")

    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, dict) or workflow_run.get("id") != run_id:
        raise ReleaseContractError("artifact workflow run drift")
    repository_id = workflow_run.get("repository_id")
    if (
        type(repository_id) is not int
        or repository_id <= 0
        or workflow_run.get("head_repository_id") != repository_id
    ):
        raise ReleaseContractError("artifact repository drift")
    if workflow_run.get("head_sha") != source_sha:
        raise ReleaseContractError("artifact source drift")
    if workflow_run.get("head_branch") != "main":
        raise ReleaseContractError("artifact source branch drift")


def _recapture_bound_artifact(
    snapshot: ReleaseSnapshot, binding: ArtifactBinding, github: Any
) -> None:
    if binding.repository != snapshot.repository or binding.source_sha != snapshot.source_sha:
        raise ReleaseContractError("artifact release binding drift")
    verify_artifact_metadata(
        github.recapture_artifact(binding.artifact_id),
        repository=binding.repository,
        artifact_id=binding.artifact_id,
        artifact_digest=binding.artifact_digest,
        source_sha=binding.source_sha,
        run_id=binding.run_id,
        name=binding.name,
    )


def publish_assets_transactional(
    snapshot: ReleaseSnapshot,
    manifest: dict[str, object],
    dist_dir: Path,
    github: Any,
    *,
    artifact_binding: ArtifactBinding,
) -> None:
    """Publish every distribution to one numeric Release ID or restore an empty set."""

    project = manifest.get("project")
    version = manifest.get("version")
    if not isinstance(project, str) or not isinstance(version, str):
        raise ReleaseContractError("manifest schema mismatch")
    if create_manifest(dist_dir, project=project, version=version) != manifest:
        raise ReleaseContractError("artifact digest mismatch")
    expected = _expected_assets(manifest)

    ref_payload, release_payload = github.recapture_release(snapshot)
    verify_snapshot(
        snapshot,
        ref_payload=ref_payload,
        release_payload=release_payload,
        expect_no_assets=True,
    )

    uploaded: list[dict[str, object]] = []
    transaction_id = secrets.token_hex(16)
    ownership_markers: set[str] = set()
    pending: tuple[dict[str, object], str] | None = None
    try:
        for index, (filename, artifact) in enumerate(expected.items(), start=1):
            pending = None
            _recapture_bound_artifact(snapshot, artifact_binding, github)
            ref_payload, release_payload = github.recapture_release(snapshot)
            verify_snapshot(
                snapshot,
                ref_payload=ref_payload,
                release_payload=release_payload,
                expect_no_assets=not uploaded,
            )
            _assert_transaction_assets(release_payload, uploaded)
            marker = _ownership_marker(transaction_id, index, artifact)
            ownership_markers.add(marker)
            pending = (artifact, marker)
            response = github.upload_asset(snapshot.release_id, dist_dir / filename, marker)
            validated = _validate_uploaded_asset(response, artifact, marker)
            current_assets = _recapture_release_assets(snapshot, github)
            if current_assets != [*uploaded, validated]:
                raise ReleaseContractError("release asset set drift")
            uploaded.append(_record_uploaded_asset(validated))
            pending = None

        ref_payload, release_payload = github.recapture_release(snapshot)
        verify_published_assets(
            snapshot,
            manifest,
            ref_payload=ref_payload,
            release_payload=release_payload,
        )
    except Exception as error:
        try:
            if not _rollback_transaction_assets(
                snapshot, github, uploaded, pending, ownership_markers
            ):
                raise ReleaseContractError("asset rollback retry exhausted")
        except Exception as rollback_error:
            raise ReleaseContractError(
                "asset publication failed and rollback was incomplete"
            ) from rollback_error
        if isinstance(error, ReleaseContractError):
            raise
        raise ReleaseContractError(f"GitHub asset publication failed: {error}") from error


class GitHubReleaseClient:
    """Bounded GitHub REST client for one repository's numeric release endpoints."""

    def __init__(self, repository: str, token: str) -> None:
        _validate_repository(repository)
        if not token:
            raise ReleaseContractError("GitHub token is required for release publication")
        self.repository = repository
        self._token = token

    def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "dcc-mcp-material-maker-release-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            raise ReleaseContractError(f"GitHub {method} request failed") from error
        if not isinstance(payload, dict):
            raise ReleaseContractError("GitHub response was not an object")
        return payload

    def recapture_release(
        self, snapshot: ReleaseSnapshot
    ) -> tuple[dict[str, object], dict[str, object]]:
        if snapshot.repository != self.repository:
            raise ReleaseContractError("release repository drift")
        encoded_tag = urllib.parse.quote(snapshot.tag, safe="")
        base = f"https://api.github.com/repos/{self.repository}"
        return (
            self._json_request(f"{base}/git/ref/tags/{encoded_tag}"),
            self._json_request(f"{base}/releases/{snapshot.release_id}"),
        )

    def recapture_artifact(self, artifact_id: int) -> dict[str, object]:
        if type(artifact_id) is not int or artifact_id <= 0:
            raise ReleaseContractError("invalid artifact identity")
        url = f"https://api.github.com/repos/{self.repository}/actions/artifacts/{artifact_id}"
        return self._json_request(url)

    def upload_asset(self, release_id: int, path: Path, ownership_marker: str) -> dict[str, object]:
        if type(release_id) is not int or release_id <= 0:
            raise ReleaseContractError("invalid release identity")
        if not path.is_file() or _is_link_or_reparse(path):
            raise ReleaseContractError("distribution asset is missing or unsafe")
        if not re.fullmatch(r"dcc-mcp-tx-[0-9a-f]{32}-\d+-[0-9a-f]{12}", ownership_marker):
            raise ReleaseContractError("invalid asset ownership marker")
        query = urllib.parse.urlencode({"name": path.name, "label": ownership_marker})
        url = (
            f"https://uploads.github.com/repos/{self.repository}/releases/"
            f"{release_id}/assets?{query}"
        )
        return self._json_request(
            url,
            method="POST",
            data=path.read_bytes(),
            content_type="application/octet-stream",
        )

    def delete_asset(self, release_id: int, asset_id: int) -> None:
        if (
            type(release_id) is not int
            or release_id <= 0
            or type(asset_id) is not int
            or asset_id <= 0
        ):
            raise ReleaseContractError("invalid release asset identity")
        url = f"https://api.github.com/repos/{self.repository}/releases/assets/{asset_id}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "dcc-mcp-material-maker-release-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = urllib.request.Request(url, headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            raise ReleaseContractError("GitHub DELETE request failed") from error


def _github_json(repository: str, path: str, token: str) -> dict[str, object]:
    if not token:
        raise ReleaseContractError("GitHub token is required for identity recapture")
    url = f"https://api.github.com/repos/{repository}/{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dcc-mcp-material-maker-release-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise ReleaseContractError(f"GitHub identity recapture failed: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseContractError("GitHub identity response was not an object")
    return payload


def _live_payloads(
    repository: str, tag: str, token: str
) -> tuple[dict[str, object], dict[str, object]]:
    encoded_tag = urllib.parse.quote(tag, safe="")
    return (
        _github_json(repository, f"git/ref/tags/{encoded_tag}", token),
        _github_json(repository, f"releases/tags/{encoded_tag}", token),
    )


def _verify_local_checkout(source_sha: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    if head.returncode or head.stdout.strip() != source_sha:
        raise ReleaseContractError("local checkout source drift")
    for command in (
        ["git", "diff", "--quiet", "HEAD", "--"],
        ["git", "diff", "--cached", "--quiet", "HEAD", "--"],
    ):
        diff = subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
        if diff.returncode:
            raise ReleaseContractError("tracked source drift")


def _write_outputs(values: dict[str, object]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise ReleaseContractError("GITHUB_OUTPUT is required")
    with Path(output_path).open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _load_snapshot(path: Path, expected_sha256: str) -> ReleaseSnapshot:
    return ReleaseSnapshot.from_dict(_load_canonical_json(path, expected_sha256, label="snapshot"))


def _common_live(
    args: argparse.Namespace,
) -> tuple[ReleaseSnapshot, dict[str, object], dict[str, object], dict[str, object]]:
    _verify_local_checkout(args.source_sha)
    manifest = verify_manifest(
        args.state_dir / "manifest.json",
        args.dist_dir,
        args.expected_manifest_sha256,
    )
    snapshot = _load_snapshot(
        args.state_dir / "snapshot.json",
        args.expected_snapshot_sha256,
    )
    if (
        snapshot.repository != args.repository
        or snapshot.tag != args.tag
        or snapshot.source_sha != args.source_sha
        or snapshot.release_id != args.expected_release_id
    ):
        raise ReleaseContractError("trusted release snapshot does not match workflow outputs")
    client = GitHubReleaseClient(args.repository, args.github_token)
    artifact_payload = client.recapture_artifact(args.artifact_id)
    verify_artifact_metadata(
        artifact_payload,
        repository=args.repository,
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        source_sha=args.source_sha,
        run_id=args.run_id,
        name=args.artifact_name,
    )
    ref_payload, release_payload = client.recapture_release(snapshot)
    return snapshot, manifest, ref_payload, release_payload


def _capture_command(args: argparse.Namespace) -> None:
    version = _validate_tag(args.tag)
    _verify_local_checkout(args.source_sha)
    manifest = create_manifest(args.dist_dir, project=args.project, version=version)
    ref_payload, release_payload = _live_payloads(args.repository, args.tag, args.github_token)
    snapshot = capture_snapshot(
        repository=args.repository,
        tag=args.tag,
        source_sha=args.source_sha,
        ref_payload=ref_payload,
        release_payload=release_payload,
    )
    manifest_sha256 = write_manifest(manifest, args.state_dir / "manifest.json")
    snapshot_sha256 = _write_json_exclusive(snapshot.to_dict(), args.state_dir / "snapshot.json")
    _write_outputs(
        {
            "source_sha": snapshot.source_sha,
            "tag_name": snapshot.tag,
            "release_id": snapshot.release_id,
            "manifest_sha256": manifest_sha256,
            "snapshot_sha256": snapshot_sha256,
        }
    )


def _verify_command(args: argparse.Namespace) -> None:
    snapshot, _, ref_payload, release_payload = _common_live(args)
    verify_snapshot(
        snapshot,
        ref_payload=ref_payload,
        release_payload=release_payload,
        expect_no_assets=args.expect_no_assets,
    )


def _verify_assets_command(args: argparse.Namespace) -> None:
    snapshot, manifest, ref_payload, release_payload = _common_live(args)
    verify_published_assets(
        snapshot,
        manifest,
        ref_payload=ref_payload,
        release_payload=release_payload,
    )


def _verify_artifact_command(args: argparse.Namespace) -> None:
    _verify_local_checkout(args.source_sha)
    client = GitHubReleaseClient(args.repository, args.github_token)
    verify_artifact_metadata(
        client.recapture_artifact(args.artifact_id),
        repository=args.repository,
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        source_sha=args.source_sha,
        run_id=args.run_id,
        name=args.artifact_name,
    )


def _publish_assets_command(args: argparse.Namespace) -> None:
    snapshot, manifest, ref_payload, release_payload = _common_live(args)
    verify_snapshot(
        snapshot,
        ref_payload=ref_payload,
        release_payload=release_payload,
        expect_no_assets=True,
    )
    client = GitHubReleaseClient(args.repository, args.github_token)
    publish_assets_transactional(
        snapshot,
        manifest,
        args.dist_dir,
        client,
        artifact_binding=ArtifactBinding(
            repository=args.repository,
            artifact_id=args.artifact_id,
            artifact_digest=args.artifact_digest,
            source_sha=args.source_sha,
            run_id=args.run_id,
            name=args.artifact_name,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repository", required=True)
    common.add_argument("--tag", required=True)
    common.add_argument("--source-sha", required=True)
    common.add_argument("--dist-dir", type=Path, default=Path("dist"))
    common.add_argument("--state-dir", type=Path, default=Path("release"))
    common.add_argument(
        "--github-token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
    )

    capture = subparsers.add_parser("capture", parents=[common])
    capture.add_argument("--project", default="dcc-mcp-material-maker")
    capture.set_defaults(handler=_capture_command)

    verify_common = argparse.ArgumentParser(add_help=False)
    verify_common.add_argument("--expected-release-id", required=True, type=int)
    verify_common.add_argument("--expected-manifest-sha256", required=True)
    verify_common.add_argument("--expected-snapshot-sha256", required=True)
    verify_common.add_argument("--artifact-id", required=True, type=int)
    verify_common.add_argument("--artifact-digest", required=True)
    verify_common.add_argument("--run-id", required=True, type=int)
    verify_common.add_argument("--artifact-name", default="release-distributions")

    verify = subparsers.add_parser("verify", parents=[common, verify_common])
    verify.add_argument("--expect-no-assets", action="store_true", required=True)
    verify.set_defaults(handler=_verify_command)

    verify_assets = subparsers.add_parser("verify-assets", parents=[common, verify_common])
    verify_assets.set_defaults(handler=_verify_assets_command)

    verify_artifact = subparsers.add_parser("verify-artifact")
    verify_artifact.add_argument("--repository", required=True)
    verify_artifact.add_argument("--source-sha", required=True)
    verify_artifact.add_argument("--artifact-id", required=True, type=int)
    verify_artifact.add_argument("--artifact-digest", required=True)
    verify_artifact.add_argument("--run-id", required=True, type=int)
    verify_artifact.add_argument("--artifact-name", default="release-distributions")
    verify_artifact.add_argument(
        "--github-token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
    )
    verify_artifact.set_defaults(handler=_verify_artifact_command)

    publish_assets = subparsers.add_parser("publish-assets", parents=[common, verify_common])
    publish_assets.set_defaults(handler=_publish_assets_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except ReleaseContractError as error:
        print(f"release guard failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
