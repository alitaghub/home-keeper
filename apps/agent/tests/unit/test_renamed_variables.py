import importlib
import sys
from collections.abc import Callable

import pytest

from home_keeper_renamed import RenamedVariableError


@pytest.mark.parametrize("entry_point", ["home_keeper_agent.serve", "home_keeper_agent.telegram"])
def test_entry_point_rejects_old_variable_before_doing_anything_else(
    monkeypatch: pytest.MonkeyPatch, entry_point: str
) -> None:
    monkeypatch.setenv("OBSERVER_PORT", "9000")
    main: Callable[[], object] = importlib.import_module(entry_point).main
    with pytest.raises(RenamedVariableError, match="HOME_KEEPER_PORT"):
        main()


def test_aegra_graph_module_rejects_old_variable_on_import(monkeypatch: pytest.MonkeyPatch) -> None:
    # `aegra dev` imports server.py directly, so the check must run at import.
    monkeypatch.setenv("OBSERVER_URL", "http://api:8000")
    monkeypatch.delitem(sys.modules, "home_keeper_agent.server", raising=False)
    with pytest.raises(RenamedVariableError, match="HOME_KEEPER_URL"):
        importlib.import_module("home_keeper_agent.server")
