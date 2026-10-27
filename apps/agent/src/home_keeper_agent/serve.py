"""Container entry point: write Aegra's config for CHAT_ORIGIN, then run `aegra serve`.

CORS must allow exactly the chat origin, and aegra.json cannot read the environment.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from home_keeper_renamed import reject_renamed_variables

DEFAULT_CHAT_ORIGIN = "http://127.0.0.1:3000"


def parse_origin(value: str) -> str:
    """Return an http(s) origin such as http://127.0.0.1:3000, or raise ValueError."""
    error = ValueError(f"CHAT_ORIGIN must be an http(s) origin without a path, got {value!r}")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise error from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise error
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def aegra_config(origin: str, graph_file: Path) -> dict[str, object]:
    # Aegra resolves graph paths relative to the config file, so the path is absolute.
    return {
        "graphs": {"agent": f"{graph_file.resolve()}:agent"},
        "http": {"cors": {"allow_origins": [origin]}},
    }


def main() -> None:
    reject_renamed_variables(os.environ)
    try:
        origin = parse_origin(os.environ.get("CHAT_ORIGIN", "").strip() or DEFAULT_CHAT_ORIGIN)
    except ValueError as error:
        sys.exit(str(error))
    config = Path(tempfile.gettempdir()) / "aegra.json"
    config.write_text(json.dumps(aegra_config(origin, Path(__file__).with_name("server.py"))))
    # `aegra serve` ignores an AEGRA_CONFIG outside the working directory; --config sets it.
    command = ["aegra", "serve", "--host", "0.0.0.0", "--port", "2026", "--config", str(config)]  # noqa: S104 -- the container binds all interfaces; Compose publishes loopback only
    os.execvp(command[0], command)  # noqa: S606 -- fixed command, no shell


if __name__ == "__main__":
    main()
