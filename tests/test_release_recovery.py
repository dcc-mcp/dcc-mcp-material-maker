from __future__ import annotations

import copy
import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_release_guard import (
    PROJECT,
    SHA,
    TAG,
    VERSION,
    _artifact_binding,
    _artifact_metadata,
    _dist,
    _metadata,
    _ref,
    _release,
    _write_current_dist_manifest,
)

import tools.release_guard as release_guard
from tools.release_guard import ReleaseContractError, create_manifest


def _draft_release(**changes: object) -> dict[str, object]:
    payload = _release(
        draft=True,
        published_at=None,
        assets=[],
        name=TAG,
        body=f"<!-- dcc-mcp-release-owner:v1:{SHA} -->",
    )
    payload.update(changes)
    return payload


def _artifact_asset(path: Path, *, asset_id: int, label: str) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": path.name,
        "label": label,
        "size": path.stat().st_size,
        "state": "uploaded",
        "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
    }


def _pypi_payload(manifest: dict[str, object]) -> dict[str, object]:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    return {
        "info": {"name": PROJECT, "version": VERSION},
        "urls": [
            {
                "filename": artifact["filename"],
                "packagetype": (
                    "bdist_wheel" if str(artifact["filename"]).endswith(".whl") else "sdist"
                ),
                "size": artifact["size"],
                "digests": {"sha256": artifact["sha256"]},
                "yanked": False,
            }
            for artifact in artifacts
        ],
    }


def test_exact_hash_pypi_state_is_a_resumable_completed_stage(tmp_path) -> None:
    manifest = create_manifest(_dist(tmp_path), project=PROJECT, version=VERSION)

    assert release_guard.verify_pypi_publication(manifest, _pypi_payload(manifest)) == "complete"


def test_pypi_preflight_resumes_exact_partial_but_rejects_hash_mismatch(tmp_path) -> None:
    manifest = create_manifest(_dist(tmp_path), project=PROJECT, version=VERSION)
    partial = _pypi_payload(manifest)
    partial["urls"] = partial["urls"][:1]
    mismatched = _pypi_payload(manifest)
    mismatched["urls"][0]["digests"]["sha256"] = "f" * 64

    assert release_guard.verify_pypi_publication(manifest, partial) == "partial"
    with pytest.raises(ReleaseContractError, match="PyPI publication mismatch"):
        release_guard.verify_pypi_publication(manifest, mismatched)


def test_pypi_publication_rejects_swapped_wheel_and_sdist_types(tmp_path) -> None:
    manifest = create_manifest(_dist(tmp_path), project=PROJECT, version=VERSION)
    swapped = _pypi_payload(manifest)
    for entry in swapped["urls"]:
        entry["packagetype"] = "sdist" if entry["packagetype"] == "bdist_wheel" else "bdist_wheel"

    with pytest.raises(ReleaseContractError, match="PyPI publication mismatch"):
        release_guard.verify_pypi_publication(manifest, swapped)


def test_draft_asset_failure_is_preserved_and_retry_resumes_without_delete(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = release_guard.capture_staged_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_draft_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.assets: list[dict[str, object]] = []
            self.upload_attempts = 0
            self.fail_second_once = True
            self.deleted: list[int] = []

        def recapture_artifact(self, _artifact_id: int) -> dict[str, object]:
            return _artifact_metadata()

        def recapture_release(self, _snapshot):
            return _ref(), _draft_release(assets=copy.deepcopy(self.assets))

        def upload_asset(self, _release_id: int, path: Path, marker: str):
            self.upload_attempts += 1
            if self.upload_attempts == 2 and self.fail_second_once:
                self.fail_second_once = False
                raise TimeoutError("second POST did not reach GitHub")
            asset = _artifact_asset(path, asset_id=self.upload_attempts, label=marker)
            self.assets.append(asset)
            return copy.deepcopy(asset)

        def delete_asset(self, _release_id: int, asset_id: int) -> None:
            self.deleted.append(asset_id)

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="GitHub asset publication incomplete"):
        release_guard.publish_assets_resumable(
            snapshot,
            manifest,
            dist,
            github,
            artifact_binding=_artifact_binding(),
        )

    assert len(github.assets) == 1
    assert github.deleted == []

    release_guard.publish_assets_resumable(
        snapshot,
        manifest,
        dist,
        github,
        artifact_binding=_artifact_binding(),
    )
    assert len(github.assets) == 2
    assert github.deleted == []


