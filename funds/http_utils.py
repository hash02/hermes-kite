"""Small HTTP helpers shared by Hermes Kite workers."""

from __future__ import annotations

import urllib.parse
import urllib.request


def safe_urlopen(req_or_url, *, timeout: int):
    """Open only HTTP(S) URLs.

    Bandit correctly warns when urllib can receive arbitrary schemes. The workers
    use hardcoded public HTTP APIs, but this helper keeps that contract explicit.
    """
    url = req_or_url.full_url if isinstance(req_or_url, urllib.request.Request) else str(req_or_url)
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"blocked URL scheme: {scheme or 'missing'}")
    return urllib.request.urlopen(req_or_url, timeout=timeout)  # nosec B310
