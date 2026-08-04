from app import public_url


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "tunnels": [{
                "public_url": "https://game-example.ngrok-free.app",
                "config": {"addr": "http://localhost:8000"},
            }]
        }


def test_discovers_ngrok_https_tunnel(monkeypatch):
    monkeypatch.setattr(public_url.httpx, "get", lambda *args, **kwargs: FakeResponse())
    assert public_url.discover_ngrok_url() == "https://game-example.ngrok-free.app"


def test_falls_back_to_request_url_without_ngrok(monkeypatch):
    monkeypatch.setattr(public_url, "_configured_public_url", lambda: None)
    monkeypatch.setattr(public_url, "discover_ngrok_url", lambda: None)
    assert public_url.public_base_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000",
        "local",
    )
