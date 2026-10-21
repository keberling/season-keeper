"""Bounded retries for Sonarr and Radarr calls.

Transport failures and brief gateway errors are tried again with a short
backoff. A 400 is the caller's problem and is not retried.
"""

from __future__ import annotations

import time

import httpx

RETRY_STATUS = {502, 503, 504}
MAX_ATTEMPTS = 3


def request_with_retry(client: httpx.Client, method: str, url: str, **kwargs):
    delay = 0.05
    last_error: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code in RETRY_STATUS and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
            continue
        return response
    if last_error is not None:
        raise last_error
    return response
