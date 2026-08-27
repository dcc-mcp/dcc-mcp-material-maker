from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
FULL_SHA_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
EXPECTED_JOBS = {
    "release-please",
    "build-release",
    "publish-pypi",
    "publish-github-assets",
}
ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "googleapis/release-please-action",
    "pypa/gh-action-pypi-publish",
    "softprops/action-gh-release",
}
MUTATING_RUN_PATTERNS = (
    re.compile(r"(?:^|[;&|\s])twine\s+upload(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|\s])gh\s+release\s+(?:create|upload|delete|edit)(?:\s|$)", re.IGNORECASE),
    re.compile(
        r"(?:^|[;&|\s])gh\s+api\b[^\n]*(?:-X|--method)\s*(?:POST|PUT|PATCH|DELETE)", re.IGNORECASE
    ),
    re.compile(r"\bcurl\b[^\n]*(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE)", re.IGNORECASE),
)
EXPECTED_RUN_PREFIXES = {
    "release-please": [],
    "build-release": [
        "python -m pip install",
        "python -m build",
        "python -m twine check",
        "python tools/release_guard.py capture",
    ],
    "publish-pypi": ["python tools/release_guard.py verify"],
    "publish-github-assets": [
        "python tools/release_guard.py verify",
        "python tools/release_guard.py verify-assets",
    ],
}


def _load_workflow() -> dict[str, Any]:
    document = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return {value}
    assert isinstance(value, list)
    return set(value)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    value = job.get("steps")
    assert isinstance(value, list)
    assert all(isinstance(step, dict) for step in value)
    return value


def _action_steps(job: dict[str, Any], repository: str) -> list[dict[str, Any]]:
    prefix = f"{repository}@"
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)]


def _single_action(job: dict[str, Any], repository: str) -> dict[str, Any]:
    matches = _action_steps(job, repository)
    assert len(matches) == 1, f"expected exactly one {repository} action, found {len(matches)}"
    return matches[0]


def _all_action_steps(jobs: dict[str, Any], repository: str) -> list[dict[str, Any]]:
    return [step for job in jobs.values() for step in _action_steps(job, repository)]


def _scalar_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _scalar_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _scalar_strings(item)]
    return []


def _active_shell(run: object) -> str:
    lines = []
    for raw_line in str(run or "").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return "\n".join(lines)


def _assert_immediate_guard(
    job: dict[str, Any], mutation_step: dict[str, Any], *, expect_assets: bool
) -> None:
    steps = _steps(job)
    mutation_index = steps.index(mutation_step)
    assert mutation_index > 0
    guard = steps[mutation_index - 1]
    run = _active_shell(guard.get("run"))
    expected_command = (
        "tools/release_guard.py verify-assets "
        if expect_assets
        else "tools/release_guard.py verify "
    )
    assert expected_command in run
    assert "--expected-manifest-sha256" in run
    assert "--expected-snapshot-sha256" in run
    assert "--expected-release-id" in run
    if not expect_assets:
        assert "--expect-no-assets" in run


