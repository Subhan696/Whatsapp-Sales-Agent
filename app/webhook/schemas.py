"""Pydantic models for Meta WhatsApp Cloud API webhook payloads.

Models use extra="allow" so unknown fields are silently ignored — Meta
frequently adds new fields and the schema must be forward-compatible.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Extra(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Leaf objects
# ---------------------------------------------------------------------------


class MessageText(_Extra):
    body: str = ""


class MessageImage(_Extra):
    id: str  # media ID
    mime_type: str = ""
    caption: str | None = None
    sha256: str | None = None


class MessageAudio(_Extra):
    id: str
    mime_type: str = ""


class MessageDocument(_Extra):
    id: str
    mime_type: str = ""
    filename: str | None = None
    caption: str | None = None


class MessageLocation(_Extra):
    latitude: float = 0.0
    longitude: float = 0.0
    name: str | None = None
    address: str | None = None


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


class Message(_Extra):
    """Single WhatsApp message inside a webhook entry.

    The ``from`` field is renamed to ``from_`` to avoid the Python keyword.
    """

    id: str
    from_: str = Field(alias="from")
    timestamp: str
    type: str  # text | image | audio | document | location | reaction | …

    text: MessageText | None = None
    image: MessageImage | None = None
    audio: MessageAudio | None = None
    document: MessageDocument | None = None
    location: MessageLocation | None = None

    def body_or_summary(self) -> str:
        """Return a human-readable summary suitable for message_log storage."""
        if self.type == "text" and self.text:
            return self.text.body
        if self.type == "image":
            caption = self.image.caption if self.image else None
            return f"[image] {caption}" if caption else "[image]"
        if self.type == "audio":
            return "[audio message]"
        if self.type == "document":
            fname = self.document.filename if self.document else None
            return f"[document: {fname}]" if fname else "[document]"
        if self.type == "location" and self.location:
            return f"[location: {self.location.latitude}, {self.location.longitude}]"
        return f"[{self.type}]"


# ---------------------------------------------------------------------------
# Surrounding envelope
# ---------------------------------------------------------------------------


class ContactProfile(_Extra):
    name: str = ""


class Contact(_Extra):
    wa_id: str
    profile: ContactProfile = Field(default_factory=ContactProfile)


class WebhookMetadata(_Extra):
    display_phone_number: str = ""
    phone_number_id: str = ""


class ChangeValue(_Extra):
    messaging_product: str = ""
    metadata: WebhookMetadata | None = None
    contacts: list[Contact] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    # statuses[] is intentionally not modelled — we ignore delivery receipts here


class Change(_Extra):
    value: ChangeValue
    field: str = ""


class Entry(_Extra):
    id: str = ""
    changes: list[Change] = Field(default_factory=list)


class WebhookPayload(_Extra):
    object: str = ""
    entry: list[Entry] = Field(default_factory=list)
