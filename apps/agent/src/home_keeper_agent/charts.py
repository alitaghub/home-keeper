"""The show_chart tool: the agent picks a chart kind and its data; the dashboard draws it.

The spec has no colors, sizes, fonts or other styling: the app owns the look. The web
panel's parser (apps/web/src/agent/chartSpec.ts) accepts and rejects the same specs; the
fixtures in apps/agent/tests/fixtures/chart_specs.json hold both to that.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, Self

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_config
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictFloat,
    model_validator,
)

from home_keeper_agent.keeper import TelegramSender
from home_keeper_agent.tools import run_context

# The dashboard has this many series colors.
MAX_SERIES = 5
MAX_CATEGORIES = 60
SHOWN = "Chart shown."

CHART_RULE = """\
- When a chart helps, call show_chart. For on-time per day use its usage_by_day kind, which \
draws Home Assistant's history itself; use bar or line only with numbers from tool results.
"""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _text_only(value: object) -> object:
    # Pydantic also reads a number as a Unix time; the spec takes ISO 8601 text only.
    if not isinstance(value, str):
        raise ValueError("must be an ISO 8601 timestamp with a UTC offset")  # noqa: TRY004 -- Pydantic reports only a ValueError as a validation error
    return value


Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_text_only),
    Field(description="ISO 8601 with a UTC offset."),
]


class Series(Strict):
    name: str = Field(min_length=1, max_length=60, description="Legend name of this series.")
    # Strict: a numeric string or a boolean is a mistake, not a number.
    values: list[StrictFloat] = Field(description="One value per category, in category order.")


class Categorical(Strict):
    title: str = Field(min_length=1, max_length=120)
    unit: str | None = Field(
        default=None, min_length=1, max_length=20, description="Unit of every value."
    )
    x_label: str | None = Field(default=None, min_length=1, max_length=60)
    y_label: str | None = Field(default=None, min_length=1, max_length=60)
    categories: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=MAX_CATEGORIES, description="X-axis labels, in order."
    )
    series: list[Series] = Field(min_length=1, max_length=MAX_SERIES)

    @model_validator(mode="after")
    def one_value_per_category(self) -> Self:
        for series in self.series:
            if len(series.values) != len(self.categories):
                raise ValueError(
                    f"series {series.name!r} has {len(series.values)} values "
                    f"for {len(self.categories)} categories"
                )
        return self


class BarChart(Categorical):
    """Compare values across categories, such as devices or days."""

    kind: Literal["bar"]


class LineChart(Categorical):
    """Show how values change over ordered categories, such as hours or days."""

    kind: Literal["line"]


class UsageByDay(Strict):
    """On and unknown time per day for monitored devices, drawn by the dashboard from Home
    Assistant's history. Pass no numbers: the dashboard reads them itself."""

    kind: Literal["usage_by_day"]
    entity_ids: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, max_length=MAX_SERIES
    )
    start: Timestamp
    end: Timestamp

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


ChartSpec = Annotated[BarChart | LineChart | UsageByDay, Field(discriminator="kind")]


class ShowChartArgs(Strict):
    spec: ChartSpec


@tool(args_schema=ShowChartArgs)
def show_chart(spec: ChartSpec) -> str:
    """Show the user a chart in the chat, next to your answer. Use usage_by_day for on and
    unknown time per day; it draws Home Assistant's history, so you pass no numbers. Use bar
    or line only with numbers from tool results. You choose the chart kind and data; the app
    chooses colors and layout."""
    del spec  # The dashboard draws the chart from the call's arguments.
    return SHOWN


def chart_tools() -> list[BaseTool]:
    return [show_chart]


class ChartsInBrowserOnly(AgentMiddleware):
    """Offers show_chart, and the rule on when to use it, only to runs that can show a chart.

    Telegram answers are text only, so a Telegram run's model never learns of the tool.
    """

    def _for_channel(self, request: ModelRequest) -> ModelRequest:
        if isinstance(run_context(get_config()).sender, TelegramSender):
            tools = [
                tool
                for tool in request.tools
                if (tool.get("name") if isinstance(tool, dict) else tool.name) != show_chart.name
            ]
            return request.override(tools=tools)
        blocks = request.system_message.content_blocks if request.system_message else []
        return request.override(
            system_message=SystemMessage(
                content_blocks=[*blocks, {"type": "text", "text": CHART_RULE}]
            )
        )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        return handler(self._for_channel(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        return await handler(self._for_channel(request))
