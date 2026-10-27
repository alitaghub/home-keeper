"""Reads the service credential file. Kept apart from the API so collectors import no API code."""

from pathlib import Path


def load_token(path: Path) -> str:
    return path.read_text().strip()
