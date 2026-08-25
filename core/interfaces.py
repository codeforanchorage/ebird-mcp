"""Core interfaces and data models for eBird MCP plugins.

This module defines the abstract base classes and data models that all plugins
must implement. The core framework is universal and is not modified by plugins.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PluginType(str, Enum):
    """Types of plugins supported by the framework."""

    OPEN_DATA = "open_data"
    CUSTOM_API = "custom_api"
    DATABASE = "database"
    ANALYTICS = "analytics"


class ToolInputError(ValueError):
    """Raised when a tool rejects the caller's arguments.

    A marker, not a behaviour change: it exists so the plugin's handler can
    tell "the caller asked for something invalid" from "this server broke",
    and log the first at WARNING with no traceback. A traceback is a claim
    that the server failed; spending one on "you forgot speciesCode" is
    what makes real faults hard to find in CloudWatch.

    Deliberately NOT inferred from ValueError alone. That heuristic is
    wrong here in two ways:
      * json.JSONDecodeError subclasses ValueError, so a malformed
        upstream payload would be misfiled as a caller mistake and lose
        its stack trace.
      * ``_parse_hotspot_text`` coerces eBird's CSV fallback with bare
        float(), which raises ValueError on a malformed upstream row — a
        genuine upstream fault whose traceback we want.
    Both stay plain ValueError and keep their tracebacks.

    Subclasses ValueError so every existing ``except ValueError`` keeps
    working, which makes converting a raise site mechanical and safe.
    """


class InvalidToolParamsError(ValueError):
    """Raised when a tools/call request is itself malformed.

    Covers the cases the MCP tools spec calls "requests that fail to
    satisfy the CallToolRequest schema" — a missing tool name, or
    ``arguments`` that is not an object. Mapped to JSON-RPC -32602
    ("Invalid params"): the request never described a valid call, so it is
    neither a server fault (-32603) nor a tool execution error reported
    through ``isError``.

    Subclasses ValueError for the same reason UnknownToolError does.
    """


class UnknownToolError(ValueError):
    """Raised when tools/call names a tool this server does not expose.

    Subclasses ValueError deliberately: it is an argument problem, and
    existing ``except ValueError`` handlers around execute_tool keep
    working unchanged.

    Mapped to JSON-RPC -32602 with the message shape the MCP tools spec
    uses ("Unknown tool: <name>") rather than the generic -32603
    "Internal error": naming a missing tool is a caller mistake, not a
    server fault. The available-tool list travels in ``data`` so a model
    can self-correct instead of just being told no — and unlike a genuine
    fault there is nothing sensitive to scrub, since the same list is
    already public via tools/list.
    """

    def __init__(self, tool_name: str, available: str = "") -> None:
        self.tool_name = tool_name
        self.available = available
        super().__init__(f"Unknown tool: {tool_name}")


class ToolDefinition(BaseModel):
    """Definition of an MCP tool provided by a plugin."""

    name: str = Field(..., description="Tool name (without plugin prefix)")
    description: str = Field(..., description="Human-readable tool description")
    input_schema: Dict[str, Any] = Field(
        ..., description="JSON Schema for tool input parameters"
    )


class ToolResult(BaseModel):
    """Result of executing a tool."""

    content: List[Dict[str, Any]] = Field(
        default_factory=list, description="Tool output content"
    )
    success: bool = Field(..., description="Whether the tool execution succeeded")
    error_message: Optional[str] = Field(
        None, description="Error message if execution failed"
    )


class MCPPlugin(ABC):
    """Abstract base class for all plugins.

    All plugins must inherit from this class and implement all required methods.
    Plugins are discovered automatically and loaded by the Plugin Manager.
    """

    plugin_name: str = ""
    plugin_type: PluginType = PluginType.CUSTOM_API
    plugin_version: str = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialize plugin with configuration."""
        self.config = config
        self._initialized = False

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize the plugin and verify it can connect to its data source."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up plugin resources."""
        pass

    def get_instructions(self) -> Optional[str]:
        """Server-level usage guidance surfaced in the MCP initialize response.

        Optional. Return a short plain-text guide (workflow, data caveats)
        for LLM clients; return None to omit the `instructions` field.
        """
        return None

    @abstractmethod
    def get_tools(self) -> List[ToolDefinition]:
        """Get list of tools provided by this plugin.

        Tool names should NOT include the plugin prefix. The Plugin Manager
        will add the prefix automatically using double underscores
        (e.g., ``ebird__get_recent_observations``).
        """
        pass

    @abstractmethod
    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        """Execute a tool by name."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the plugin is healthy and can reach its data source."""
        pass

    @property
    def is_initialized(self) -> bool:
        return self._initialized
