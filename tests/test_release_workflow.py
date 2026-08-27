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
}
PINNED_ACTIONS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "googleapis/release-please-action": "45996ed1f6d02564a971a2fa1b5860e934307cf7",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}
MUTATING_RUN_PATTERNS = (
    re.compile(r"(?:^|[;&|\s])git\s+push(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|\s])gh\s+api(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|\s])curl(?:\.exe)?(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|\s])twine\s+upload(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|\s])gh\s+release\s+(?:create|upload|delete|edit)(?:\s|$)", re.IGNORECASE),
    re.compile(
        r"(?:^|[;&|\s])gh\s+api\b[^\n]*(?:-X|--method)\s*(?:POST|PUT|PATCH|DELETE)", re.IGNORECASE
    ),
    re.compile(r"\bcurl\b[^\n]*(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE)", re.IGNORECASE),
)
ARTIFACT_GUARD = (
    "python tools/release_guard.py verify-artifact "
    '--repository "${{ github.repository }}" '
    '--artifact-id "${{ needs.build-release.outputs.artifact_id }}" '
    '--artifact-digest "${{ needs.build-release.outputs.artifact_digest }}" '
    '--source-sha "${{ needs.build-release.outputs.source_sha }}" '
    '--run-id "${{ needs.build-release.outputs.run_id }}"'
)
RELEASE_BINDINGS = (
    '--repository "${{ github.repository }}" '
    '--tag "${{ needs.build-release.outputs.tag_name }}" '
    '--source-sha "${{ needs.build-release.outputs.source_sha }}" '
    '--expected-release-id "${{ needs.build-release.outputs.release_id }}" '
    '--expected-manifest-sha256 "${{ needs.build-release.outputs.manifest_sha256 }}" '
    '--expected-snapshot-sha256 "${{ needs.build-release.outputs.snapshot_sha256 }}" '
    '--artifact-id "${{ needs.build-release.outputs.artifact_id }}" '
    '--artifact-digest "${{ needs.build-release.outputs.artifact_digest }}" '
    '--run-id "${{ needs.build-release.outputs.run_id }}"'
)
EXPECTED_RUN_COMMANDS = {
    "release-please": [],
    "build-release": [
        (
            'python -m pip install --disable-pip-version-check "build>=1.2,<2" '
            '"twine>=7,<8" "dcc-mcp-core==0.20.14" "jsonschema>=4.17,<5"'
        ),
        "python -m build",
        "python -m twine check dist/* && python tools/wheel_smoke.py",
        (
            "python tools/release_guard.py capture "
            '--repository "${{ github.repository }}" '
            '--tag "${{ needs.release-please.outputs.tag_name }}" '
            '--source-sha "${{ github.sha }}"'
        ),
    ],
    "publish-pypi": [
        ARTIFACT_GUARD,
        f"python tools/release_guard.py verify {RELEASE_BINDINGS} --expect-no-assets",
    ],
    "publish-github-assets": [
        ARTIFACT_GUARD,
        f"python tools/release_guard.py publish-assets {RELEASE_BINDINGS}",
    ],
}
EXPECTED_STEP_SEQUENCES = {
    "release-please": ["action:googleapis/release-please-action"],
    "build-release": [
        "action:actions/checkout",
        "action:actions/setup-python",
        *(f"run:{command}" for command in EXPECTED_RUN_COMMANDS["build-release"]),
        "action:actions/upload-artifact",
    ],
    "publish-pypi": [
        "action:actions/checkout",
        "action:actions/setup-python",
        f"run:{EXPECTED_RUN_COMMANDS['publish-pypi'][0]}",
        "action:actions/download-artifact",
        f"run:{EXPECTED_RUN_COMMANDS['publish-pypi'][1]}",
        "action:pypa/gh-action-pypi-publish",
    ],
    "publish-github-assets": [
        "action:actions/checkout",
        "action:actions/setup-python",
        f"run:{EXPECTED_RUN_COMMANDS['publish-github-assets'][0]}",
        "action:actions/download-artifact",
        f"run:{EXPECTED_RUN_COMMANDS['publish-github-assets'][1]}",
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


def _canonical_shell(run: object) -> str:
    return re.sub(r"\s+", " ", _active_shell(run)).strip()


def _assert_immediate_guard(job: dict[str, Any], mutation_step: dict[str, Any]) -> None:
    steps = _steps(job)
    mutation_index = steps.index(mutation_step)
    assert mutation_index > 0
    guard = steps[mutation_index - 1]
    run = _active_shell(guard.get("run"))
    assert "tools/release_guard.py verify " in run
    assert "--expected-manifest-sha256" in run
    assert "--expected-snapshot-sha256" in run
    assert "--expected-release-id" in run
    assert "--artifact-id" in run
    assert "--artifact-digest" in run
    assert "--run-id" in run
    assert "--expect-no-assets" in run


def assert_release_workflow_contract(document: dict[str, Any]) -> None:
    assert set(document) == {"name", "on", "permissions", "jobs"}
    assert document.get("name") == "Release"
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

    assert set(release) == {"runs-on", "timeout-minutes", "permissions", "outputs", "steps"}
    assert set(build) == {
        "needs",
        "if",
        "runs-on",
        "timeout-minutes",
        "permissions",
        "outputs",
        "steps",
    }
    assert set(pypi) == {
        "needs",
        "runs-on",
        "timeout-minutes",
        "environment",
        "permissions",
        "steps",
    }
    assert set(github_assets) == {
        "needs",
        "runs-on",
        "timeout-minutes",
        "permissions",
        "steps",
    }
    assert release["runs-on"] == build["runs-on"] == pypi["runs-on"] == "ubuntu-latest"
    assert github_assets["runs-on"] == "ubuntu-latest"
    assert release["timeout-minutes"] == "10"
    assert build["timeout-minutes"] == "15"
    assert pypi["timeout-minutes"] == github_assets["timeout-minutes"] == "10"
    assert build["if"] == "${{ needs.release-please.outputs.release_created == 'true' }}"
    assert release["outputs"] == {
        "release_created": "${{ steps.release.outputs.release_created }}",
        "tag_name": "${{ steps.release.outputs.tag_name }}",
    }

    assert release.get("permissions") == {"contents": "write", "pull-requests": "write"}
    assert build.get("permissions") == {"contents": "read"}
    assert pypi.get("permissions") == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert github_assets.get("permissions") == {"actions": "read", "contents": "write"}
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
            assert step.get("if") is None
            assert step.get("continue-on-error") is None
            uses = step.get("uses")
            if uses is not None:
                assert set(step) <= {"id", "name", "uses", "with"}
                assert FULL_SHA_ACTION.fullmatch(str(uses)), f"action is not SHA-pinned: {uses}"
                repository, sha = str(uses).split("@", 1)
                assert repository in ALLOWED_ACTIONS
                assert PINNED_ACTIONS.get(repository) == sha, f"unexpected action pin: {uses}"
            else:
                assert set(step) <= {"id", "name", "run", "env"}
                command = _canonical_shell(step.get("run"))
                expected_env = (
                    {"GH_TOKEN": "${{ github.token }}"}
                    if "tools/release_guard.py" in command
                    else None
                )
                assert step.get("env") == expected_env

    workflow_text = "\n".join(_scalar_strings(document))
    assert "${{ secrets." not in workflow_text
    assert "PERSONAL_ACCESS_TOKEN" not in workflow_text
    assert "password:" not in workflow_text
    assert "api-token:" not in workflow_text

    release_action = _single_action(release, "googleapis/release-please-action")
    assert len(_all_action_steps(jobs, "googleapis/release-please-action")) == 1
    assert release_action.get("with", {}) == {
        "token": "${{ github.token }}",
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }

    setup_steps = _all_action_steps(jobs, "actions/setup-python")
    assert len(setup_steps) == 3
    assert all(step.get("with", {}) == {"python-version": "3.12"} for step in setup_steps)

    build_checkout = _single_action(build, "actions/checkout")
    assert build_checkout.get("with", {}) == {
        "ref": "${{ needs.release-please.outputs.tag_name }}",
        "fetch-depth": "1",
        "persist-credentials": "false",
    }
    build_commands = [_canonical_shell(step.get("run")) for step in _steps(build)]
    assert sum("python -m build" in command for command in build_commands) == 1
    assert any("python -m twine check dist/*" in command for command in build_commands)
    assert any("tools/release_guard.py capture" in command for command in build_commands)
    capture_command = next(
        command for command in build_commands if "tools/release_guard.py capture" in command
    )
    assert '--source-sha "${{ github.sha }}"' in capture_command

    upload = _single_action(build, "actions/upload-artifact")
    upload_inputs = upload.get("with", {})
    assert set(upload_inputs) == {
        "name",
        "path",
        "if-no-files-found",
        "compression-level",
        "retention-days",
        "overwrite",
    }
    assert upload_inputs["name"] == "release-distributions"
    assert set(str(upload_inputs["path"]).split()) == {"dist/", "release/"}
    assert upload_inputs["if-no-files-found"] == "error"
    assert upload_inputs["compression-level"] == "0"
    assert upload_inputs["retention-days"] == "1"
    assert upload_inputs["overwrite"] == "false"
    build_outputs = build.get("outputs", {})
    assert set(build_outputs) == {
        "tag_name",
        "source_sha",
        "release_id",
        "manifest_sha256",
        "snapshot_sha256",
        "artifact_id",
        "artifact_digest",
        "run_id",
    }
    assert build_outputs["artifact_id"] == "${{ steps.upload.outputs['artifact-id'] }}"
    assert build_outputs["artifact_digest"] == "${{ steps.upload.outputs['artifact-digest'] }}"
    assert build_outputs["run_id"] == "${{ github.run_id }}"
    capture_step = next(
        step for step in _steps(build) if "tools/release_guard.py capture" in str(step.get("run"))
    )
    assert _steps(build).index(upload) == _steps(build).index(capture_step) + 1

    assert len(_all_action_steps(jobs, "actions/checkout")) == 3
    for job in (pypi, github_assets):
        checkout = _single_action(job, "actions/checkout")
        assert checkout.get("with", {}) == {
            "ref": "${{ needs.build-release.outputs.source_sha }}",
            "fetch-depth": "1",
            "persist-credentials": "false",
        }
        download = _single_action(job, "actions/download-artifact")
        assert download.get("with", {}) == {
            "artifact-ids": "${{ needs.build-release.outputs.artifact_id }}",
            "path": ".",
        }
    assert len(_all_action_steps(jobs, "actions/download-artifact")) == 2
    assert len(_all_action_steps(jobs, "actions/upload-artifact")) == 1

    pypi_action = _single_action(pypi, "pypa/gh-action-pypi-publish")
    assert len(_all_action_steps(jobs, "pypa/gh-action-pypi-publish")) == 1
    assert pypi.get("environment", {}) == {
        "name": "pypi",
        "url": "https://pypi.org/p/dcc-mcp-material-maker",
    }
    assert pypi_action.get("continue-on-error") is None
    assert pypi_action.get("with", {}) == {
        "packages-dir": "dist",
        "skip-existing": "false",
        "verbose": "true",
        "print-hash": "true",
        "attestations": "true",
    }
    _assert_immediate_guard(pypi, pypi_action)

    assert github_assets.get("if") is None
    publish_commands = [
        _canonical_shell(step.get("run"))
        for step in _steps(github_assets)
        if "tools/release_guard.py publish-assets" in str(step.get("run", ""))
    ]
    assert len(publish_commands) == 1

    for job_name, job in jobs.items():
        active_commands = []
        step_sequence = []
        for step in _steps(job):
            uses = step.get("uses")
            if uses:
                step_sequence.append(f"action:{str(uses).split('@', 1)[0]}")
            command = _canonical_shell(step.get("run"))
            if not command:
                continue
            step_sequence.append(f"run:{command}")
            active_commands.append(command)
            for pattern in MUTATING_RUN_PATTERNS:
                assert pattern.search(command) is None, (
                    f"unexpected publication mutation path: {command}"
                )
        assert active_commands == EXPECTED_RUN_COMMANDS[job_name]
        assert step_sequence == EXPECTED_STEP_SEQUENCES[job_name]


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
    existing = next(
        step
        for step in duplicate_asset_mutation["jobs"]["publish-github-assets"]["steps"]
        if "tools/release_guard.py publish-assets" in str(step.get("run", ""))
    )
    duplicate_asset_mutation["jobs"]["publish-github-assets"]["steps"].append(
        copy.deepcopy(existing)
    )
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(duplicate_asset_mutation)

    misplaced_asset_mutation = copy.deepcopy(document)
    misplaced_asset_mutation["jobs"]["build-release"]["steps"].append(copy.deepcopy(existing))
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(misplaced_asset_mutation)


def test_contract_rejects_allowlisted_action_at_untrusted_sha() -> None:
    document = copy.deepcopy(_load_workflow())
    checkout = _single_action(document["jobs"]["build-release"], "actions/checkout")
    checkout["uses"] = "actions/checkout@" + "f" * 40
    with pytest.raises(AssertionError, match="unexpected action pin"):
        assert_release_workflow_contract(document)


@pytest.mark.parametrize(
    "case",
    [
        "always",
        "git-push",
        "gh-implicit-post",
        "curl-upload",
        "extra-secret",
        "repository-drift",
        "persisted-credentials",
        "extra-credentialed-checkout",
        "extra-job-token",
        "reordered-actions",
    ],
)
def test_contract_rejects_alternate_mutation_and_credential_routes(case: str) -> None:
    document = copy.deepcopy(_load_workflow())
    jobs = document["jobs"]
    capture = next(
        step
        for step in jobs["build-release"]["steps"]
        if "tools/release_guard.py capture" in str(step.get("run", ""))
    )
    build_checkout = _single_action(jobs["build-release"], "actions/checkout")

    if case == "always":
        _single_action(jobs["publish-pypi"], "pypa/gh-action-pypi-publish")["if"] = "always()"
    elif case == "git-push":
        capture["run"] += "\ngit push origin HEAD:main"
    elif case == "gh-implicit-post":
        capture["run"] += "\ngh api repos/other/project/releases -f tag_name=v9.9.9"
    elif case == "curl-upload":
        capture["run"] += "\ncurl -T dist/package.whl https://example.invalid/upload"
    elif case == "extra-secret":
        capture.setdefault("env", {})["EXTRA_TOKEN"] = "${{ secrets.EXTRA_TOKEN }}"
    elif case == "repository-drift":
        build_checkout.setdefault("with", {})["repository"] = "other/project"
    elif case == "persisted-credentials":
        build_checkout.setdefault("with", {})["persist-credentials"] = "true"
    elif case == "extra-credentialed-checkout":
        jobs["release-please"]["steps"].insert(
            0,
            {"uses": f"actions/checkout@{PINNED_ACTIONS['actions/checkout']}"},
        )
    elif case == "extra-job-token":
        jobs["build-release"]["env"] = {"GH_TOKEN": "${{ github.token }}"}
    elif case == "reordered-actions":
        steps = jobs["build-release"]["steps"]
        steps[0], steps[1] = steps[1], steps[0]
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    with pytest.raises(AssertionError):
        assert_release_workflow_contract(document)


def test_workflow_binds_exact_artifact_id_digest_run_and_source_for_each_consumer() -> None:
    jobs = _load_workflow()["jobs"]
    build_outputs = jobs["build-release"]["outputs"]
    assert build_outputs["artifact_id"] == "${{ steps.upload.outputs['artifact-id'] }}"
    assert build_outputs["artifact_digest"] == "${{ steps.upload.outputs['artifact-digest'] }}"
    assert build_outputs["run_id"] == "${{ github.run_id }}"

    for job_name in ("publish-pypi", "publish-github-assets"):
        steps = _steps(jobs[job_name])
        download = _single_action(jobs[job_name], "actions/download-artifact")
        assert download.get("with", {}) == {
            "artifact-ids": "${{ needs.build-release.outputs.artifact_id }}",
            "path": ".",
        }
        download_index = steps.index(download)
        assert download_index > 0
        metadata_guard = _active_shell(steps[download_index - 1].get("run"))
        assert "tools/release_guard.py verify-artifact " in metadata_guard
        for binding in ("--artifact-id", "--artifact-digest", "--source-sha", "--run-id"):
            assert binding in metadata_guard

        consumer = (
            _single_action(jobs[job_name], "pypa/gh-action-pypi-publish")
            if job_name == "publish-pypi"
            else next(
                step
                for step in steps
                if "tools/release_guard.py publish-assets " in _active_shell(step.get("run"))
            )
        )
        consumer_index = steps.index(consumer)
        assert consumer_index > download_index
        immediate_guard = _active_shell(steps[consumer_index - 1].get("run"))
        if job_name == "publish-pypi":
            for binding in ("--artifact-id", "--artifact-digest", "--run-id"):
                assert binding in immediate_guard
        else:
            mutation = _active_shell(consumer.get("run"))
            for binding in ("--artifact-id", "--artifact-digest", "--run-id"):
                assert binding in mutation
