"""Pydantic models for Auth Broker."""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class AuthStatus(str, Enum):
    pending = "pending"
    url_ready = "url_ready"
    approved = "approved"
    completed = "completed"
    denied = "denied"


class AuthRequest(BaseModel):
    service: str
    consumer_id: str = "default"
    callback_url: str = ""
    scopes: List[str] = []


class ManualTokenInput(BaseModel):
    token: str
    expires_at: int = 0
    scope: str = ""


class TokenRecord(BaseModel):
    request_id: str
    service: str
    consumer_id: str
    status: str
    token: Optional[dict] = None
