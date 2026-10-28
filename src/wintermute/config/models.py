"""JSON 配置的数据结构及跨字段校验，不创建模型客户端或运行任务。"""

import json
import os
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

type PositiveInt = Annotated[int, Field(gt=0)]
type NonnegativeInt = Annotated[int, Field(ge=0)]
type PositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type Nonblank = Annotated[str, StringConstraints(pattern=r"\S")]
type ToolName = Literal["read", "write", "edit", "exec"]


class ConfigError(ValueError):
    """配置无法加载、校验、保存或用于创建运行组件。"""


class _Settings(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ModelSettings(_Settings):
    """模型服务配置；显式密钥优先，否则在创建客户端时读取指定环境变量。"""

    api: Literal["responses", "chat_completions"] = "responses"
    base_url: Nonblank = "https://ark.cn-beijing.volces.com/api/v3"
    model: Nonblank = "doubao-seed-2-0-lite-260428"
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = (
        "ARK_API_KEY"
    )
    timeout: PositiveNumber = 60
    max_retries: NonnegativeInt = 0
    extra_body: dict[str, JsonValue] = Field(default_factory=dict, repr=False)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in value)
            )
            # 显式访问端口，拒绝超出范围或无法解析的端口字符串。
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query or fragment"
            )
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value

    @model_validator(mode="after")
    def validate_extra_body(self) -> Self:
        # JsonValue 可包含任意层级的浮点数，额外拒绝嵌套的 NaN/Infinity。
        json.dumps(self.extra_body, allow_nan=False)
        reserved = {"model", "tools", "stream"}
        if self.api == "responses":
            reserved.update(
                {
                    "input",
                    "instructions",
                    "store",
                    "include",
                    "previous_response_id",
                    "conversation",
                    "background",
                }
            )
        else:
            reserved.update({"messages", "n"})
        if reserved.intersection(self.extra_body):
            raise ValueError("extra_body must not override model protocol fields")
        return self

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> str:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        values = os.environ if environ is None else environ
        value = values.get(self.api_key_env, "") if self.api_key_env is not None else ""
        if not value.strip():
            raise ConfigError("Model API key is missing; set api_key or the configured api_key_env")
        return value


class ContextSettings(_Settings):
    """主/子 Agent 可以分别选择预算与摘要策略的基础参数。"""

    enabled: bool = False
    context_window: PositiveInt = 64000
    reserve_tokens: NonnegativeInt = 4096
    keep_recent_tokens: NonnegativeInt = 8192
    max_summary_tokens: PositiveInt = 2048
    summary_input_limit: PositiveInt = 16000
    summary_timeout: PositiveNumber | None = 60

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.reserve_tokens >= self.context_window:
            raise ValueError("reserve_tokens must be smaller than context_window")
        if self.keep_recent_tokens + self.max_summary_tokens >= (
            self.context_window - self.reserve_tokens
        ):
            raise ValueError("Recent context and summary budgets must leave room for fixed context")
        return self


class AgentSettings(_Settings):
    system_prompt: str = ""
    max_turns: PositiveInt = 20
    tool_timeout: PositiveNumber | None = 60
    model_timeout: PositiveNumber | None = 90


class ToolsSettings(_Settings):
    enabled: list[ToolName] = Field(default_factory=list)
    cwd: Nonblank = ".."
    shell: Nonblank | None = None
    command_timeout: PositiveNumber = 60
    max_output_bytes: PositiveInt = 50 * 1024
    max_output_lines: PositiveInt = 2000

    @field_validator("enabled")
    @classmethod
    def validate_enabled(cls, value: list[ToolName]) -> list[ToolName]:
        if len(value) != len(set(value)):
            raise ValueError("Tool names must be unique")
        return value


class SubagentSettings(_Settings):
    name: Nonblank
    description: str = ""
    model: ModelSettings | None = None
    agent: AgentSettings = Field(default_factory=AgentSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    timeout: PositiveNumber | None = 120


class BackgroundSettings(_Settings):
    max_concurrency: PositiveInt = 4
    max_tasks: PositiveInt = 128
    max_output_bytes: PositiveInt = 50 * 1024


class EventSettings(_Settings):
    database_path: Nonblank = "../data/events.sqlite3"
    max_event_bytes: PositiveInt = 256 * 1024
    max_concurrency: PositiveInt = 4
    poll_interval: PositiveNumber = 0.1
    lease_seconds: PositiveNumber = 30
    delivery_retry_interval: PositiveNumber = 2
    delivery_timeout: PositiveNumber = 30
    max_reference_bytes: Annotated[int, Field(ge=512)] = 16 * 1024
    max_notifications: Annotated[int, Field(ge=0, le=10000)] = 8
    max_states: Annotated[int, Field(ge=0, le=10000)] = 24

    @model_validator(mode="after")
    def validate_lease(self) -> Self:
        if self.poll_interval >= self.lease_seconds / 3:
            raise ValueError("poll_interval must be less than one third of lease_seconds")
        return self


class LoggingSettings(_Settings):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console: bool = True
    format: Literal["text", "json"] = "text"
    file: Nonblank | None = None
    max_bytes: PositiveInt = 5 * 1024 * 1024
    backup_count: NonnegativeInt = 3


type EnvironmentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
type InterfaceIdentifier = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"\S")
]


def _resolve_secret(
    secret: SecretStr | None, variable: str | None, environ: Mapping[str, str] | None
) -> str:
    values = os.environ if environ is None else environ
    value = secret.get_secret_value() if secret is not None else values.get(variable or "", "")
    if not value.strip():
        raise ConfigError(
            "Interface credential is missing; set the configured environment variable"
        )
    return value


