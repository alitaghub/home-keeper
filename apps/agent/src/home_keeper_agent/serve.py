"""Container entry point: write Aegra's config for CHAT_ORIGIN, pass on its database
password, then run `aegra serve`.

CORS must allow exactly the chat origin, and aegra.json cannot read the environment.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from home_keeper_agent.settings import AgentSettings
from home_keeper_settings import SettingsError, load

DEFAULT_CHAT_ORIGIN = "http://localhost:3000"


def aegra_config(origin: str, graph_file: Path) -> dict[str, object]:
    # Aegra resolves graph paths relative to the config file, so the path is absolute.
    return {
        "graphs": {"agent": f"{graph_file.resolve()}:agent"},
        "http": {"cors": {"allow_origins": [origin]}},
    }


def main() -> None:
    try:
        settings = load(AgentSettings)
    except SettingsError as error:
        sys.exit(f"Agent configuration invalid: {error}")
    origin = settings.chat_origin or DEFAULT_CHAT_ORIGIN
    if settings.database_password_file is not None:
        # Compose keeps the password out of the environment it shows; Aegra reads it from here.
        os.environ["POSTGRES_PASSWORD"] = settings.database_password_file.read_text().strip()
    config = Path(tempfile.gettempdir()) / "aegra.json"
    config.write_text(json.dumps(aegra_config(origin, Path(__file__).with_name("server.py"))))
    # `aegra serve` ignores an AEGRA_CONFIG outside the working directory; --config sets it.
    command = ["aegra", "serve", "--host", "0.0.0.0", "--port", "2026", "--config", str(config)]  # noqa: S104 -- the container binds all interfaces; Compose publishes loopback only
    os.execvp(command[0], command)  # noqa: S606 -- fixed command, no shell


if __name__ == "__main__":
    main()
