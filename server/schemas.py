from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Register(DTO):
    username: str = Field(pattern=r'^[A-Za-z0-9_]{3,32}$')
    display_name: str = Field(min_length=1,max_length=40)
    password: str = Field(min_length=12,max_length=128)


class Login(DTO):
    username: str = Field(pattern=r'^[A-Za-z0-9_]{3,32}$')
    password: str = Field(min_length=12,max_length=128)


class Empty(DTO):
    pass


class NewConversation(DTO):
    title: str = Field(default='新对话',min_length=1,max_length=80)


class Rename(NewConversation):
    revision: int = Field(ge=1)


class Question(DTO):
    content: str = Field(min_length=1,max_length=6000)
    mode: Literal['general','document']
    file_refs: list[dict] = Field(max_length=5)
    intent: Literal['question','summary']

    @field_validator('content')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('EMPTY_QUESTION')
        return value


class Retry(DTO):
    failed_request_id: str
    scope_policy: Literal['current']
    confirmed_file_refs: list[dict] = Field(default_factory=list,max_length=5)

    @field_validator('failed_request_id')
    @classmethod
    def valid_id(cls, value):
        UUID(value)
        return value