def test_resumable_asset_stage_refuses_unknown_owner_without_mutation(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = release_guard.capture_staged_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_draft_release(),
    )
    first = sorted(dist.iterdir(), key=lambda path: path.name)[0]

    class FakeGitHub:
        def __init__(self) -> None:
            self.uploaded = False
            self.assets = [_artifact_asset(first, asset_id=77, label="another-owner")]

        def recapture_artifact(self, _artifact_id: int) -> dict[str, object]:
            return _artifact_metadata()

        def recapture_release(self, _snapshot):
            return _ref(), _draft_release(assets=copy.deepcopy(self.assets))

        def upload_asset(self, *_args):
            self.uploaded = True
            raise AssertionError("must fail before mutation")

    github = FakeGitHub()
    with pytest.raises(ReleaseContractError, match="unknown release asset owner"):
        release_guard.publish_assets_resumable(
            snapshot,
            manifest,
            dist,
            github,
            artifact_binding=_artifact_binding(),
        )
    assert github.uploaded is False


def test_staged_release_requires_exact_owner_tag_source_and_draft_state() -> None:
    for changes in (
        {"body": "unowned draft"},
        {"name": "another release"},
        {"target_commitish": "2" * 40},
        {"draft": False, "published_at": "2026-08-28T00:00:00Z"},
    ):
        with pytest.raises(ReleaseContractError, match="release staging state drift"):
            release_guard.capture_staged_snapshot(
                repository="dcc-mcp/dcc-mcp-material-maker",
                tag=TAG,
                source_sha=SHA,
                ref_payload=_ref(),
                release_payload=_draft_release(**changes),
            )


def test_finalize_is_the_only_transition_from_complete_draft_to_public(tmp_path) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = release_guard.capture_staged_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_draft_release(),
    )

    class FakeGitHub:
        def __init__(self) -> None:
            self.draft = True
            self.assets = [
                _artifact_asset(
                    path,
                    asset_id=index,
                    label=release_guard.asset_ownership_marker(snapshot, artifact),
                )
                for index, (path, artifact) in enumerate(
                    zip(
                        sorted(dist.iterdir(), key=lambda item: item.name),
                        manifest["artifacts"],
                    ),
                    start=1,
                )
            ]
            self.published_ids: list[int] = []

        def recapture_release(self, _snapshot):
            payload = _draft_release(assets=copy.deepcopy(self.assets))
            if not self.draft:
                payload.update(draft=False, published_at="2026-08-28T00:00:00Z")
            return _ref(), payload

        def publish_release(self, release_id: int) -> dict[str, object]:
            self.published_ids.append(release_id)
            self.draft = False
            return self.recapture_release(snapshot)[1]

    github = FakeGitHub()
    release_guard.finalize_staged_release(snapshot, manifest, github)
    release_guard.finalize_staged_release(snapshot, manifest, github)
    assert github.published_ids == [snapshot.release_id]


