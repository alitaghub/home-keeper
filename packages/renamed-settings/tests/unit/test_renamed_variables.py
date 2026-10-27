import pytest

from home_keeper_renamed import RenamedVariableError, reject_renamed_variables


def test_environment_without_old_variables_passes() -> None:
    assert reject_renamed_variables({"HOME_KEEPER_PORT": "8000", "PATH": "/bin"}) is None


def test_old_variable_is_rejected_with_its_replacement() -> None:
    with pytest.raises(RenamedVariableError, match="OBSERVER_PORT was renamed to HOME_KEEPER_PORT"):
        reject_renamed_variables({"OBSERVER_PORT": "9000"})


def test_every_old_variable_is_named_in_one_error() -> None:
    with pytest.raises(RenamedVariableError) as caught:
        reject_renamed_variables({"OBSERVER_URL": "x", "OBSERVER_PORT": "1", "HOME_KEEPER_DB": "y"})
    message = str(caught.value)
    assert "OBSERVER_PORT was renamed to HOME_KEEPER_PORT" in message
    assert "OBSERVER_URL was renamed to HOME_KEEPER_URL" in message
    assert message.index("OBSERVER_PORT") < message.index("OBSERVER_URL")  # sorted
    assert "HOME_KEEPER_DB was" not in message


def test_old_variable_with_empty_value_is_ignored() -> None:
    assert reject_renamed_variables({"OBSERVER_PORT": ""}) is None


def test_error_is_a_value_error() -> None:
    assert issubclass(RenamedVariableError, ValueError)
