"""scripts/pr_images.py puts screenshots where a reviewer reads them, under links GitHub serves."""

import json
import sys
from pathlib import Path

import pr_images
import pytest
from pr_images import image_markdown, stored_name, with_images

TEMPLATE_BODY = """## Change

The dashboard shows gaps.

## Screenshots

<!-- UI changes only. -->

## Proof

- `mise run check` passes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"""


def test_images_go_under_screenshots_before_the_next_section() -> None:
    body = with_images(TEMPLATE_BODY, "![after](url)")

    assert "<!-- UI changes only. -->\n\n![after](url)\n\n## Proof" in body
    assert body.endswith("(https://claude.com/claude-code)")


def test_a_second_upload_adds_below_the_first() -> None:
    body = with_images(with_images(TEMPLATE_BODY, "![before](a)"), "![after](b)")

    assert "![before](a)\n\n![after](b)\n\n## Proof" in body


def test_a_body_without_the_section_gets_one_at_the_end() -> None:
    assert with_images("## Change\n\nText.\n", "![x](u)") == (
        "## Change\n\nText.\n\n## Screenshots\n\n![x](u)\n"
    )
    assert with_images(None, "![x](u)") == "## Screenshots\n\n![x](u)\n"


def test_file_names_become_plain_link_paths() -> None:
    assert stored_name(Path("shots/Before [dark] mode.PNG")) == "Before-dark-mode.png"
    assert stored_name(Path("...png")) == "image.png"


def test_links_pin_the_commit_and_keep_the_file_name_as_alt_text() -> None:
    markdown = image_markdown("o/r", "abc123", [Path("a/[x] before.png"), Path("after.png")])

    assert markdown == (
        r"![\[x\] before](https://github.com/o/r/blob/abc123/x-before.png?raw=true)"
        "\n\n![after](https://github.com/o/r/blob/abc123/after.png?raw=true)"
    )


class FakeGitHub:
    """Answers the git data API calls `upload` makes and records them in order."""

    def __init__(self, existing_ref: str | None) -> None:
        self.existing_ref = existing_ref
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(self, method: str, route: str, body: dict[str, object] | None = None) -> str:
        self.calls.append((method, route, body))
        if route.endswith("/matching-refs/screenshots/pr-1"):
            # The prefix match also returns pr-12, which must not become the parent.
            refs = [{"ref": "refs/screenshots/pr-12", "object": {"sha": "other"}}]
            if self.existing_ref:
                refs.append({"ref": "refs/screenshots/pr-1", "object": {"sha": self.existing_ref}})
            return json.dumps(refs)
        if route.endswith("/git/commits/parent"):
            return json.dumps({"tree": {"sha": "parent-tree"}})
        return json.dumps({"sha": f"new-{route.rsplit('/', 1)[-1]}"})


@pytest.mark.parametrize("existing_ref", [None, "parent"])
def test_upload_commits_on_top_of_the_pr_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_ref: str | None
) -> None:
    image = tmp_path / "after.png"
    image.write_bytes(b"png")
    github = FakeGitHub(existing_ref)
    monkeypatch.setattr(pr_images, "gh_api", github)

    assert pr_images.upload("o/r", "screenshots/pr-1", [image]) == "new-commits"

    writes = {route: body for method, route, body in github.calls if method != "GET"}
    tree, commit = writes["repos/o/r/git/trees"], writes["repos/o/r/git/commits"]
    assert tree is not None
    assert commit is not None
    if existing_ref:
        assert tree["base_tree"] == "parent-tree"
        assert commit["parents"] == ["parent"]
        assert github.calls[-1] == (
            "PATCH",
            "repos/o/r/git/refs/screenshots/pr-1",
            {"sha": "new-commits", "force": False},
        )
    else:
        assert "base_tree" not in tree
        assert commit["parents"] == []
        assert github.calls[-1] == (
            "POST",
            "repos/o/r/git/refs",
            {"ref": "refs/screenshots/pr-1", "sha": "new-commits"},
        )


def test_names_that_collide_after_cleanup_stop_before_any_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [tmp_path / "Before [dark].png", tmp_path / "Before (dark).png"]
    for file in files:
        file.write_bytes(b"png")
    monkeypatch.setattr(sys, "argv", ["pr_images.py", "--pr", "1", *map(str, files)])
    monkeypatch.setattr(pr_images.subprocess, "run", pytest.fail)

    with pytest.raises(SystemExit, match=r"Two files upload as Before-dark\.png"):
        pr_images.main()