def test_finalize_command_rechecks_exact_frozen_pypi_set_immediately_before_publish(
    tmp_path, monkeypatch
) -> None:
    dist = _dist(tmp_path)
    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    snapshot = release_guard.capture_staged_snapshot(
        repository="dcc-mcp/dcc-mcp-material-maker",
        tag=TAG,
        source_sha=SHA,
        ref_payload=_ref(),
        release_payload=_draft_release(),
    )
    events: list[object] = []

    class FakeGitHub:
        def __init__(self) -> None:
            self.draft = True

        def recapture_release(self, _snapshot):
            payload = _draft_release(
                assets=[
                    _artifact_asset(
                        path,
                        asset_id=index,
                        label=release_guard.asset_ownership_marker(snapshot, artifact),
                    )
                    for index, (path, artifact) in enumerate(
                        zip(
                            sorted(dist.iterdir(), key=lambda item: item.name),
                            manifest["artifacts"],
                        ),
                        start=1,
                    )
                ]
            )
            if not self.draft:
                payload.update(draft=False, published_at="2026-08-28T00:00:00Z")
            return _ref(), payload

        def publish_release(self, release_id: int) -> dict[str, object]:
            events.append(("publish", release_id))
            self.draft = False
            return self.recapture_release(snapshot)[1]

    github = FakeGitHub()
    monkeypatch.setattr(
        release_guard,
        "_common_live",
        lambda _args: (
            events.append("common") or snapshot,
            manifest,
            _ref(),
            _draft_release(),
        ),
    )
    monkeypatch.setattr(release_guard, "GitHubReleaseClient", lambda *_args: github)

    def fetch(project: str, version: str) -> dict[str, object]:
        events.append(("pypi", project, version))
        return _pypi_payload(manifest)

    monkeypatch.setattr(release_guard, "_fetch_pypi_payload", fetch)
    release_guard._finalize_command(
        SimpleNamespace(repository="dcc-mcp/dcc-mcp-material-maker", github_token="token")
    )

    assert events == [
        "common",
        ("pypi", PROJECT, VERSION),
        ("publish", snapshot.release_id),
    ]


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "partial",
        "extra",
        "hash-drift",
        "yanked",
        "wrong-project",
        "wrong-version",
        "typed-field",
        "swapped-packagetype",
        "lookup-error",
    ],
)
def test_finalize_command_fails_closed_on_non_exact_pypi_state(
    tmp_path, monkeypatch, case: str
) -> None:
    manifest = create_manifest(_dist(tmp_path), project=PROJECT, version=VERSION)
    snapshot = SimpleNamespace(schema_version=3)
    payload = _pypi_payload(manifest)
    if case == "absent":
        payload = None
    elif case == "partial":
        payload["urls"] = payload["urls"][:1]
    elif case == "extra":
        extra = copy.deepcopy(payload["urls"][0])
        extra["filename"] = "unexpected.whl"
        payload["urls"].append(extra)
    elif case == "hash-drift":
        payload["urls"][0]["digests"]["sha256"] = "f" * 64
    elif case == "yanked":
        payload["urls"][0]["yanked"] = True
    elif case == "wrong-project":
        payload["info"]["name"] = "other-project"
    elif case == "wrong-version":
        payload["info"]["version"] = "9.9.9"
    elif case == "typed-field":
        payload["urls"] = "not-a-list"
    elif case == "swapped-packagetype":
        for entry in payload["urls"]:
            entry["packagetype"] = (
                "sdist" if entry["packagetype"] == "bdist_wheel" else "bdist_wheel"
            )

    mutations: list[str] = []
    monkeypatch.setattr(
        release_guard,
        "_common_live",
        lambda _args: (snapshot, manifest, {}, {}),
    )
    monkeypatch.setattr(release_guard, "GitHubReleaseClient", lambda *_args: object())
    monkeypatch.setattr(
        release_guard,
        "finalize_staged_release",
        lambda *_args: mutations.append("public-transition"),
    )

    if case == "lookup-error":

        def fetch(*_args):
            raise ReleaseContractError("PyPI publication lookup failed")

        monkeypatch.setattr(release_guard, "_fetch_pypi_payload", fetch)
    else:
        monkeypatch.setattr(release_guard, "_fetch_pypi_payload", lambda *_args: payload)

    with pytest.raises(ReleaseContractError):
        release_guard._finalize_command(
            SimpleNamespace(repository="dcc-mcp/dcc-mcp-material-maker", github_token="token")
        )
    assert mutations == []


def test_pypi_lookup_timeout_is_a_typed_release_failure(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(release_guard.urllib.request, "urlopen", timeout)
    with pytest.raises(ReleaseContractError, match="PyPI publication lookup failed"):
        release_guard._fetch_pypi_payload(PROJECT, VERSION)


def test_workflow_stages_draft_checks_exact_pypi_and_publishes_release_last() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    config = (root / "release-please-config.json").read_text(encoding="utf-8")
    jobs = workflow["jobs"]

    assert '"skip-github-release":true' in config.replace(" ", "")
    assert set(jobs) == {
        "release-please",
        "stage-release",
        "publish-pypi",
        "publish-github-assets",
        "finalize-release",
    }
    assert jobs["stage-release"]["outputs"]["release_id"]
    pypi_steps = "\n".join(str(step) for step in jobs["publish-pypi"]["steps"])
    assert "pypi-preflight" in pypi_steps
    assert "pypi-verify" in pypi_steps
    assert "'skip-existing': 'true'" in pypi_steps
    assert jobs["finalize-release"]["needs"] == [
        "stage-release",
        "publish-pypi",
        "publish-github-assets",
    ]
    finalize_steps = "\n".join(str(step) for step in jobs["finalize-release"]["steps"])
    assert "finalize" in finalize_steps


def _write_pax_override_sdist(path: Path, *, raw_name: str, override_path: str) -> None:
    metadata = _metadata()
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(raw_name)
        info.pax_headers = {"path": override_path}
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_raw_ustar_traversal_cannot_be_hidden_by_safe_pax_path(tmp_path) -> None:
    dist = _dist(tmp_path)
    sdist = dist / "dcc_mcp_material_maker-0.4.1.tar.gz"
    _write_pax_override_sdist(
        sdist,
        raw_name="../outside-review-root.txt",
        override_path="dcc_mcp_material_maker-0.4.1/PKG-INFO",
    )

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)