def assert_release_workflow_contract(document: dict[str, Any]) -> None:
    assert document.get("on") == {"push": {"branches": ["main"]}}
    permissions = document.get("permissions")
    assert permissions == {"contents": "read"}

    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    assert set(jobs) == EXPECTED_JOBS

    release = jobs["release-please"]
    build = jobs["build-release"]
    pypi = jobs["publish-pypi"]
    github_assets = jobs["publish-github-assets"]

    assert release.get("permissions") == {"contents": "write", "pull-requests": "write"}
    assert build.get("permissions") == {"contents": "read"}
    assert pypi.get("permissions") == {"contents": "read", "id-token": "write"}
    assert github_assets.get("permissions") == {"contents": "write"}
    assert _needs(build) == {"release-please"}
    assert _needs(pypi) == {"build-release"}
    assert _needs(github_assets) == {"build-release", "publish-pypi"}

    for job_name, job in jobs.items():
        job_permissions = job.get("permissions", {})
        if job_name == "publish-pypi":
            assert job_permissions.get("id-token") == "write"
        else:
            assert job_permissions.get("id-token") != "write"
        for step in _steps(job):
            uses = step.get("uses")
            if uses is not None:
                assert FULL_SHA_ACTION.fullmatch(str(uses)), f"action is not SHA-pinned: {uses}"
                assert str(uses).split("@", 1)[0] in ALLOWED_ACTIONS

    workflow_text = "\n".join(_scalar_strings(document))
    assert "PERSONAL_ACCESS_TOKEN" not in workflow_text
    assert "password:" not in workflow_text
    assert "api-token:" not in workflow_text

    release_action = _single_action(release, "googleapis/release-please-action")
    assert len(_all_action_steps(jobs, "googleapis/release-please-action")) == 1
    assert release_action.get("with", {}).get("token") == "${{ github.token }}"

    build_checkout = _single_action(build, "actions/checkout")
    assert (
        build_checkout.get("with", {}).get("ref") == "${{ needs.release-please.outputs.tag_name }}"
    )
    build_commands = [_active_shell(step.get("run")) for step in _steps(build)]
    assert sum("python -m build" in command for command in build_commands) == 1
    assert any("python -m twine check dist/*" in command for command in build_commands)
    assert any("tools/release_guard.py capture" in command for command in build_commands)
    capture_command = next(
        command for command in build_commands if "tools/release_guard.py capture" in command
    )
    assert '--source-sha "${{ github.sha }}"' in capture_command

    upload = _single_action(build, "actions/upload-artifact")
    assert upload.get("with", {}).get("if-no-files-found") == "error"
    upload_paths = str(upload.get("with", {}).get("path", ""))
    assert "dist/" in upload_paths
    assert "release/" in upload_paths
    build_outputs = build.get("outputs", {})
    assert set(build_outputs) >= {
        "source_sha",
        "release_id",
        "manifest_sha256",
        "snapshot_sha256",
        "artifact_digest",
    }
    assert build_outputs["artifact_digest"] == "${{ steps.upload.outputs['artifact-digest'] }}"

    for job in (pypi, github_assets):
        checkout = _single_action(job, "actions/checkout")
        assert (
            checkout.get("with", {}).get("ref") == "${{ needs.build-release.outputs.source_sha }}"
        )
        download = _single_action(job, "actions/download-artifact")
        assert download.get("with", {}).get("name") == "release-distributions"
        assert checkout.get("with", {}).get("persist-credentials") == "false"

    pypi_action = _single_action(pypi, "pypa/gh-action-pypi-publish")
    assert len(_all_action_steps(jobs, "pypa/gh-action-pypi-publish")) == 1
    assert pypi.get("environment", {}).get("name") == "pypi"
    assert pypi_action.get("continue-on-error") is None
    assert pypi_action.get("with", {}).get("skip-existing") == "false"
    _assert_immediate_guard(pypi, pypi_action, expect_assets=False)

    github_action = _single_action(github_assets, "softprops/action-gh-release")
    assert len(_all_action_steps(jobs, "softprops/action-gh-release")) == 1
    assert github_assets.get("if") is None
    github_inputs = github_action.get("with", {})
    assert github_inputs.get("tag_name") == "${{ needs.build-release.outputs.tag_name }}"
    assert github_inputs.get("files") == "dist/*"
    assert github_inputs.get("overwrite_files") == "false"
    assert github_inputs.get("fail_on_unmatched_files") == "true"
    steps = _steps(github_assets)
    github_index = steps.index(github_action)
    assert github_index > 0
    pre_upload_guard = steps[github_index - 1]
    pre_upload_run = _active_shell(pre_upload_guard.get("run"))
    assert "tools/release_guard.py verify " in pre_upload_run
    assert "--expect-no-assets" in pre_upload_run
    assert "--expected-snapshot-sha256" in pre_upload_run
    assert github_index + 1 < len(steps)
    post_upload_run = _active_shell(steps[github_index + 1].get("run"))
    assert "tools/release_guard.py verify-assets " in post_upload_run

    for job_name, job in jobs.items():
        active_commands = []
        for step in _steps(job):
            command = _active_shell(step.get("run"))
            if not command:
                continue
            active_commands.append(command)
            for pattern in MUTATING_RUN_PATTERNS:
                assert pattern.search(command) is None, (
                    f"unexpected publication mutation path: {command}"
                )
        expected_prefixes = EXPECTED_RUN_PREFIXES[job_name]
        assert len(active_commands) == len(expected_prefixes)
        assert all(
            any(command.startswith(prefix) for prefix in expected_prefixes)
            for command in active_commands
        )


def test_release_workflow_is_semantically_fail_closed() -> None:
    assert_release_workflow_contract(_load_workflow())


def test_contract_ignores_decoy_labels_but_rejects_extra_mutation_paths() -> None:
    document = _load_workflow()
    assert_release_workflow_contract(document)

    decoy = copy.deepcopy(document)
    decoy["jobs"]["build-release"]["steps"][0]["name"] = (
        "pypa/gh-action-pypi-publish and softprops/action-gh-release decoy"
    )
    capture_step = next(
        step
        for step in decoy["jobs"]["build-release"]["steps"]
        if "tools/release_guard.py capture" in str(step.get("run", ""))
    )
    capture_step["run"] = "# twine upload dist/*\n" + capture_step["run"]
    assert_release_workflow_contract(decoy)

    extra_mutation = copy.deepcopy(document)
    extra_mutation["jobs"]["build-release"]["steps"].append(
        {"name": "hidden publisher", "run": "python -m twine upload dist/*"}
    )
    with pytest.raises(AssertionError, match="unexpected publication mutation path"):
        assert_release_workflow_contract(extra_mutation)

    duplicate_asset_mutation = copy.deepcopy(document)
    existing = _single_action(
        duplicate_asset_mutation["jobs"]["publish-github-assets"],
        "softprops/action-gh-release",
    )
    duplicate_asset_mutation["jobs"]["publish-github-assets"]["steps"].append(existing)
    with pytest.raises(AssertionError, match="expected exactly one softprops/action-gh-release"):
        assert_release_workflow_contract(duplicate_asset_mutation)

    misplaced_asset_mutation = copy.deepcopy(document)
    misplaced_asset_mutation["jobs"]["build-release"]["steps"].append(copy.deepcopy(existing))
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(misplaced_asset_mutation)
