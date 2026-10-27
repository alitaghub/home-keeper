import importlib
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
