import pytest

from home_observer.runtime import local_authorities, parse_origin


def test_default_http_port_is_omitted_like_clients_omit_it() -> None:
    assert local_authorities(80, ("localhost", "api")) == ("localhost", "api")


def test_other_ports_are_part_of_the_authority() -> None:
    assert local_authorities(8000, ("localhost", "api")) == ("localhost:8000", "api:8000")


def test_configured_url_is_reduced_to_its_origin() -> None:
    assert parse_origin("URL", "http://127.0.0.1:8123/") == "http://127.0.0.1:8123"
    assert parse_origin("URL", "") is None


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "http://user:pass@ha:8123", "http://ha:8123/api", "http://ha:99999"],
)
def test_configured_url_rejects_anything_but_an_http_origin(value: str) -> None:
    with pytest.raises(ValueError, match="URL must be an http"):
        parse_origin("URL", value)
