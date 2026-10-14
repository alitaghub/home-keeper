"""Upload screenshots for a pull request and show them in its description.

Usage: mise run pr:images -- --pr <number> <image>...

The images are committed to refs/screenshots/pr-<number>, outside refs/heads so a
plain `git fetch` skips them. The links pin that commit, so a later upload never
changes what an older description shows. The images go under the description's
`## Screenshots` heading, which is added at the end when it is missing.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SECTION = "## Screenshots"


class Sha(BaseModel):
    sha: str


class Commit(BaseModel):
    tree: Sha


class MatchingRef(BaseModel):
    ref: str
    object: Sha


class Pull(BaseModel):
    body: str | None


def stored_name(file: Path) -> str:
    """Letters, digits, `.`, `_` and `-` only, so GitHub serves the link without encoding."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", file.stem).strip("-.") or "image"
    return f"{stem}{file.suffix.lower()}"


def image_markdown(repo: str, commit: str, files: list[Path]) -> str:
    def line(file: Path) -> str:
        alt = file.stem.replace("[", "\\[").replace("]", "\\]")
        return f"![{alt}](https://github.com/{repo}/blob/{commit}/{stored_name(file)}?raw=true)"

    return "\n\n".join(map(line, files))


def with_images(body: str | None, markdown: str) -> str:
    """Adds the images at the end of the Screenshots section, before the next heading."""
    text = (body or "").rstrip()
    start = text.find(SECTION)
    if start == -1:
        return f"{text}\n\n{SECTION}\n\n{markdown}\n".lstrip()
    next_heading = text.find("\n## ", start + len(SECTION))
    end = len(text) if next_heading == -1 else next_heading
    return f"{text[:end].rstrip()}\n\n{markdown}\n{text[end:]}"


def gh_api(method: str, route: str, body: dict[str, object] | None = None) -> str:
    # `gh pr edit` is avoided: the API calls below need no extra scopes or GraphQL.
    result = subprocess.run(
        ["gh", "api", "--method", method, route, *(["--input", "-"] if body else [])],
        input=json.dumps(body) if body else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"pr:images: {method} {route} failed: {result.stderr.strip()}")
    return result.stdout


def upload(repo: str, ref: str, files: list[Path]) -> str:
    """Commits the images on top of the ref and returns the new commit's SHA."""
    # matching-refs answers [] for a missing ref, where git/ref would answer 404.
    matches = json.loads(gh_api("GET", f"repos/{repo}/git/matching-refs/{ref}"))
    parent = next(
        (m.object.sha for m in map(MatchingRef.model_validate, matches) if m.ref == f"refs/{ref}"),
        None,
    )
    entries: list[dict[str, object]] = [
        {
            "path": stored_name(file),
            "mode": "100644",
            "type": "blob",
            "sha": Sha.model_validate_json(
                gh_api(
                    "POST",
                    f"repos/{repo}/git/blobs",
                    {"content": base64.b64encode(file.read_bytes()).decode(), "encoding": "base64"},
                )
            ).sha,
        }
        for file in files
    ]
    tree_request: dict[str, object] = {"tree": entries}
    if parent:
        parent_commit = gh_api("GET", f"repos/{repo}/git/commits/{parent}")
        tree_request["base_tree"] = Commit.model_validate_json(parent_commit).tree.sha
    tree = Sha.model_validate_json(gh_api("POST", f"repos/{repo}/git/trees", tree_request)).sha
    commit = Sha.model_validate_json(
        gh_api(
            "POST",
            f"repos/{repo}/git/commits",
            {
                "message": f"Screenshots for {ref}",
                "tree": tree,
                "parents": [parent] if parent else [],
            },
        )
    ).sha
    if parent:
        gh_api("PATCH", f"repos/{repo}/git/refs/{ref}", {"sha": commit, "force": False})
    else:
        gh_api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/{ref}", "sha": commit})
    return commit


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mise run pr:images --", description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument("images", type=Path, nargs="+")
    args = parser.parse_args()
    pr: int = args.pr
    images: list[Path] = args.images

    problems = [f"No such file: {f}" for f in images if not f.is_file()]
    problems += [
        f"Not an image ({', '.join(sorted(IMAGE_SUFFIXES))}): {f}"
        for f in images
        if f.suffix.lower() not in IMAGE_SUFFIXES
    ]
    names = [stored_name(f) for f in images]
    problems += [
        f"Two files upload as {n}; rename one"
        for n in sorted({n for n in names if names.count(n) > 1})
    ]
    if problems:
        sys.exit("\n".join(problems))

    repo = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    markdown = image_markdown(repo, upload(repo, f"screenshots/pr-{pr}", images), images)
    pull = Pull.model_validate_json(gh_api("GET", f"repos/{repo}/pulls/{pr}"))
    gh_api("PATCH", f"repos/{repo}/pulls/{pr}", {"body": with_images(pull.body, markdown)})
    print(markdown)
    print(f"Added to pull request #{pr}.", file=sys.stderr)


if __name__ == "__main__":
    main()
