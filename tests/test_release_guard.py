from __future__ import annotations

import copy
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.release_guard as release_guard
from tools.release_guard import (
    GitHubReleaseClient,
    ReleaseContractError,
    capture_snapshot,
    create_manifest,
    publish_assets_transactional,
    verify_artifact_metadata,
    verify_manifest,
    verify_published_assets,
    verify_snapshot,
    write_manifest,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 compatibility
    import tomli as tomllib

SHA = "1" * 40
TAG = "v0.4.1"
PROJECT = "dcc-mcp-material-maker"
VERSION = "0.4.1"
ROOT = Path(__file__).resolve().parents[1]


def _ref(sha: str = SHA) -> dict[str, object]:
    return {
        "ref": f"refs/tags/{TAG}",
        "object": {"type": "commit", "sha": sha},
    }


def _release(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 12345,
        "node_id": "RE_kwDOTijwzs4WbAoL",
        "tag_name": TAG,
        "target_commitish": SHA,
        "draft": False,
        "prerelease": False,
        "immutable": False,
        "created_at": "2026-08-27T00:00:00Z",
        "published_at": "2026-08-27T00:00:01Z",
        "url": "https://api.github.com/repos/dcc-mcp/dcc-mcp-material-maker/releases/12345",
        "upload_url": (
            "https://uploads.github.com/repos/dcc-mcp/dcc-mcp-material-maker/"
            "releases/12345/assets{?name,label}"
        ),
        "assets": [],
    }
    payload.update(changes)
    return payload


def _metadata(project: str = PROJECT, version: str = VERSION) -> bytes:
    return (f"Metadata-Version: 2.4\nName: {project}\nVersion: {version}\n\n").encode()


def _write_wheel(path: Path, *, project: str = PROJECT, version: str = VERSION) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"dcc_mcp_material_maker-{VERSION}.dist-info/METADATA",
            _metadata(project, version),
        )