def test_safe_pax_path_may_only_complete_its_exact_100_byte_raw_prefix(tmp_path) -> None:
    dist = _dist(tmp_path)
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    long_path = f"{root}/pkg/{'a' * 110}.txt"
    with tarfile.open(
        dist / "dcc_mcp_material_maker-0.4.1.tar.gz",
        "w:gz",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        metadata_info = tarfile.TarInfo(f"{root}/PKG-INFO")
        metadata_info.size = len(metadata)
        archive.addfile(metadata_info, io.BytesIO(metadata))
        payload = b"safe long path"
        long_info = tarfile.TarInfo(long_path)
        long_info.size = len(payload)
        archive.addfile(long_info, io.BytesIO(payload))

    manifest = create_manifest(dist, project=PROJECT, version=VERSION)
    assert manifest["version"] == VERSION


@pytest.mark.parametrize("phase", ["create", "verify"])
def test_pax_effective_path_must_itself_be_nfkc(tmp_path, phase: str) -> None:
    dist = _dist(tmp_path)
    root = "dcc_mcp_material_maker-0.4.1"
    metadata = _metadata()
    long_path = f"{root}/pkg/{'a' * 110}/\uff26\uff2f\uff2f.txt"
    with tarfile.open(
        dist / "dcc_mcp_material_maker-0.4.1.tar.gz",
        "w:gz",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        metadata_info = tarfile.TarInfo(f"{root}/PKG-INFO")
        metadata_info.size = len(metadata)
        archive.addfile(metadata_info, io.BytesIO(metadata))
        payload = b"unsafe normalized path"
        unsafe = tarfile.TarInfo(long_path)
        unsafe.size = len(payload)
        archive.addfile(unsafe, io.BytesIO(payload))

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        if phase == "create":
            create_manifest(dist, project=PROJECT, version=VERSION)
        else:
            manifest_path = tmp_path / "manifest.json"
            manifest_sha256 = _write_current_dist_manifest(dist, manifest_path)
            release_guard.verify_manifest(manifest_path, dist, manifest_sha256)


def test_publisher_preflight_reuses_raw_tar_validator_on_self_consistent_bytes(tmp_path) -> None:
    dist = _dist(tmp_path)
    _write_pax_override_sdist(
        dist / "dcc_mcp_material_maker-0.4.1.tar.gz",
        raw_name="../outside-review-root.txt",
        override_path="dcc_mcp_material_maker-0.4.1/PKG-INFO",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_sha256 = _write_current_dist_manifest(dist, manifest_path)

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        release_guard.verify_manifest(manifest_path, dist, manifest_sha256)


@pytest.mark.parametrize(
    "raw_name,override_path",
    [
        ("dcc_mcp_material_maker-0.4.1/PKG-INFO", "../outside-review-root.txt"),
        ("dcc_mcp_material_maker-0.4.1/PKG-INFO", "C:/outside-review-root.txt"),
        ("dcc_mcp_material_maker-0.4.1/PKG-INFO", "safe\\alias.txt"),
        ("dcc_mcp_material_maker-0.4.1/PKG-INFO", "safe/./alias.txt"),
    ],
)
def test_pax_override_decoys_are_validated_before_normalized_members(
    tmp_path, raw_name: str, override_path: str
) -> None:
    dist = _dist(tmp_path)
    _write_pax_override_sdist(
        dist / "dcc_mcp_material_maker-0.4.1.tar.gz",
        raw_name=raw_name,
        override_path=override_path,
    )

    with pytest.raises(ReleaseContractError, match="distribution archive mismatch"):
        create_manifest(dist, project=PROJECT, version=VERSION)
