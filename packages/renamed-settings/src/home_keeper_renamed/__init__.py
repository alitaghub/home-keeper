"""Fail fast on `OBSERVER_*` variables, which an upgraded `.env` can still hold.

Ignoring them would silently fall back to defaults. See docs/upgrade.md.
"""

from collections.abc import Mapping

# Every variable kept its suffix; only the prefix changed.
OLD_PREFIX = "OBSERVER_"
NEW_PREFIX = "HOME_KEEPER_"


class RenamedVariableError(ValueError):
    """An environment variable still uses its name from before the rename."""


def reject_renamed_variables(environ: Mapping[str, str]) -> None:
    """Raise RenamedVariableError naming the replacement of every old variable that is set."""
    # Compose leaves an unset variable out; an empty value is treated as unset too.
    old = sorted(name for name, value in environ.items() if name.startswith(OLD_PREFIX) and value)
    if old:
        renames = "; ".join(
            f"{name} was renamed to {NEW_PREFIX}{name.removeprefix(OLD_PREFIX)}" for name in old
        )
        raise RenamedVariableError(f"{renames}. See docs/upgrade.md.")
