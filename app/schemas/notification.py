from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class NotificationRead(BaseModel):
    id: int
    type: str
    title: str
    message: Optional[str] = None
    link: Optional[str] = None
    is_read: bool
    created_at: datetime
    read_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class NotificationList(BaseModel):
    items: list[NotificationRead]
    unread_count: int


class UnreadCount(BaseModel):
    unread_count: int


class ReadAllResult(BaseModel):
    updated: int
    unread_count: int
