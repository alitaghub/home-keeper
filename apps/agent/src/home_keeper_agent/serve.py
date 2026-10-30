"""Container entry point: write Aegra's config, pass on its database password, run `aegra serve`.

The browser reaches Aegra on the dashboard's own origin, so CORS allows no origin.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from home_keeper_agent.settings import AgentSettings
from home_keeper_settings import SettingsError, load


def aegra_config(graph_file: Path) -> dict[str, object]:
    # Aegra resolves graph paths relative to the config file, so the path is absolute.
    return {
        "graphs": {"agent": f"{graph_file.resolve()}:agent"},
        # Without a cors section Aegra allows every origin; an empty list allows none.
        "http": {"cors": {"allow_origins": []}},
    }


def main() -> None:
    try:
        settings = load(AgentSettings)
    except SettingsError as error:
        sys.exit(f"Agent configuration invalid: {error}")
    if settings.database_password_file is not None:
        # Compose keeps the password out of the environment it shows; Aegra reads it from here.
        os.environ["POSTGRES_PASSWORD"] = settings.database_password_file.read_text().strip()
    config = Path(tempfile.gettempdir()) / "aegra.json"
    config.write_text(json.dumps(aegra_config(Path(__file__).with_name("server.py"))))
    # `aegra serve` ignores an AEGRA_CONFIG outside the working directory; --config sets it.
    command = ["aegra", "serve", "--host", "0.0.0.0", "--port", "2026", "--config", str(config)]  # noqa: S104 -- the container binds all interfaces; Compose publishes loopback only
    os.execvp(command[0], command)  # noqa: S606 -- fixed command, no shell


if __name__ == "__main__":
    main()
