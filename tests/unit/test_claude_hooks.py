"""The Claude Code hooks in .claude/hooks behave as .claude/settings.json promises."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
# Claude Code blocks the tool call when a PreToolUse hook exits 2.
BLOCKED = 2


def run_hook(name: str, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"CLAUDE_PROJECT_DIR": str(REPO)},
        timeout=30,
    )


def test_settings_wire_only_hooks_that_exist() -> None:
    settings = (REPO / ".claude" / "settings.json").read_text()
    wired = re.findall(r"\.claude/hooks/([\w-]+\.sh)", settings)
    assert wired
    assert sorted(wired) == sorted(path.name for path in HOOKS.glob("*.sh"))


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m wip",
        "git push --no-verify",
        "git commit -n -m wip",
        "git commit -anm wip",
        "cd apps && git -C .. commit '--no-verify' -m wip",
        "env SKIP=1 git commit --no-verify",
    ],
)
def test_no_verify_guard_blocks_hook_bypass(command: str) -> None:
    result = run_hook("no-verify-guard.sh", {"tool_input": {"command": command}})
    assert result.returncode == BLOCKED
    assert "Fix the failing hook" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'never use --no-verify'",
        "git commit -F - <<'EOF'\nskip --no-verify\nEOF",
        "grep -n -- --no-verify AGENTS.md",
        "git log -n 3",
        "git commit -m wip",
    ],
)
def test_no_verify_guard_allows_commands_that_only_mention_it(command: str) -> None:
    result = run_hook("no-verify-guard.sh", {"tool_input": {"command": command}})
    assert result.returncode == 0, result.stderr


def test_format_hook_formats_the_edited_python_file(tmp_path: Path) -> None:
    edited = tmp_path / "edited.py"
    edited.write_text('import sys\nimport os\nx=  {"a":1}\n')
    result = run_hook("format-edited-file.sh", {"tool_input": {"file_path": str(edited)}})
    assert result.returncode == 0
    assert edited.read_text() == 'import os\nimport sys\n\nx = {"a": 1}\n'
