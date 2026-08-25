"""Pydantic configuration schema for the eBird plugin."""

from pydantic import BaseModel, ConfigDict, Field


class EBirdPluginConfig(BaseModel):
    """Configuration schema for the eBird plugin.

    Loaded from the `plugins.ebird` section of config.yaml.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    api_key: str = Field(
        ...,
        description="eBird API key. Request one at https://ebird.org/api/keygen",
        min_length=1,
    )
    base_url: str = Field(
        default="https://api.ebird.org/v2",
        description="Base URL of the eBird API",
    )
    timeout: int = Field(
        default=20,
        ge=1,
        le=28,
        description=(
            "HTTP request timeout in seconds for calls to api.ebird.org. MUST "
            "stay below the Lambda timeout (28s, itself under API Gateway's "
            "hard 29s ceiling) so a hung eBird call produces a readable "
            "'upstream timed out' tool error instead of the Lambda being "
            "killed mid-flight and the caller getting an opaque 502. The "
            "default of 20 leaves ~8s to format and return."
        ),
    )
    default_max_results: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Default maxResults when not provided by the caller. Capped at "
            "1000 to match the per-call ceiling in the tool schemas — larger "
            "responses render to multiple megabytes of text."
        ),
    )
    default_back: int = Field(
        default=14,
        ge=1,
        le=30,
        description="Default 'back' (days) when not provided by the caller. eBird hard-caps at 30.",
    )
    include_observer_name: bool = Field(
        default=False,
        description=(
            "If true, include the observer's display name in formatted observations. "
            "eBird already shows opted-in display names publicly; we default to false "
            "because the LLM can aggregate per-observer queries more easily than a "
            "browser can. Set to true to match eBird.org's UI."
        ),
    )

    model_config = ConfigDict(extra="forbid")