def _write_sdist(path: Path, *, project: str = PROJECT, version: str = VERSION) -> None:
    payload = _metadata(project, version)
    member = tarfile.TarInfo(f"dcc_mcp_material_maker-{VERSION}/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))


def _dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    _write_wheel(wheel)
    _write_sdist(sdist)
    return dist


def test_manifest_binds_exact_wheel_and_sdist_bytes(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)

    assert manifest["schema_version"] == 1
    assert manifest["project"] == PROJECT
    assert manifest["version"] == VERSION
    assert [entry["filename"] for entry in manifest["artifacts"]] == [
        "dcc_mcp_material_maker-0.4.1-py3-none-any.whl",
        "dcc_mcp_material_maker-0.4.1.tar.gz",
    ]
    for entry in manifest["artifacts"]:
        path = dist / entry["filename"]
        assert entry["size"] == path.stat().st_size
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    manifest_path = tmp_path / "release" / "manifest.json"
    manifest_sha256 = write_manifest(manifest, manifest_path)
    assert manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert verify_manifest(manifest_path, dist, manifest_sha256) == manifest

    (dist / "dcc_mcp_material_maker-0.4.1.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ReleaseContractError, match="artifact digest mismatch"):
        verify_manifest(manifest_path, dist, manifest_sha256)


@pytest.mark.parametrize(
    "missing_name",
    [
        "dcc_mcp_material_maker-0.4.1-py3-none-any.whl",
        "dcc_mcp_material_maker-0.4.1.tar.gz",
    ],
)
def test_manifest_rejects_missing_or_extra_distribution(tmp_path, missing_name: str) -> None:
    dist = _dist(tmp_path)
    (dist / missing_name).unlink()
    with pytest.raises(ReleaseContractError, match="exactly one wheel and one sdist"):
        create_manifest(dist, project=PROJECT, version=VERSION)

    dist = _dist(tmp_path / "extra")
    (dist / "unexpected.zip").write_bytes(b"extra")
    with pytest.raises(ReleaseContractError, match="unexpected distribution files"):
        create_manifest(dist, project=PROJECT, version=VERSION)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_manifest_rejects_filename_spoofed_distribution_metadata(tmp_path, kind: str) -> None:
    dist = _dist(tmp_path)
    if kind == "wheel":
        _write_wheel(
            dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl",
            project="different-project",
        )
    else:
        _write_sdist(
            dist / "dcc_mcp_material_maker-0.4.1.tar.gz",
            version="9.9.9",
        )

    with pytest.raises(ReleaseContractError, match="distribution metadata mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_wheel_metadata_outside_canonical_dist_info_root(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("wrong-0.4.1.dist-info/METADATA", _metadata())

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_wheel_path_traversal(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../../escape.txt", b"escape")

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_wheel_symlink_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        link = zipfile.ZipInfo("dcc_mcp_material_maker/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../escape.txt")

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_duplicate_wheel_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("dcc_mcp_material_maker/payload.txt", b"first")
            archive.writestr("dcc_mcp_material_maker/payload.txt", b"second")

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_wheel_with_unbounded_member_count(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        for index in range(10_001):
            archive.writestr(f"dcc_mcp_material_maker/members/{index}", b"")

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_metadata_outside_canonical_root(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    payload = _metadata()
    member = tarfile.TarInfo("wrong-root/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_path_traversal(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    traversal = tarfile.TarInfo(f"{root}/../../escape.txt")
    traversal.size = len(b"escape")
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(traversal, io.BytesIO(b"escape"))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_symlink_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    link = tarfile.TarInfo(f"{root}/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../escape.txt"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(link)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_fifo_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    fifo = tarfile.TarInfo(f"{root}/pipe")
    fifo.type = tarfile.FIFOTYPE
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(fifo)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_with_multiple_roots(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    other = tarfile.TarInfo("other-root/payload.txt")
    other.size = len(b"payload")
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(other, io.BytesIO(b"payload"))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_duplicate_sdist_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    first = tarfile.TarInfo(f"{root}/payload.txt")
    first.size = len(b"first")
    second = tarfile.TarInfo(f"{root}/payload.txt")
    second.size = len(b"second")
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(first, io.BytesIO(b"first"))
        archive.addfile(second, io.BytesIO(b"second"))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_sdist_with_unbounded_member_count(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        for index in range(10_001):
            member = tarfile.TarInfo(f"{root}/members/{index}")
            member.size = 0
            archive.addfile(member, io.BytesIO())

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/absolute.txt",
        "//server/share.txt",
        "C:/drive.txt",
        "dcc_mcp_material_maker\\backslash.txt",
        "dcc_mcp_material_maker/\x01control.txt",
        "dcc_mcp_material_maker/\u0085control.txt",
    ],
)
def test_manifest_rejects_unsafe_archive_paths(tmp_path, unsafe_path: str) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(unsafe_path, b"unsafe")
    if "\\" in unsafe_path:
        stored_path = unsafe_path.replace("\\", "/").encode()
        wheel.write_bytes(wheel.read_bytes().replace(stored_path, unsafe_path.encode()))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_nul_in_raw_wheel_member_name(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    safe_name = b"dcc_mcp_material_maker/nulx.txt"
    unsafe_name = b"dcc_mcp_material_maker/nul\x00.txt"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(safe_name.decode(), b"unsafe")
    wheel.write_bytes(wheel.read_bytes().replace(safe_name, unsafe_name))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_wraps_unsupported_wheel_compression_with_stable_error(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    raw = bytearray(wheel.read_bytes())
    local_header = raw.index(b"PK\x03\x04")
    central_header = raw.index(b"PK\x01\x02")
    raw[local_header + 8 : local_header + 10] = (99).to_bytes(2, "little")
    raw[central_header + 10 : central_header + 12] = (99).to_bytes(2, "little")
    wheel.write_bytes(raw)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_sdist_validation_does_not_bulk_enumerate_archive(tmp_path, monkeypatch) -> None:
    dist = _dist(tmp_path)

    def reject_unbounded_enumeration(_archive):
        raise AssertionError("archive members must be consumed with a bounded iterator")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", reject_unbounded_enumeration)
    create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_wheel_special_file_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        fifo = zipfile.ZipInfo("dcc_mcp_material_maker/pipe")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
        archive.writestr(fifo, b"")

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_additional_wheel_metadata_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("other-9.9.9.dist-info/METADATA", _metadata())

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_additional_sdist_pkg_info_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    canonical = tarfile.TarInfo(f"{root}/PKG-INFO")
    canonical.size = len(metadata)
    additional = tarfile.TarInfo(f"{root}/nested/PKG-INFO")
    additional.size = len(metadata)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(canonical, io.BytesIO(metadata))
        archive.addfile(additional, io.BytesIO(metadata))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_oversized_distribution_metadata_before_read(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    metadata_path = "dcc_mcp_material_maker-0.4.1.dist-info/METADATA"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(metadata_path, b"x" * (1024 * 1024 + 1))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_oversized_sdist_pkg_info_before_read(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    payload = b"x" * (1024 * 1024 + 1)
    member = tarfile.TarInfo("dcc_mcp_material_maker-0.4.1/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_oversized_archive_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("dcc_mcp_material_maker/oversized.bin", b"x" * (16 * 1024 * 1024 + 1))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_oversized_sdist_member(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    payload = b"x" * (16 * 1024 * 1024 + 1)
    oversized = tarfile.TarInfo(f"{root}/oversized.bin")
    oversized.size = len(payload)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(oversized, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_excessive_total_uncompressed_size(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    payload = b"x" * (14 * 1024 * 1024)
    with zipfile.ZipFile(wheel, "a", compression=zipfile.ZIP_STORED) as archive:
        for index in range(5):
            archive.writestr(f"dcc_mcp_material_maker/large-{index}.bin", payload)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_excessive_sdist_total_uncompressed_size(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    payload = b"x" * (14 * 1024 * 1024)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        for index in range(5):
            member = tarfile.TarInfo(f"{root}/large-{index}.bin")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_excessive_wheel_compression_ratio(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dcc_mcp_material_maker/bomb.bin", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_manifest_rejects_excessive_sdist_compression_ratio(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    metadata_member = tarfile.TarInfo(f"{root}/PKG-INFO")
    metadata_member.size = len(metadata)
    payload = b"0" * (2 * 1024 * 1024)
    compressed = tarfile.TarInfo(f"{root}/bomb.bin")
    compressed.size = len(payload)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(metadata_member, io.BytesIO(metadata))
        archive.addfile(compressed, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_snapshot_recaptures_exact_tag_source_and_release_identity() -> None:
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )
    assert snapshot.release_node_id == "RE_kwDOTijwzs4WbAoL"
    verify_snapshot(snapshot, ref_payload=_ref(), release_payload=_release(), expect_no_assets=True)

    recreated_release = _release(
        id=999,
        url="https://api.github.com/repos/dcc-mcp/dcc-mcp-material-maker/releases/999",
        upload_url=(
            "https://uploads.github.com/repos/dcc-mcp/dcc-mcp-material-maker/"
            "releases/999/assets{?name,label}"
        ),
    )
    stale_cases = [
        (_ref("2" * 40), _release(), "tag source drift"),
        (_ref(), recreated_release, "release identity drift"),
        (_ref(), _release(target_commitish="2" * 40), "release source drift"),
        (_ref(), _release(draft=True), "release state drift"),
        (_ref(), _release(prerelease=True), "release state drift"),
        (_ref(), _release(immutable=True), "release state drift"),
        (_ref(), _release(node_id="RE_recreated"), "release state drift"),
        (_ref(), _release(published_at="2026-08-27T00:00:02Z"), "release state drift"),
    ]
    for ref_payload, release_payload, message in stale_cases:
        with pytest.raises(ReleaseContractError, match=message):
            verify_snapshot(
                snapshot,
                ref_payload=ref_payload,
                release_payload=release_payload,
                expect_no_assets=True,
            )


def test_snapshot_fails_closed_on_annotated_tag_or_existing_asset() -> None:
    with pytest.raises(ReleaseContractError, match="lightweight commit tag"):
        capture_snapshot(
            repository="dcc-mcp/dcc-mcp-material-maker",
            tag=TAG,
            source_sha=SHA,
            ref_payload={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "2" * 40}},
            release_payload=_release(),
        )

    existing_asset = {
        "id": 7,
        "name": "dcc_mcp_material_maker-0.4.1.tar.gz",
        "size": 1,
        "state": "uploaded",
        "digest": "sha256:" + "3" * 64,
    }
    with pytest.raises(ReleaseContractError, match="release assets must be empty"):
        capture_snapshot(
            repository="dcc-mcp/dcc-mcp-material-maker",
            tag=TAG,
            source_sha=SHA,
            ref_payload=_ref(),
            release_payload=_release(assets=[existing_asset]),
        )


def test_published_assets_must_exactly_match_manifest(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )
    assets = [
        {
            "id": index,
            "name": entry["filename"],
            "size": entry["size"],
            "state": "uploaded",
            "digest": f"sha256:{entry['sha256']}",
        }
        for index, entry in enumerate(manifest["artifacts"], start=1)
    ]
    verify_published_assets(
        snapshot, manifest, ref_payload=_ref(), release_payload=_release(assets=assets)
    )

    mismatched = copy.deepcopy(assets)
    mismatched[0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ReleaseContractError, match="published asset mismatch"):
        verify_published_assets(
            snapshot,
            manifest,
            ref_payload=_ref(),
            release_payload=_release(assets=mismatched),
        )

    extra = copy.deepcopy(assets)
    extra.append(
        {
            "id": 99,
            "name": "decoy.whl",
            "size": 1,
            "state": "uploaded",
            "digest": "sha256:" + "0" * 64,
        }
    )
    with pytest.raises(ReleaseContractError, match="published asset set mismatch"):
        verify_published_assets(
            snapshot,
            manifest,
            ref_payload=_ref(),
            release_payload=_release(assets=extra),
        )


def test_manifest_file_rejects_noncanonical_or_untrusted_json(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    manifest_path = tmp_path / "manifest.json"
    expected_sha = write_manifest(manifest, manifest_path)

    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
    decoded["artifacts"][0]["filename"] = "../escape.whl"
    manifest_path.write_text(json.dumps(decoded), encoding="utf-8")
    with pytest.raises(ReleaseContractError, match="manifest SHA-256 mismatch"):
        verify_manifest(manifest_path, dist, expected_sha)


def test_verify_manifest_repeats_archive_validation_for_downloaded_bytes(tmp_path) -> None:
    dist = _dist(tmp_path)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../../escape.txt", b"escape")
    manifest = {
        "schema_version": 1,
        "project": PROJECT,
        "version": VERSION,
        "artifacts": [
            {
                "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for path in sorted(dist.iterdir(), key=lambda item: item.name)
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_sha256 = write_manifest(manifest, manifest_path)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        verify_manifest(manifest_path, dist, manifest_sha256)


def test_local_checkout_rejects_tracked_source_drift(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "rev-parse":
            return release_guard.subprocess.CompletedProcess(
                command, 0, stdout=SHA + "\n", stderr=""
            )
        return release_guard.subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(release_guard.subprocess, "run", fake_run)
    with pytest.raises(ReleaseContractError, match="tracked source drift"):
        release_guard._verify_local_checkout(SHA)
    assert calls == [
        ["git", "rev-parse", "HEAD^{commit}"],
        ["git", "diff", "--quiet", "HEAD", "--"],
    ]


def test_sdist_explicitly_excludes_local_release_evidence() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = set(config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])
    assert {
        ".cache/**",
        ".tmp/**",
        ".venv/**",
        "dist/**",
        "release/**",
        "work/**",
    } <= excludes


def test_asset_publication_rolls_back_if_identity_drifts_between_assets(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.upload_release_ids: list[int] = []
            self.deleted: list[tuple[int, int]] = []

        def recapture_release(self, expected_snapshot):
            assert expected_snapshot == snapshot
            self.recaptures += 1
            payload = _release(assets=copy.deepcopy(self.assets))
            if self.recaptures == 3:
                payload["published_at"] = "2026-08-27T00:00:02Z"
            return _ref(), payload

        def upload_asset(
            self, release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            self.upload_release_ids.append(release_id)
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, release_id: int, asset_id: int) -> None:
            self.deleted.append((release_id, asset_id))
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="release state drift"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.upload_release_ids == [snapshot.release_id]
    assert github.deleted == [(snapshot.release_id, 1)]
    assert github.assets == []


def test_asset_rollback_retries_when_delete_did_not_reach_github(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.delete_attempts = 0

        def recapture_release(self, _snapshot):
            self.recaptures += 1
            payload = _release(assets=copy.deepcopy(self.assets))
            if self.recaptures == 3:
                payload["published_at"] = "2026-08-27T00:00:02Z"
            return _ref(), payload

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            asset = {
                "id": 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.delete_attempts += 1
            if self.delete_attempts < 3:
                raise TimeoutError("DELETE did not reach GitHub")
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="release state drift"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.delete_attempts == 3
    assert github.assets == []


def test_asset_rollback_retry_exhaustion_is_bounded_and_fail_closed(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.delete_attempts = 0

        def recapture_release(self, _snapshot):
            self.recaptures += 1
            payload = _release(assets=copy.deepcopy(self.assets))
            if self.recaptures == 3:
                payload["published_at"] = "2026-08-27T00:00:02Z"
            return _ref(), payload

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            asset = {
                "id": 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id: int, _asset_id: int) -> None:
            self.delete_attempts += 1
            raise TimeoutError("DELETE never reached GitHub")

    github = FakeGitHub()
    with pytest.raises(
        ReleaseContractError, match="asset publication failed and rollback was incomplete"
    ):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.delete_attempts == 3
    assert [asset["id"] for asset in github.assets] == [1]


def test_asset_publication_rolls_back_asset_created_with_bad_server_digest(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.deleted: list[tuple[int, int]] = []

        def recapture_release(self, expected_snapshot):
            assert expected_snapshot == snapshot
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def upload_asset(
            self, release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            assert release_id == snapshot.release_id
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.assets:
                digest = "0" * 64
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{digest}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, release_id: int, asset_id: int) -> None:
            self.deleted.append((release_id, asset_id))
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="uploaded asset mismatch"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.deleted == [(snapshot.release_id, 2), (snapshot.release_id, 1)]
    assert github.assets == []


def test_asset_conflict_preflight_finishes_before_first_mutation(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )
    existing = {
        "id": 99,
        "name": manifest["artifacts"][0]["filename"],
        "size": 1,
        "state": "uploaded",
        "digest": "sha256:" + "0" * 64,
    }

    class FakeGitHub:
        mutations = 0

        def recapture_release(self, _snapshot):
            return _ref(), _release(assets=[existing])

        def upload_asset(self, _release_id, _path, _ownership_marker):
            self.mutations += 1
            raise AssertionError("upload must not run")

        def delete_asset(self, _release_id, _asset_id):
            self.mutations += 1
            raise AssertionError("delete must not run")

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="release assets must be empty"):
        publish_assets_transactional(snapshot, manifest, dist, github)
    assert github.mutations == 0


def test_asset_publication_is_sequential_and_recaptures_before_each_post(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.events: list[str] = []

        def recapture_release(self, _snapshot):
            self.events.append("recapture")
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def upload_asset(
            self, release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            assert release_id == snapshot.release_id
            self.events.append(f"upload:{path.name}")
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id, _asset_id):
            raise AssertionError("successful publication must not roll back")

    github = FakeGitHub()
    publish_assets_transactional(snapshot, manifest, dist, github)
    assert github.events == [
        "recapture",
        "recapture",
        "upload:dcc_mcp_material_maker-0.4.1-py3-none-any.whl",
        "recapture",
        "upload:dcc_mcp_material_maker-0.4.1.tar.gz",
        "recapture",
    ]


def test_asset_publication_recovers_ambiguous_post_timeout_without_partial_assets(
    tmp_path,
) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.deleted: list[int] = []

        def recapture_release(self, _snapshot):
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            if len(self.assets) == 2:
                raise TimeoutError("response lost after server commit")
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="GitHub asset publication failed"):
        publish_assets_transactional(snapshot, manifest, dist, github)
    assert github.deleted == [2, 1]
    assert github.assets == []


def test_lost_post_response_uses_transaction_marker_and_preserves_contender(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.deleted: list[int] = []
            self.markers: list[str] = []

        def recapture_release(self, _snapshot):
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def make_asset(self, path: Path, asset_id: int, marker: str | None) -> dict[str, object]:
            return {
                "id": asset_id,
                "name": path.name,
                "label": marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            assert ownership_marker
            self.markers.append(ownership_marker)
            if not self.assets:
                asset = self.make_asset(path, 1, ownership_marker)
                self.assets.append(asset)
                return copy.deepcopy(asset)
            self.assets.extend(
                [
                    self.make_asset(path, 2, ownership_marker),
                    self.make_asset(path, 3, ownership_marker),
                    self.make_asset(path, 99, "another-transaction"),
                ]
            )
            raise TimeoutError("POST response lost after multiple server commits")

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="GitHub asset publication failed"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert len(github.markers) == 2
    assert github.markers[0] != github.markers[1]
    assert set(github.deleted) == {1, 2, 3}
    assert [asset["id"] for asset in github.assets] == [99]


def test_lost_post_response_cleans_exact_candidate_despite_same_marker_drift(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.deleted: list[int] = []

        def recapture_release(self, _snapshot):
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def make_asset(self, path: Path, asset_id: int, ownership_marker: str) -> dict[str, object]:
            return {
                "id": asset_id,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            if not self.assets:
                asset = self.make_asset(path, 1, ownership_marker)
                self.assets.append(asset)
                return copy.deepcopy(asset)
            exact = self.make_asset(path, 2, ownership_marker)
            drifted = self.make_asset(path, 3, ownership_marker)
            drifted["digest"] = "sha256:" + "0" * 64
            self.assets.extend([exact, drifted])
            raise TimeoutError("POST response lost with mixed marker candidates")

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(
        ReleaseContractError, match="asset publication failed and rollback was incomplete"
    ):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert set(github.deleted) == {1, 2}
    assert [asset["id"] for asset in github.assets] == [3]


def test_lost_post_response_retries_pending_asset_recapture(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.deleted: list[int] = []

        def recapture_release(self, _snapshot):
            self.recaptures += 1
            if self.recaptures == 3:
                raise TimeoutError("first recovery recapture was lost")
            return _ref(), _release(assets=copy.deepcopy(self.assets))

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            self.assets.append(
                {
                    "id": 1,
                    "name": path.name,
                    "label": ownership_marker,
                    "size": path.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                }
            )
            raise TimeoutError("POST response lost after server commit")

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="GitHub asset publication failed"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.deleted == [1]
    assert github.assets == []


def test_lost_post_response_recovers_asset_after_delayed_visibility(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.deleted: list[int] = []

        def recapture_release(self, _snapshot):
            self.recaptures += 1
            visible = self.assets if self.recaptures >= 4 else []
            return _ref(), _release(assets=copy.deepcopy(visible))

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            self.assets.append(
                {
                    "id": 1,
                    "name": path.name,
                    "label": ownership_marker,
                    "size": path.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                }
            )
            raise TimeoutError("POST response lost before asset became visible")

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="GitHub asset publication failed"):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.deleted == [1]
    assert github.assets == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("name", "contender.whl"),
        ("digest", "sha256:" + "0" * 64),
        ("size", 999_999),
        ("state", "new"),
        ("label", "another-transaction"),
    ],
)
def test_rollback_preserves_identity_drift_and_continues_other_owned_assets(
    tmp_path, field: str, replacement: object
) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.recaptures = 0
            self.deleted: list[int] = []

        def recapture_release(self, _snapshot):
            self.recaptures += 1
            payload = _release(assets=copy.deepcopy(self.assets))
            if self.recaptures == 4:
                self.assets[1][field] = replacement
                payload = _release(
                    assets=copy.deepcopy(self.assets),
                    published_at="2026-08-27T00:00:02Z",
                )
            return _ref(), payload

        def upload_asset(
            self, _release_id: int, path: Path, ownership_marker: str
        ) -> dict[str, object]:
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
                "label": ownership_marker,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    github = FakeGitHub()
    with pytest.raises(
        ReleaseContractError, match="asset publication failed and rollback was incomplete"
    ):
        publish_assets_transactional(snapshot, manifest, dist, github)

    assert github.deleted == [1]
    assert [asset["id"] for asset in github.assets] == [2]


def test_release_client_recaptures_and_uploads_only_by_numeric_release_id(
    tmp_path, monkeypatch
) -> None:
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )
    asset = tmp_path / "artifact.whl"
    asset.write_bytes(b"wheel")
    ownership_marker = f"dcc-mcp-tx-{'a' * 32}-1-{'b' * 12}"
    requests: list[tuple[str, str]] = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        assert timeout == 30
        requests.append((request.get_method(), request.full_url))
        if request.full_url.endswith(f"git/ref/tags/{TAG}"):
            return Response(json.dumps(_ref()).encode())
        if request.full_url.endswith("releases/12345"):
            return Response(json.dumps(_release()).encode())
        assert request.get_method() == "POST"
        return Response(
            json.dumps(
                {
                    "id": 7,
                    "name": asset.name,
                    "label": ownership_marker,
                    "size": asset.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(asset.read_bytes()).hexdigest()}",
                }
            ).encode()
        )

    monkeypatch.setattr(release_guard.urllib.request, "urlopen", fake_urlopen)
    client = GitHubReleaseClient(snapshot.repository, "token")
    client.recapture_release(snapshot)
    client.upload_asset(snapshot.release_id, asset, ownership_marker)

    assert requests == [
        (
            "GET",
            f"https://api.github.com/repos/{snapshot.repository}/git/ref/tags/{TAG}",
        ),
        (
            "GET",
            f"https://api.github.com/repos/{snapshot.repository}/releases/{snapshot.release_id}",
        ),
        (
            "POST",
            "https://uploads.github.com/repos/"
            f"{snapshot.repository}/releases/{snapshot.release_id}/"
            f"assets?name=artifact.whl&label={ownership_marker}",
        ),
    ]


def test_artifact_metadata_binds_id_digest_source_run_repository_and_expiry() -> None:
    artifact_id = 987
    digest = "a" * 64
    run_id = 456
    repository = "dcc-mcp/dcc-mcp-material-maker"
    metadata: dict[str, object] = {
        "id": artifact_id,
        "name": "release-distributions",
        "size_in_bytes": 4096,
        "url": f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}",
        "archive_download_url": (
            f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        ),
        "expired": False,
        "created_at": "2026-08-27T00:00:00Z",
        "expires_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-27T00:00:01Z",
        "digest": f"sha256:{digest}",
        "workflow_run": {
            "id": run_id,
            "repository_id": 123,
            "head_repository_id": 123,
            "head_branch": "main",
            "head_sha": SHA,
        },
    }
    expected = {
        "repository": repository,
        "artifact_id": artifact_id,
        "artifact_digest": digest,
        "source_sha": SHA,
        "run_id": run_id,
        "name": "release-distributions",
    }
    now = datetime(2026, 8, 27, 1, tzinfo=timezone.utc)
    verify_artifact_metadata(metadata, now=now, **expected)

    stale_cases = [
        ({"expired": True}, "artifact expired"),
        ({"digest": "sha256:" + "b" * 64}, "artifact digest drift"),
        ({"workflow_run": {**metadata["workflow_run"], "head_sha": "2" * 40}}, "source drift"),
        ({"expires_at": "2026-08-27T00:30:00Z"}, "artifact expired"),
    ]
    for changes, message in stale_cases:
        candidate = copy.deepcopy(metadata)
        candidate.update(changes)
        with pytest.raises(ReleaseContractError, match=message):
            verify_artifact_metadata(candidate, now=now, **expected)


def test_release_client_recaptures_artifact_by_exact_numeric_id(monkeypatch) -> None:
    artifact_id = 987
    requested: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        assert timeout == 30
        requested.append(request.full_url)
        return Response(json.dumps({"id": artifact_id}).encode())

    monkeypatch.setattr(release_guard.urllib.request, "urlopen", fake_urlopen)
    client = GitHubReleaseClient("dcc-mcp/dcc-mcp-material-maker", "token")
    assert client.recapture_artifact(artifact_id) == {"id": artifact_id}
    assert requested == [
        "https://api.github.com/repos/dcc-mcp/dcc-mcp-material-maker/"
        f"actions/artifacts/{artifact_id}"
    ]
