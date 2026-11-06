def normalize_channel_scope(channel_type: str) -> str:
    """Normalize the external channel dimension used by delivery identity."""

    normalized = str(channel_type or "").strip().casefold()
    if not normalized:
        raise ValueError("Outreach channel scope is required")
    return normalized
