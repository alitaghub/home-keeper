import httpx
import pytest

from home_observer_agent import telegram


@pytest.mark.parametrize("fatal_code", [None, 401, 404, 409])
def test_temporary_startup_failure_recovers_without_process_restart(
    monkeypatch: pytest.MonkeyPatch,
    fatal_code: int | None,
) -> None:
    requests: list[str] = []
    delays: list[float] = []

    def transport(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        requests.append(method)
        if fatal_code is not None:
            return httpx.Response(fatal_code, json={"ok": False, "error_code": fatal_code})
        if len(requests) == 1:
            raise httpx.ConnectError("temporary outage", request=request)
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        assert method == "getUpdates"
        raise KeyboardInterrupt

    real_bot = telegram.TelegramBot

    def make_bot(settings: telegram.Settings) -> telegram.TelegramBot:
        return real_bot(
            settings,
            telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
            agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
        )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:synthetic-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("TELEGRAM_STATE_PATH", ":memory:")
    monkeypatch.setenv("TELEGRAM_API_URL", "https://telegram.test")
    monkeypatch.setattr(telegram, "TelegramBot", make_bot)
    monkeypatch.setattr(telegram.time, "sleep", delays.append)
    exit_code = None
    try:
        telegram.main()
    except SystemExit as exc:
        exit_code = exc.code

    if fatal_code is not None:
        assert exit_code == 1
        assert requests == ["getWebhookInfo"]
        assert delays == []
        return
    assert exit_code is None, (
        f"Temporary startup failure stopped worker: exit={exit_code}, "
        f"requests={requests}, retry_delays={delays}"
    )
    assert "getUpdates" in requests
    assert delays
