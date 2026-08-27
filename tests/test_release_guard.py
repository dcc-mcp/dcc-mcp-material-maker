from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import tools.release_guard as release_guard
from tools.release_guard import (
    ReleaseContractError,
    capture_snapshot,
    create_manifest,
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


def _dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    wheel = dist / "dcc_mcp_material_maker-0.4.1-py3-none-any.whl"
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    wheel.write_bytes(b"wheel-content")
    sdist.write_bytes(b"sdist-content")
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


def test_snapshot_recaptures_exact_tag_source_and_release_identity() -> None:
    snapshot = capture_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_release(),
    )
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
