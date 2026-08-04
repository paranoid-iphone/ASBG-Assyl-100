from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_health_and_admin_setup():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        anonymous = client.get("/admin")
        assert anonymous.status_code == 401

        authenticated = client.get("/admin", auth=("admin", "change-me"))
        assert authenticated.status_code == 200
        assert "Настройка мероприятия" in authenticated.text
