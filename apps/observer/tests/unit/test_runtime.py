from home_observer.runtime import local_authorities


def test_default_http_port_is_omitted_like_clients_omit_it() -> None:
    assert local_authorities(80, ("localhost", "api")) == ("localhost", "api")


def test_other_ports_are_part_of_the_authority() -> None:
    assert local_authorities(8000, ("localhost", "api")) == ("localhost:8000", "api:8000")
