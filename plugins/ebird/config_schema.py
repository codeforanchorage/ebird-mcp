"""Pydantic configuration schema for the eBird plugin."""

from typing import Optional

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
        default=25,
        ge=1,
        le=120,
        description=(
            "HTTP request timeout in seconds for calls to api.ebird.org. Keep at "
            "least 5 seconds below the Lambda timeout so the function has time to "
            "format and return a clean error before AWS pulls the plug."
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
