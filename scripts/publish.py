"""Publish tested release assets and container images from GitHub Actions."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
RELEASE_QUERY = """
query($owner: String!, $name: String!, $tag: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) { target { __typename oid } }
    release(tagName: $tag) { isDraft }
    latestRelease { tagName }
  }
}
"""


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def command_error(exc: subprocess.CalledProcessError) -> str:
    details = (exc.stderr or "").strip()
    token = os.environ.get("GH_TOKEN")
    if token:
        details = details.replace(token, "[REDACTED]")
    message = f"Publishing stopped: {' '.join(exc.cmd[:3])} failed (exit {exc.returncode})."
    return f"{message}\n{details[:2000]}" if details else message


def tag_commit(repository: str, target: dict) -> str:
    """Resolve both lightweight and annotated tags to their commit."""
    kind, sha = target["__typename"], target["oid"]
    seen = set()
    while kind == "Tag":
        if sha in seen:
            raise ValueError("The release tag contains a circular reference.")
        seen.add(sha)
        target = json.loads(command("gh", "api", f"repos/{repository}/git/tags/{sha}"))["object"]
        kind = {"commit": "Commit", "tag": "Tag"}.get(target["type"])
        sha = target["sha"]
    if kind != "Commit":
        raise ValueError("The release tag does not point to a commit.")
    return sha


def publish(directory: Path) -> None:
    version = os.environ.get("RELEASE_VERSION", "")
    parsed_version = VERSION.fullmatch(version)
    if not parsed_version:
        raise ValueError("RELEASE_VERSION must use stable X.Y.Z format.")
    sha = os.environ.get("GITHUB_SHA", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GITHUB_SHA must contain the full commit SHA.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must contain owner/repository.")
    if not re.fullmatch(r"[1-9]\d*", run_number):
        raise ValueError("GITHUB_RUN_NUMBER must contain a positive run number.")
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("GH_TOKEN is required to publish the release.")

    directory = directory.resolve()
    files = [
        directory / f"ochecore-{version}-py3-none-any.whl",
        directory / f"ochecore-{version}.tar.gz",
        directory / "ochecore-linux-amd64.tar.gz",
        directory / "ochecore-linux-arm64.tar.gz",
    ]
    for path in files:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Required release asset is missing or empty: {path.name}")

    tag = f"v{version}"
    owner, name = repository.split("/")
    response = json.loads(
        command(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={RELEASE_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"tag={tag}",
            "-f",
            f"ref=refs/tags/{tag}",
        )
    )
    if response.get("errors") or not response.get("data", {}).get("repository"):
        raise ValueError("Cannot inspect the release repository.")
    state = response["data"]["repository"]
    ref, release = state["ref"], state["release"]
    if release and not ref:
        raise ValueError("The existing release has no tag. Restore its tag before retrying.")
    if ref and tag_commit(repository, ref["target"]) != sha:
        raise ValueError(f"{tag} already points to a different commit. Bump the version.")
    if release and not release["isDraft"]:
        print(f"{tag} is already published at this commit; nothing to change.")
        return

    latest = state["latestRelease"]
    latest_version = VERSION.fullmatch(latest["tagName"].removeprefix("v")) if latest else None
    promote_latest = not latest or (
        latest_version is not None
        and tuple(map(int, parsed_version.groups())) >= tuple(map(int, latest_version.groups()))
    )

    checksums = directory / "SHA256SUMS"
    lines = []
    for path in files:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    checksums.write_text("".join(lines), encoding="utf-8")

    if not ref:
        command(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/git/refs",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        )
    if not release:
        command(
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--verify-tag",
            "--draft",
            "--generate-notes",
            "--title",
            f"OcheCore {tag}",
        )
    print(f"Uploading release assets for {tag}...", flush=True)
    command(
        "gh",
        "release",
        "upload",
        tag,
        "--repo",
        repository,
        "--clobber",
        *(str(path) for path in [*files, checksums]),
    )

    image = f"ghcr.io/{repository.lower()}"
    images = []
    for arch in ("amd64", "arm64"):
        target = f"{image}:{version}-{arch}"
        print(f"Publishing linux/{arch} image...", flush=True)
        command("docker", "load", "--input", str(directory / f"ochecore-linux-{arch}.tar.gz"))
        command("docker", "tag", f"ochecore:build-{run_number}-{arch}", target)
        command("docker", "push", target)
        images.append(target)
    tags = ["--tag", f"{image}:{version}"]
    if promote_latest:
        tags.extend(["--tag", f"{image}:latest"])
    command("docker", "buildx", "imagetools", "create", *tags, *images)
    print(f"Publishing release {tag}...", flush=True)
    command(
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        repository,
        "--draft=false",
        f"--latest={str(promote_latest).lower()}",
    )
    print(f"Published {tag} and {image}:{version}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/publish.py ASSET_DIRECTORY")
    try:
        publish(Path(sys.argv[1]))
    except subprocess.CalledProcessError as exc:
        sys.exit(command_error(exc))
    except (KeyError, OSError, ValueError) as exc:
        sys.exit(f"Publishing stopped: {exc}")
