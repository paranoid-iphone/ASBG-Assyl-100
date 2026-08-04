from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, PendingRegistration, Player, Team


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def create_registered_player() -> tuple[int, int, int]:
    with SessionLocal() as db:
        event = Event(name="Assyl 100", registration_code="ACTIVE")
        db.add(event)
        db.flush()
        team = Team(event_id=event.id, name="Альфа", code="ALPHA")
        db.add(team)
        db.flush()
        player = Player(
            team_id=team.id,
            full_name="Анна Петрова",
            registration_code="AUTO-1",
            telegram_user_id="12345",
            telegram_username="anna",
        )
        db.add(player)
        db.commit()
        return event.id, team.id, player.id


def test_unassign_returns_registered_player_to_pool():
    event_id, team_id, player_id = create_registered_player()
    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/players/{player_id}/unassign",
            auth=("admin", "change-me"),
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        pending = db.scalar(select(PendingRegistration).where(PendingRegistration.telegram_user_id == "12345"))
        assert pending is not None
        assert pending.full_name == "Анна Петрова"
        assert db.get(Player, player_id) is None
        assert db.get(Team, team_id) is not None


def test_deleting_team_returns_telegram_players_to_pool():
    event_id, team_id, _ = create_registered_player()
    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/teams/{team_id}/delete",
            auth=("admin", "change-me"),
            data={"confirmation": "Альфа"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        assert db.get(Team, team_id) is None
        assert db.scalar(select(func.count(PendingRegistration.id))) == 1
