import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "publish", Path(__file__).parents[1] / "scripts" / "publish.py"
)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

SHA = "a" * 40
VERSION = "0.1.10"
TAG = f"v{VERSION}"
REPOSITORY = "Example/OcheCore"
IMAGE = "ghcr.io/example/ochecore"


@pytest.fixture
def assets(tmp_path, monkeypatch):
    for key, value in {
        "RELEASE_VERSION": VERSION,
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_NUMBER": "42",
        "GH_TOKEN": "test-token",
    }.items():
        monkeypatch.setenv(key, value)
    for name in (
        f"ochecore-{VERSION}-py3-none-any.whl",
        f"ochecore-{VERSION}.tar.gz",
        "ochecore-linux-amd64.tar.gz",
        "ochecore-linux-arm64.tar.gz",
    ):
        (tmp_path / name).write_bytes(name.encode())
    return tmp_path


@pytest.fixture
def remote(monkeypatch):
    remote = SimpleNamespace(
        calls=[],
        state={"ref": None, "release": None, "latestRelease": None},
        tags={},
        fail=None,
    )

    def command(*args):
        remote.calls.append(args)
        if remote.fail and args[: len(remote.fail)] == remote.fail:
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ("gh", "api", "graphql"):
            return json.dumps({"data": {"repository": remote.state}})
        if args[:2] == ("gh", "api") and "/git/tags/" in args[2]:
            return json.dumps({"object": remote.tags[args[2].rsplit("/", 1)[1]]})
        return ""

    monkeypatch.setattr(publisher, "command", command)
    return remote


def existing_tag(remote, sha=SHA):
    remote.state["ref"] = {"target": {"__typename": "Commit", "oid": sha}}


def test_new_release_uploads_verified_assets_and_publishes_last(assets, remote):
    publisher.publish(assets)

    assert remote.calls[1] == (
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{REPOSITORY}/git/refs",
        "-f",
        f"ref=refs/tags/{TAG}",
        "-f",
        f"sha={SHA}",
    )
    assert remote.calls[2][:4] == ("gh", "release", "create", TAG)
    assert {"--draft", "--verify-tag", "--generate-notes"} <= set(remote.calls[2])
    upload = remote.calls[3]
    assert upload[:7] == ("gh", "release", "upload", TAG, "--repo", REPOSITORY, "--clobber")
    assert {Path(path).name for path in upload[7:]} == {path.name for path in assets.iterdir()}
    expected_checksums = "".join(
        f"{hashlib.sha256(Path(path).read_bytes()).hexdigest()}  {Path(path).name}\n"
        for path in upload[7:-1]
    )
    assert (assets / "SHA256SUMS").read_text() == expected_checksums
    for arch in ("amd64", "arm64"):
        assert (
            "docker",
            "load",
            "--input",
            str(assets / f"ochecore-linux-{arch}.tar.gz"),
        ) in remote.calls
        assert (
            "docker",
            "tag",
            f"ochecore:build-42-{arch}",
            f"{IMAGE}:{VERSION}-{arch}",
        ) in remote.calls
        assert ("docker", "push", f"{IMAGE}:{VERSION}-{arch}") in remote.calls
    assert remote.calls[-2] == (
        "docker",
        "buildx",
        "imagetools",
        "create",
        "--tag",
        f"{IMAGE}:{VERSION}",
        "--tag",
        f"{IMAGE}:latest",
        f"{IMAGE}:{VERSION}-amd64",
        f"{IMAGE}:{VERSION}-arm64",
    )
    assert remote.calls[-1] == (
        "gh",
        "release",
        "edit",
        TAG,
        "--repo",
        REPOSITORY,
        "--draft=false",
        "--latest=true",
    )


def test_published_release_at_same_commit_is_a_noop(assets, remote):
    existing_tag(remote)
    remote.state["release"] = {"isDraft": False}
    publisher.publish(assets)
    assert len(remote.calls) == 1
    assert not (assets / "SHA256SUMS").exists()


def test_retry_reuses_draft_without_moving_tag(assets, remote):
    existing_tag(remote)
    remote.state["release"] = {"isDraft": True}
    publisher.publish(assets)
    assert remote.calls[1][:4] == ("gh", "release", "upload", TAG)
    assert "--clobber" in remote.calls[1]
    assert remote.calls[-1][:4] == ("gh", "release", "edit", TAG)


