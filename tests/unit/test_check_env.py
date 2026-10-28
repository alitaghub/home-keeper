"""scripts/check_env.py keeps .env.example and Compose in step with what the services read."""

from check_env import problems

COMPOSE = """
services:
  api:
    ports:
      - "127.0.0.1:${HOME_KEEPER_PORT:-8000}:8000"
    environment:
      HOME_KEEPER_RETENTION_DAYS: ${HOME_KEEPER_RETENTION_DAYS:-30}
  agent:
    environment:
      AZURE_OPENAI_API_KEY:
      AUTH_TYPE: noop
  postgres:
    environment:
      POSTGRES_PASSWORD: aegra
"""
EXAMPLE = """# Copy to .env.
AZURE_OPENAI_API_KEY=
# HOME_KEEPER_PORT=8000
# HOME_KEEPER_RETENTION_DAYS=30
"""


def test_matching_files_have_no_problems() -> None:
    assert problems([COMPOSE], EXAMPLE) == []


def test_a_variable_compose_reads_must_be_in_the_example() -> None:
    example = EXAMPLE.replace("# HOME_KEEPER_PORT=8000\n", "")

    assert problems([COMPOSE], example) == [
        "HOME_KEEPER_PORT is read by Compose but missing from .env.example"
    ]


def test_a_passed_through_variable_must_be_in_the_example() -> None:
    example = EXAMPLE.replace("AZURE_OPENAI_API_KEY=\n", "")

    assert problems([COMPOSE], example) == [
        "AZURE_OPENAI_API_KEY is read by Compose but missing from .env.example"
    ]


def test_an_example_variable_compose_never_reads_is_reported() -> None:
    assert problems([COMPOSE], EXAMPLE + "# HOME_KEEPER_DB=/data/home.sqlite\n") == [
        "HOME_KEEPER_DB is in .env.example but Compose never reads it"
    ]


def test_a_misspelled_service_variable_is_reported() -> None:
    compose = COMPOSE.replace("HOME_KEEPER_RETENTION_DAYS: $", "HOME_KEEPER_RETENTON_DAYS: $")

    assert problems([compose], EXAMPLE) == [
        "HOME_KEEPER_RETENTON_DAYS is set on api but its settings never read it"
    ]


def test_override_tags_parse_as_their_plain_value() -> None:
    compose = (
        COMPOSE
        + """
  collector:
    environment:
      HOME_KEEPER_HA_URL: ${HOME_KEEPER_HA_URL:?Set the origin}
    volumes: !override
      - type: bind
        source: ${HOME_KEEPER_HA_TOKEN_FILE:?Set the path}
"""
    )

    assert problems([compose], EXAMPLE) == [
        "HOME_KEEPER_HA_TOKEN_FILE is read by Compose but missing from .env.example",
        "HOME_KEEPER_HA_URL is read by Compose but missing from .env.example",
    ]