class ConversationBindingSettings(_Settings):
    """将已认证的外部终端固定映射到应用用户和会话。"""

    terminal_id: InterfaceIdentifier
    user_id: InterfaceIdentifier
    conversation_id: InterfaceIdentifier

    @field_validator("terminal_id")
    @classmethod
    def validate_terminal(cls, value: str) -> str:
        if value.startswith("wintermute:internal:"):
            raise ValueError("Terminal ID uses a reserved internal prefix")
        return value


class HTTPTerminalSettings(ConversationBindingSettings):
    token: SecretStr | None = Field(default=None, repr=False)
    token_env: EnvironmentName | None = None

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        if self.token is None and self.token_env is None:
            raise ValueError("HTTP terminal requires token or token_env")
        if self.token is not None and not self.token.get_secret_value().strip():
            raise ValueError("HTTP token must not be blank")
        return self

    def resolve_token(self, environ: Mapping[str, str] | None = None) -> str:
        value = _resolve_secret(self.token, self.token_env, environ)
        if any(character.isspace() for character in value):
            raise ConfigError("HTTP token must not contain whitespace")
        return value


class HTTPSettings(_Settings):
    enabled: bool = False
    host: Nonblank = "127.0.0.1"
    port: Annotated[int, Field(ge=0, le=65535)] = 8080
    max_body_bytes: Annotated[int, Field(ge=256, le=16 * 1024 * 1024)] = 65536
    terminals: list[HTTPTerminalSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_terminals(self) -> Self:
        if self.enabled and not self.terminals:
            raise ValueError("Enabled HTTP interface requires terminal bindings")
        ids = [item.terminal_id for item in self.terminals]
        if len(ids) != len(set(ids)):
            raise ValueError("HTTP terminal IDs must be unique")
        return self


class TelegramBindingSettings(ConversationBindingSettings):
    chat_id: int
    allowed_user_ids: Annotated[list[PositiveInt], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_chat(self) -> Self:
        if self.chat_id == 0:
            raise ValueError("Telegram chat ID must not be zero")
        if len(self.allowed_user_ids) != len(set(self.allowed_user_ids)):
            raise ValueError("Telegram allowed user IDs must be unique")
        return self


class TelegramSettings(_Settings):
    enabled: bool = False
    bot_token: SecretStr | None = Field(default=None, repr=False)
    bot_token_env: EnvironmentName | None = "TELEGRAM_BOT_TOKEN"
    api_base: Nonblank = "https://api.telegram.org"
    poll_timeout: Annotated[int, Field(ge=0, le=50)] = 25
    request_timeout: PositiveNumber = 35
    retry_delay: PositiveNumber = 3
    bindings: list[TelegramBindingSettings] = Field(default_factory=list)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        return ModelSettings.validate_url(value).rstrip("/")

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.enabled and not self.bindings:
            raise ValueError("Enabled Telegram interface requires chat bindings")
        if self.request_timeout <= self.poll_timeout:
            raise ValueError("Telegram request_timeout must exceed poll_timeout")
        if self.bot_token is not None and not self.bot_token.get_secret_value().strip():
            raise ValueError("Telegram bot token must not be blank")
        chats = [item.chat_id for item in self.bindings]
        terminals = [item.terminal_id for item in self.bindings]
        if len(chats) != len(set(chats)) or len(terminals) != len(set(terminals)):
            raise ValueError("Telegram chat IDs and terminal IDs must be unique")
        return self

    def resolve_bot_token(self, environ: Mapping[str, str] | None = None) -> str:
        value = _resolve_secret(self.bot_token, self.bot_token_env, environ)
        if re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", value) is None:
            raise ConfigError("Telegram bot token has an invalid format")
        return value


class InterfacesSettings(_Settings):
    """交流层的传输适配器；默认不启动网络接口。"""

    http: HTTPSettings = Field(default_factory=HTTPSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    @model_validator(mode="after")
    def validate_scopes(self) -> Self:
        bindings = [*self.http.terminals, *self.telegram.bindings]
        ids = [item.terminal_id for item in bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("Terminal IDs must be unique across interfaces")
        conversations: dict[str, str] = {}
        for item in bindings:
            if conversations.setdefault(item.conversation_id, item.user_id) != item.user_id:
                raise ValueError("A conversation cannot belong to different users")
        return self


class AppConfig(_Settings):
    """完整应用配置；模型对象和工具执行器由运行层按此配置创建。"""

    schema_version: Literal[1] = 1
    model: ModelSettings = Field(default_factory=ModelSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    subagents: list[SubagentSettings] = Field(default_factory=list)
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    interfaces: InterfacesSettings = Field(default_factory=InterfacesSettings)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_subagents(self) -> Self:
        names = [subagent.name for subagent in self.subagents]
        if len(names) != len(set(names)):
            raise ValueError("Subagent names must be unique")
        return self

    def redacted_dict(self) -> dict[str, Any]:
        """返回展示用副本；已声明的密钥由 SecretStr 遮盖，不用于持久化。"""
        return _redact_mapping(self.model_dump(mode="json"))


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    hidden = {"api_key", "apikey", "authorization", "password", "secret", "token", "access_token"}

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: (
                    "**********"
                    if child is not None and key.lower().replace("-", "_") in hidden
                    else visit(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    return {key: visit(item) for key, item in value.items()}
