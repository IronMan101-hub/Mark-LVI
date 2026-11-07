"""One HTTP client for every plugin: timeouts, UA, size cap, no secrets in logs."""
from __future__ import annotations

import requests

from . import VERSION, logs

log = logs.get("http")
UA = f"GALAHAD/{VERSION} (+personal assistant)"
MAX_BYTES = 5_000_000


class Http:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA

    def request(self, method: str, url: str, **kw) -> requests.Response:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) URLs are allowed")
        kw.setdefault("timeout", self.timeout)
        r = self.session.request(method, url, **kw)
        if len(r.content) > MAX_BYTES:
            raise ValueError("response too large")
        return r

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)
