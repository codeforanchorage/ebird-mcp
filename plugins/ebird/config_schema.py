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
        default=30,
        ge=1,
        le=120,
        description="HTTP request timeout in seconds",
    )
    default_max_results: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Default maxResults when not provided by the caller",
    )
    default_back: int = Field(
        default=14,
        ge=1,
        le=30,
        description="Default 'back' (days) when not provided by the caller. eBird hard-caps at 30.",
    )

    model_config = ConfigDict(extra="forbid")
