"""Print the public API's OpenAPI schema without starting a server."""

import json
import tempfile
from pathlib import Path

from home_observer.api import create_app


def main():
    # The schema does not depend on stored data, so a throwaway database is enough.
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / "openapi.sqlite", service_token="openapi")
        print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
