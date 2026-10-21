"""Radarr v3 client. Movies are added and searched. Nothing here deletes a movie."""

from __future__ import annotations

import copy
import logging
from typing import Any

import httpx

from app.http_retry import request_with_retry

logger = logging.getLogger("trimarr.radarr")


class RadarrError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def movie_add_payload(looked_up: dict, *, profile_id: int, root: str, tag_id: int) -> dict:
    payload = copy.deepcopy(looked_up)
    for key in ("id", "statistics", "hasFile", "movieFile", "sizeOnDisk", "folderName"):
        payload.pop(key, None)
    payload["qualityProfileId"] = int(profile_id)
    payload["rootFolderPath"] = root
    payload["monitored"] = True
    payload["tags"] = [int(tag_id)]
    payload["addOptions"] = {"searchForMovie": True}
    return payload


def movie_monitor_payload(raw: dict, *, tag_id: int) -> dict:
    payload = copy.deepcopy(raw)
    for key in ("statistics", "hasFile", "movieFile", "sizeOnDisk", "folderName"):
        payload.pop(key, None)
    payload["monitored"] = True
    tags = [int(tag) for tag in (payload.get("tags") or [])]
    if int(tag_id) not in tags:
        tags.append(int(tag_id))
    payload["tags"] = tags
    return payload


class RadarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = request_with_retry(self._client, method, self._url(path), **kwargs)
        if response.status_code >= 400:
            body = response.text[:500]
            raise RadarrError(
                f"Radarr {method} {path} returned {response.status_code}: {body}",
                status=response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    def status_version(self) -> str:
        data = self._request("GET", "/api/v3/system/status")
        return str((data or {}).get("version") or "unknown")

    def root_folders(self) -> list[dict]:
        return self._request("GET", "/api/v3/rootfolder") or []

    def quality_profiles(self) -> list[dict]:
        return self._request("GET", "/api/v3/qualityprofile") or []

    def ensure_tag(self, label: str) -> int:
        tags = self._request("GET", "/api/v3/tag") or []
        for tag in tags:
            if str(tag.get("label", "")).lower() == label.lower():
                return int(tag["id"])
        created = self._request("POST", "/api/v3/tag", json={"label": label})
        logger.info("Created Radarr tag %s", label)
        return int(created["id"])

    def list_movies(self) -> list[dict]:
        return self._request("GET", "/api/v3/movie") or []

    def lookup_movie(self, term: str) -> dict | None:
        rows = self._request("GET", "/api/v3/movie/lookup", params={"term": term}) or []
        if not rows:
            return None
        return rows[0]

    def add_movie(self, payload: dict) -> dict:
        return self._request("POST", "/api/v3/movie", json=payload) or {}

    def update_movie(self, payload: dict) -> None:
        movie_id = payload["id"]
        self._request("PUT", f"/api/v3/movie/{movie_id}", json=payload)

    def search_movie(self, movie_id: int) -> None:
        self._request(
            "POST",
            "/api/v3/command",
            json={"name": "MoviesSearch", "movieIds": [int(movie_id)]},
        )
