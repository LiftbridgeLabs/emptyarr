import json
from urllib.parse import urlparse

import requests

from src.worker_auth import signed_headers


class RepairWorkerClient:
    def __init__(self, worker, timeout: int = 15):
        self.name = worker.name
        self.url = worker.url.rstrip("/")
        self.secret = worker.token
        self.controller_url = worker.controller_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload=None,
                 timeout: int | None = None) -> dict:
        body = b"" if payload is None else json.dumps(
            payload, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        headers = signed_headers(self.secret, self.name, method, path, body)
        if body:
            headers["Content-Type"] = "application/json"
        response = requests.request(
            method, self.url + path, data=body or None, headers=headers,
            timeout=timeout or self.timeout,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"ok": False, "error": f"Worker returned HTTP {response.status_code}"}
        if not response.ok:
            raise RuntimeError(result.get("error", f"Worker returned HTTP {response.status_code}"))
        return result

    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def discover(self) -> dict:
        return self._request("GET", "/api/v1/databases")

    def audit(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/audit", payload, timeout=60)

    def run(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/run", payload, timeout=3700)

    def status(self) -> dict:
        return self._request("GET", "/api/v1/status")

    def cancel(self) -> dict:
        return self._request("POST", "/api/v1/cancel", {})

    def recover(self, instance_name: str) -> dict:
        return self._request("POST", "/api/v1/recover", {"instance": instance_name})


def validate_worker_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
