"""Fail when .env.example or the Compose files drift from what the services read.

Usage: mise run check:env

Two checks:
- .env.example lists exactly the variables Compose reads from your shell or .env:
  every ${NAME} it interpolates and every environment entry it passes through bare.
- Every variable Compose sets on one of our services is a field of that service's
  settings class. A misspelled name would otherwise be ignored and keep its default.
"""

import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic_settings import BaseSettings

from home_keeper.settings import ApiSettings, CollectorSettings, SetupSettings
from home_keeper_agent.settings import AgentSettings
from home_keeper_agent.telegram import Settings as TelegramSettings
from home_keeper_controller.settings import ControllerSettings

ROOT = Path(__file__).resolve().parent.parent
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
EXAMPLE_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# The settings class each of our services reads. The demo collector reads a subset of
# CollectorSettings. Home Assistant, Postgres and the chat UI are third-party images.
SERVICE_SETTINGS: Mapping[str, type[BaseSettings]] = {
    "api": ApiSettings,
    "collector": CollectorSettings,
    "ha-setup": SetupSettings,
    "controller": ControllerSettings,
    "agent": AgentSettings,
    "telegram": TelegramSettings,
}
# Variables a service passes to a library rather than to its settings class.
LIBRARY_VARIABLES: Mapping[str, frozenset[str]] = {
    # Aegra's own settings, and the key langchain reads for openai: models.
    "agent": frozenset({"DATABASE_URL", "REDIS_BROKER_ENABLED", "AUTH_TYPE", "OPENAI_API_KEY"}),
}


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's merge tags, such as !override, as the plain value they wrap."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    # Compose files hold only mappings, sequences and scalars.
    return loader.construct_scalar(node)  # pyright: ignore[reportArgumentType]


# types-pyyaml leaves the constructor parameter untyped.
ComposeLoader.add_multi_constructor("!", _untagged)  # pyright: ignore[reportUnknownMemberType]


class Service(BaseModel):
    environment: dict[str, str | int | bool | None] = {}


class ComposeFile(BaseModel):
    services: dict[str, Service] = {}


def load_compose(text: str) -> tuple[ComposeFile, JsonValue]:
    raw = TypeAdapter(JsonValue).validate_python(yaml.load(text, ComposeLoader))  # noqa: S506 -- SafeLoader subclass
    return ComposeFile.model_validate(raw), raw


def _strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def host_variables(compose: ComposeFile, raw: JsonValue) -> set[str]:
    """What Compose takes from your shell or .env: interpolations and bare entries."""
    names = {match for text in _strings(raw) for match in INTERPOLATION.findall(text)}
    for service in compose.services.values():
        names |= {name for name, value in service.environment.items() if value is None}
    return names


def example_variables(text: str) -> set[str]:
    """Names set or shown commented out, as in `# HA_PORT=8123`."""
    return set(EXAMPLE_LINE.findall(text))


def settings_variables(settings: type[BaseSettings]) -> set[str]:
    prefix = str(settings.model_config.get("env_prefix", ""))
    return {f"{prefix}{field}".upper() for field in settings.model_fields}


def unread_variables(compose: ComposeFile) -> list[str]:
    """Each variable Compose sets on one of our services that its settings never read."""
    problems: list[str] = []
    for name, service in compose.services.items():
        settings = SERVICE_SETTINGS.get(name)
        if settings is None:
            continue
        known = settings_variables(settings) | LIBRARY_VARIABLES.get(name, frozenset())
        problems += [
            f"{variable} is set on {name} but its settings never read it"
            for variable in service.environment
            if variable not in known
        ]
    return problems


def problems(compose_texts: list[str], example: str) -> list[str]:
    host: set[str] = set()
    found: list[str] = []
    for text in compose_texts:
        compose, raw = load_compose(text)
        host |= host_variables(compose, raw)
        found += unread_variables(compose)
    documented = example_variables(example)
    found += [
        f"{name} is read by Compose but missing from .env.example"
        for name in sorted(host - documented)
    ]
    found += [
        f"{name} is in .env.example but Compose never reads it"
        for name in sorted(documented - host)
    ]
    return found


def main() -> None:
    compose_texts = [path.read_text() for path in sorted(ROOT.glob("compose*.yaml"))]
    found = problems(compose_texts, (ROOT / ".env.example").read_text())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
