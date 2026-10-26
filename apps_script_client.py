"""HTTP client for the Google Apps Script receiver."""

from __future__ import annotations

import uuid
from typing import Any, Callable

import requests


class AppsScriptError(RuntimeError):
    pass


class AppsScriptClient:
    def __init__(
        self,
        url: str,
        secret: str,
        timeout_seconds: float = 20,
        post: Callable[..., Any] | None = None,
    ):
        self.url = url
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self._post = post or requests.post

    def _request(self, action: str, records: list[dict[str, Any]] | None = None):
        body: dict[str, Any] = {
            "version": 1,
            "action": action,
            "secret": self.secret,
            "request_id": str(uuid.uuid4()),
        }
        if records is not None:
            body["records"] = records
        try:
            response = self._post(
                self.url,
                json=body,
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AppsScriptError(f"Apps Script request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("error", "invalid_response") if isinstance(payload, dict) else "invalid_response"
            raise AppsScriptError(f"Apps Script rejected request: {reason}")
        return payload

    def sync(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not records:
            return []
        payload = self._request("sync", records)
        results = payload.get("results")
        if not isinstance(results, list):
            raise AppsScriptError("Apps Script response has no results list")
        return results

    def repair(self) -> dict[str, Any]:
        return self._request("repair")

