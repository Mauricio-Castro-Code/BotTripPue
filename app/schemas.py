from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class WaTextBody(BaseModel):
    body: str


class WaButtonReply(BaseModel):
    id: str | None = None
    title: str | None = None


class WaInteractivePayload(BaseModel):
    type: str | None = None
    button_reply: WaButtonReply | None = None

    model_config = {"extra": "allow"}


class WaMessage(BaseModel):
    from_: str | None = Field(default=None, alias="from")
    id: str | None = None
    timestamp: str | None = None
    text: WaTextBody | None = None
    type: str | None = None
    interactive: WaInteractivePayload | None = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class WaContact(BaseModel):
    profile: dict[str, Any] | None = None
    wa_id: str | None = None


class WaMetadata(BaseModel):
    display_phone_number: str | None = None
    phone_number_id: str | None = None


class WaValue(BaseModel):
    messaging_product: str | None = None
    metadata: WaMetadata | None = None
    contacts: list[WaContact] | None = None
    messages: list[WaMessage] | None = None

    model_config = {"extra": "allow"}


class WaChange(BaseModel):
    value: WaValue | None = None
    field: str | None = None


class WaEntry(BaseModel):
    id: str | None = None
    changes: list[WaChange] | None = None


class WhatsAppWebhookPayload(BaseModel):
    object: str | None = None
    entry: list[WaEntry] | None = None
