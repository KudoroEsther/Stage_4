from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ProfileRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("name must be a string")
        if not value.strip():
            raise ValueError("name must not be empty")
        return value.strip().lower()


class AuthExchangeRequest(BaseModel):
    code: str | None = None
    state: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class OAuthStartRequest(BaseModel):
    client: Literal["cli", "web", "browser_direct"] = "web"
    redirect_uri: str | None = None
    state: str | None = None
    code_challenge: str | None = None
    return_to: str | None = None


class UpdateRoleRequest(BaseModel):
    role: Literal["admin", "analyst"]
    is_active: bool = True


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=10, ge=1, le=50)
