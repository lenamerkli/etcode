"""Client-facing wire models."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextContent(StrictModel):
    type: Literal["text"] = "text"
    text: str


class MediaContent(StrictModel):
    type: Literal["image"] = "image"
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


Content = Annotated[Union[TextContent, MediaContent], Field(discriminator="type")]


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class SystemMessage(StrictModel):
    role: Literal["system"] = "system"
    content: list[Content]


class UserMessage(StrictModel):
    role: Literal["user"] = "user"
    content: list[Content] = Field(min_length=1)


class ToolMessage(StrictModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str = Field(min_length=1)
    content: list[Content] = Field(min_length=1)


class AssistantMessage(StrictModel):
    role: Literal["assistant"] = "assistant"
    text: TextContent = Field(default_factory=TextContent)
    reasoning: TextContent = Field(default_factory=TextContent)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    status: Literal["complete", "cancelled", "error"] = "complete"
    error: dict[str, Any] | None = None


Message = Annotated[Union[SystemMessage, UserMessage, ToolMessage, AssistantMessage], Field(discriminator="role")]
ClientMessage = Annotated[Union[UserMessage, ToolMessage], Field(discriminator="role")]


class InferenceOptions(StrictModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None


class SessionCreate(InferenceOptions):
    model: str
    websearch: bool = False


class ModelChange(StrictModel):
    model: str


class SubmitBatch(StrictModel):
    messages: list[ClientMessage] = Field(min_length=1, max_length=100)
    # Overrides apply to generation triggered by this submission, including a
    # generation resumed after the tool-result barrier clears.
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None

    @model_validator(mode="after")
    def single_kind(self):
        if len({m.role for m in self.messages}) > 1:
            raise ValueError("do not mix user messages and tool results in one batch")
        return self

    def overrides(self) -> dict[str, Any]:
        return self.model_dump(exclude={"messages"}, exclude_none=True)
