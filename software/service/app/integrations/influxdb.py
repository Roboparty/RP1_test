"""Influx OSS 2 HTTP write adapter. No database or API domain logic."""
from __future__ import annotations

from typing import Any
import httpx
from app.core.config import Settings

class InfluxWriteError(RuntimeError):
    """Safe error text: excludes token, URL query and arbitrary remote body."""


def write_lines(settings: Settings, lines: str, *, client: Any = None) -> None:
    token = settings.influx_token
    if token is None or not token.get_secret_value():
        raise InfluxWriteError('INFLUX_TOKEN is required')
    def write(http: Any) -> None:
        try:
            response = http.post(
                settings.influx_url.rstrip('/') + '/api/v2/write',
                params={'org': settings.influx_org, 'bucket': settings.influx_bucket, 'precision': 'ns'},
                headers={'Authorization': 'Token ' + token.get_secret_value(), 'Content-Type': 'text/plain; charset=utf-8'},
                content=lines.encode('utf-8'),
            )
        except httpx.HTTPError as exc:
            raise InfluxWriteError(f'Influx transport failure: {type(exc).__name__}') from None
        if response.status_code != 204:
            raise InfluxWriteError(f'Influx HTTP {response.status_code}; export retained for retry')
    if client is not None:
        write(client)
    else:
        with httpx.Client(timeout=settings.influx_timeout_seconds) as http:
            write(http)
