"""Headroom proxy health and savings helpers with safe direct-call fallback."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable


class HeadroomProxy:
    def __init__(self, config: dict[str, Any] | None = None, *, opener: Callable = urllib.request.urlopen,
                 command_runner: Callable = subprocess.run):
        self.config = config or {}
        options = self.config.get("headroom", self.config)
        self.base_url = options.get("base_url", "http://localhost:8787").rstrip("/")
        self.enabled = options.get("mode", "proxy") == "proxy"
        self.output_shaper = bool(options.get("output_shaper", False))
        self.opener, self.command_runner = opener, command_runner
        self.tokens_saved = 0
        self.warning: str | None = None

    def check_health(self) -> bool:
        if not self.enabled:
            return False
        try:
            with self.opener(f"{self.base_url}/health", timeout=2) as response:
                healthy = getattr(response, "status", 200) < 400
        except (OSError, urllib.error.URLError, TimeoutError):
            healthy = False
        if not healthy:
            self.enabled = False
            self.warning = f"Headroom proxy unreachable at {self.base_url}; using direct providers"
        return healthy

    @property
    def provider_base_url(self) -> str | None:
        return self.base_url if self.enabled else None

    def capture_savings(self, value: int | None = None) -> int:
        """Capture the proxy's reported savings; tolerate absent CLI/proxy data."""
        if value is None:
            try:
                result = self.command_runner(["headroom", "savings", "--json"], capture_output=True,
                                            text=True, check=False, timeout=5)
                if result.returncode:
                    return self.tokens_saved
                data = json.loads(result.stdout or "{}")
                value = data.get("tokens_saved", data.get("saved_tokens", data.get("tokensSaved", 0)))
            except (OSError, ValueError, TimeoutError, AttributeError):
                return self.tokens_saved
        self.tokens_saved = max(0, int(value))
        return self.tokens_saved

    def compress(self, text: str, *, never_compress: list[str] | None = None,
                 protected: bool = False) -> str:
        """Preserve protected prompt/draft text verbatim; proxy compression is external."""
        if protected or never_compress:
            return text
        return text


def compress(text: str, *, never_compress: list[str] | None = None,
             protected: bool = False) -> str:
    """Compatibility helper; compression is performed by the configured proxy."""
    if protected or never_compress:
        return text
    return text
