"""Print the public API's OpenAPI schema without starting a server."""

import json

from home_keeper.api import create_app
from home_keeper.reader import HttpReader


def main():
    # Never started, so neither the database nor the reader is contacted.
    app = create_app(
        "postgresql://openapi.invalid/openapi",
        service_token="openapi",  # noqa: S106 -- throwaway app, never served
        reader=HttpReader("http://reader.invalid", "openapi"),
    )
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
