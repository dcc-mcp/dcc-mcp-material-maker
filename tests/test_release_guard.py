from __future__ import annotations

import copy
import hashlib
import io
import json
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

        def upload_asset(self, release_id: int, path: Path) -> dict[str, object]:
            self.upload_release_ids.append(release_id)
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
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

        def upload_asset(self, release_id: int, path: Path) -> dict[str, object]:
            assert release_id == snapshot.release_id
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.assets:
                digest = "0" * 64
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
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

        def upload_asset(self, _release_id, _path):
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

        def upload_asset(self, release_id: int, path: Path) -> dict[str, object]:
            assert release_id == snapshot.release_id
            self.events.append(f"upload:{path.name}")
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
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

        def upload_asset(self, _release_id: int, path: Path) -> dict[str, object]:
            asset = {
                "id": len(self.assets) + 1,
                "name": path.name,
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
                    "size": asset.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(asset.read_bytes()).hexdigest()}",
                }
            ).encode()
        )

    monkeypatch.setattr(release_guard.urllib.request, "urlopen", fake_urlopen)
    client = GitHubReleaseClient(snapshot.repository, "token")
    client.recapture_release(snapshot)
    client.upload_asset(snapshot.release_id, asset)

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
            f"{snapshot.repository}/releases/{snapshot.release_id}/assets?name=artifact.whl",
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
