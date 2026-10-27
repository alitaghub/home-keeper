"""The collector processes run without any controller code."""

import importlib.util
import subprocess
import sys

import pytest

LIST_MODULES = "import sys, {module}; print('\\n'.join(sys.modules))"


@pytest.mark.parametrize("module", ["home_keeper.live", "home_keeper.demo"])
def test_collector_imports_no_controller_module(module: str) -> None:
    # The check means nothing unless the controller package is installed beside the collector.
    assert importlib.util.find_spec("home_keeper_controller") is not None
    loaded = subprocess.run(  # noqa: S603 -- this interpreter with a fixed module name
        [sys.executable, "-c", LIST_MODULES.format(module=module)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.split()
    assert module in loaded
    assert [name for name in loaded if name.startswith("home_keeper_controller")] == []
