"""scripts/pr_images.py puts screenshots where a reviewer reads them, under links GitHub serves."""

from pathlib import Path

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
