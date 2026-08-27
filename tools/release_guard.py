"""Fail-closed release identity and distribution provenance checks.

The release workflow builds once, records an immutable local manifest and a
GitHub release snapshot, then replays these checks immediately before each
external publication mutation.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(
    r"^v(?P<version>0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:[-+][0-9A-Za-z.-]+)?$"
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
            snapshot.release_tag,
            snapshot.release_target,
            snapshot.created_at,
            snapshot.published_at,
            snapshot.release_url,
            snapshot.upload_url,
        )
        if (
            type(snapshot.schema_version) is not int
            or snapshot.schema_version != 1
            or type(snapshot.release_id) is not int
            or snapshot.release_id <= 0
            or type(snapshot.draft) is not bool
            or type(snapshot.prerelease) is not bool
            or type(snapshot.immutable) is not bool
            or any(type(value) is not str for value in string_fields)
        ):
            raise ReleaseContractError("snapshot schema mismatch")
        return snapshot


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
    if release_id <= 0 or release_tag != tag:
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
            schema_version=1,
            repository=repository,
            tag=tag,
            source_sha=source_sha,
            release_id=release_id,
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
    ref_payload, release_payload = _live_payloads(args.repository, args.tag, args.github_token)
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

    verify = subparsers.add_parser("verify", parents=[common, verify_common])
    verify.add_argument("--expect-no-assets", action="store_true", required=True)
    verify.set_defaults(handler=_verify_command)

    verify_assets = subparsers.add_parser("verify-assets", parents=[common, verify_common])
    verify_assets.set_defaults(handler=_verify_assets_command)
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