def test_tag_pointing_to_other_commit_fails_before_mutation(assets, remote):
    existing_tag(remote, "b" * 40)
    remote.state["release"] = {"isDraft": False}
    with pytest.raises(ValueError, match="different commit"):
        publisher.publish(assets)
    assert len(remote.calls) == 1
    assert not (assets / "SHA256SUMS").exists()


def test_release_without_tag_fails_before_mutation(assets, remote):
    remote.state["release"] = {"isDraft": True}
    with pytest.raises(ValueError, match="has no tag"):
        publisher.publish(assets)
    assert len(remote.calls) == 1


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("index", range(4))
def test_missing_or_empty_required_asset_stops_before_remote_calls(assets, remote, empty, index):
    path = sorted(assets.iterdir())[index]
    if empty:
        path.write_bytes(b"")
    else:
        path.unlink()
    with pytest.raises(ValueError, match="missing or empty"):
        publisher.publish(assets)
    assert not remote.calls


@pytest.mark.parametrize(
    "failure",
    [
        ("gh", "release", "upload"),
        ("docker", "push"),
        ("docker", "buildx", "imagetools", "create"),
    ],
)
def test_downstream_failure_leaves_release_as_draft(assets, remote, failure):
    remote.fail = failure
    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish(assets)
    assert any(call[:3] == ("gh", "release", "create") for call in remote.calls)
    assert not any(call[:3] == ("gh", "release", "edit") for call in remote.calls)


@pytest.mark.parametrize("latest", ["v0.1.11", "nightly", "v0.2.0rc1"])
def test_old_or_unknown_latest_version_is_not_replaced(assets, remote, latest):
    remote.state["latestRelease"] = {"tagName": latest}
    publisher.publish(assets)
    assert f"{IMAGE}:latest" not in remote.calls[-2]
    assert "--latest=false" in remote.calls[-1]


@pytest.mark.parametrize("latest", ["v0.1.9", TAG])
def test_latest_comparison_is_numeric_and_accepts_same_version(assets, remote, latest):
    remote.state["latestRelease"] = {"tagName": latest}
    publisher.publish(assets)
    assert f"{IMAGE}:latest" in remote.calls[-2]
    assert "--latest=true" in remote.calls[-1]


@pytest.mark.parametrize("commit", [SHA, "b" * 40])
def test_annotated_tags_resolve_to_commit(assets, remote, commit):
    oid = "c" * 40
    remote.state["ref"] = {"target": {"__typename": "Tag", "oid": oid}}
    remote.state["release"] = {"isDraft": False}
    remote.tags[oid] = {"type": "commit", "sha": commit}
    if commit == SHA:
        publisher.publish(assets)
    else:
        with pytest.raises(ValueError, match="different commit"):
            publisher.publish(assets)
    assert remote.calls[-1] == ("gh", "api", f"repos/{REPOSITORY}/git/tags/{oid}")
    assert len(remote.calls) == 2


@pytest.mark.parametrize("version", ["", "v0.1.1", "0.1.1rc1", "0.1", "01.1.1"])
def test_invalid_version_is_rejected_without_remote_calls(assets, remote, monkeypatch, version):
    monkeypatch.setenv("RELEASE_VERSION", version)
    with pytest.raises(ValueError, match="stable X.Y.Z"):
        publisher.publish(assets)
    assert not remote.calls


def test_remote_query_failure_does_not_create_release(assets, remote):
    remote.fail = ("gh", "api", "graphql")
    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish(assets)
    assert len(remote.calls) == 1


def test_command_failure_explains_error_without_exposing_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "private-token")
    error = subprocess.CalledProcessError(
        1,
        ["gh", "release", "upload"],
        stderr="HTTP 403: permission denied with token private-token\n" + "detail " * 500,
    )
    message = publisher.command_error(error)
    assert "gh release upload failed" in message
    assert "HTTP 403: permission denied" in message
    assert "private-token" not in message
    assert "[REDACTED]" in message
    assert len(message.split("\n", 1)[1]) == 2000
