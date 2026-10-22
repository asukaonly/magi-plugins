"""Provider-neutral mail types; all mutable state belongs to a connection."""
from .models import MailMessage
from .source import MailProvider, MailSource

__all__ = ["MailMessage", "MailProvider", "MailSource"]
