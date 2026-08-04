from urllib.parse import urlparse

import httpx

from .config import get_settings


def _configured_public_url() -> str | None:
    value = get_settings().web_base_url.strip().rstrip("/")
    if not value:
        return None
    hostname = (urlparse(value).hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return value
    return None


def discover_ngrok_url() -> str | None:
    """Return the active HTTPS ngrok tunnel for the local web application."""
    try:
        response = httpx.get(
            get_settings().ngrok_api_url,
            timeout=0.35,
            trust_env=False,
        )
        response.raise_for_status()
        tunnels = response.json().get("tunnels", [])
    except (httpx.HTTPError, ValueError, TypeError):
        return None

    https_tunnels = [
        tunnel for tunnel in tunnels
        if str(tunnel.get("public_url", "")).startswith("https://")
    ]
    for tunnel in https_tunnels:
        address = str(tunnel.get("config", {}).get("addr", ""))
        if address.endswith(":8000"):
            return str(tunnel["public_url"]).rstrip("/")
    if https_tunnels:
        return str(https_tunnels[0]["public_url"]).rstrip("/")
    return None


def public_base_url(request_base_url: str) -> tuple[str, str]:
    configured = _configured_public_url()
    if configured:
        return configured, "configured"
    ngrok = discover_ngrok_url()
    if ngrok:
        return ngrok, "ngrok"
    return request_base_url.rstrip("/"), "local"
