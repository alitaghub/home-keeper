"""Print the public API's OpenAPI schema without starting a server."""

import json
import tempfile
from pathlib import Path

from home_keeper.api import create_app
from home_keeper.reader import HttpReader


def main():
    # The schema does not depend on stored data, so a throwaway database is enough.
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(
            Path(directory) / "openapi.sqlite",
            service_token="openapi",  # noqa: S106 -- throwaway app, never served
            # Never called: building the schema reads nothing.
            reader=HttpReader("http://reader.invalid", "openapi"),
        )
        print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
