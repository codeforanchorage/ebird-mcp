"""Configuration validation functions.

Enforces the "one fork = one MCP server" rule.
"""

import logging
from typing import Any, Dict, List, Tuple

import yaml

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


def validate_plugin_count(config: Dict[str, Any]) -> Tuple[List[str], int]:
    """Validate that exactly ONE plugin is enabled in the configuration."""
    plugins_config = config.get("plugins", {})
    enabled_plugins = []

    for plugin_name, plugin_config in plugins_config.items():
        if isinstance(plugin_config, dict) and plugin_config.get("enabled", False):
            enabled_plugins.append(plugin_name)

    count = len(enabled_plugins)

    if count == 0:
        raise ConfigurationError(
            "Configuration Error: No Plugins Enabled\n\n"
            "You must enable exactly ONE plugin in config.yaml.\n"
            "Set 'enabled: true' for the ebird plugin (or a custom plugin)."
        )

    if count > 1:
        plugin_list = "\n".join(f"  - {name}" for name in enabled_plugins)
        raise ConfigurationError(
            f"Configuration Error: Multiple Plugins Enabled\n\n"
            f"You have {count} plugins enabled in config.yaml:\n{plugin_list}\n\n"
            f"This framework enforces: One Fork = One MCP Server.\n"
            f"Enable exactly one plugin per deployment."
        )

    return enabled_plugins, count


def validate_config_structure(config: Dict[str, Any]) -> None:
    """Validate basic configuration structure."""
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must be a YAML dictionary")

    if "plugins" not in config:
        raise ConfigurationError(
            "Configuration missing 'plugins' section. "
            "See config-example.yaml for required structure."
        )

    if not isinstance(config.get("plugins"), dict):
        raise ConfigurationError("'plugins' section must be a dictionary")


def load_and_validate_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load and validate configuration from YAML file."""
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Create config.yaml based on config-example.yaml."
        )
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {e}")

    if config is None:
        raise ConfigurationError(f"Configuration file {config_path} is empty")

    validate_config_structure(config)
    enabled_plugins, count = validate_plugin_count(config)

    logger.info(
        f"Configuration validated: {count} plugin(s) enabled: {', '.join(enabled_plugins)}"
    )

    return config


def get_logging_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Get logging configuration from config dictionary."""
    logging_config = config.get("logging", {})
    return {
        "level": logging_config.get("level", "INFO"),
        "format": logging_config.get("format", "json"),
    }


def get_enabled_plugin_config(config: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Get the configuration for the single enabled plugin."""
    enabled_plugins, _ = validate_plugin_count(config)

    if len(enabled_plugins) != 1:
        raise ConfigurationError("Internal error: Expected exactly one enabled plugin")

    plugin_name = enabled_plugins[0]
    plugin_config = config["plugins"][plugin_name]

    return plugin_name, plugin_config
