"""HttpReader reports why the reader service could not read Home Assistant."""

from collections.abc import Callable
from functools import partial

import httpx
import pytest

from home_keeper import reader as reader_module
from home_keeper.reader import HttpReader, ReaderError, ReaderUnavailable


def reader_behind(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> HttpReader:
    # HttpReader builds its own client; its real options stay, only the network is fake.
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(reader_module.httpx, "Client", partial(httpx.Client, transport=transport))
    return HttpReader("http://reader.internal", "service-token")


@pytest.mark.parametrize("error", ["authentication", "timeout"])
def test_a_reader_503_keeps_the_home_assistant_cause(
    monkeypatch: pytest.MonkeyPatch, error: ReaderError
) -> None:
    reader = reader_behind(monkeypatch, lambda _: httpx.Response(503, json={"error": error}))

    with pytest.raises(ReaderUnavailable) as raised:
        reader.health()

    assert raised.value.error == error


def test_a_reader_that_does_not_answer_is_reader_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    reader = reader_behind(monkeypatch, refuse)

    with pytest.raises(ReaderUnavailable) as raised:
        reader.health()

    assert raised.value.error == "reader_unreachable"
