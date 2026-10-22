"""Normalized mail evidence shared by mailbox providers."""
from __future__ import annotations

from pydantic import Field
from magi_plugin_sdk.runtime import RuntimeModel
from magi_plugin_sdk.source_query import SourceEvidence


class MailMessage(RuntimeModel):
    object_id: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=256)
    thread_id: str = Field(min_length=1, max_length=1024)
    subject: str = Field(default="", max_length=1024)
    sender: str = Field(default="", max_length=2048)
    recipients: list[str] = Field(default_factory=list, max_length=200)
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    body: str = Field(default="", max_length=12000)
    labels: list[str] = Field(default_factory=list, max_length=100)
    attachment_names: list[str] = Field(default_factory=list, max_length=100)
    content_complete: bool = True
    url: str | None = None

    def evidence(self, *, include_thread: bool = False) -> SourceEvidence:
        """Thread completeness is asserted only by a provider thread read."""
        fields = ["labels", "attachments"]
        if self.content_complete:
            fields.append("content")
        if include_thread:
            fields.append("thread")
        text = f"Subject: {self.subject}\nFrom: {self.sender}\nTo: {', '.join(self.recipients)}\n\n{self.body}"
        return SourceEvidence(object_id=self.object_id, version=self.version,
            thread_id=self.thread_id, title=self.subject, text=text[:16384], occurred_at=self.occurred_at,
            fields=fields if len(text) <= 16384 else [f for f in fields if f != "content"],
            metadata={"sender": self.sender, "recipients": self.recipients, "labels": self.labels,
                "attachment_names": self.attachment_names, "content_complete": self.content_complete and len(text) <= 16384},
            url=self.url)
