from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, GameProgram, PendingRegistration, Player, PlayerRole, Team


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
            event_id=event.id,
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
        player = db.get(Player, player_id)
        assert player is not None
        assert player.full_name == "Анна Петрова"
        assert player.team_id is None
        assert player.event_id == event_id
        assert db.get(Team, team_id) is not None

    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/players/{player_id}/assign",
            auth=("admin", "change-me"),
            data={"team_id": team_id, "role": PlayerRole.PLAYER.value},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        player = db.get(Player, player_id)
        assert player is not None
        assert player.team_id == team_id


def test_delete_player_requires_explicit_second_confirmation():
    event_id, _, player_id = create_registered_player()
    with SessionLocal() as db:
        player_name = db.get(Player, player_id).full_name

    with TestClient(app) as client:
        rejected = client.post(
            f"/admin/events/{event_id}/players/{player_id}/delete",
            auth=("admin", "change-me"),
            data={"confirmation": player_name},
            follow_redirects=False,
        )
        assert rejected.status_code == 400

        deleted = client.post(
            f"/admin/events/{event_id}/players/{player_id}/delete",
            auth=("admin", "change-me"),
            data={"confirmation": player_name, "confirmed": "true"},
            follow_redirects=False,
        )
        assert deleted.status_code == 303

    with SessionLocal() as db:
        assert db.get(Player, player_id) is None


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


def test_running_game_allows_role_change_but_protects_identity():
    event_id, team_id, player_id = create_registered_player()
    with SessionLocal() as db:
        original_name = db.get(Player, player_id).full_name
        db.add(GameProgram(event_id=event_id, title="Running", status="RUNNING"))
        db.commit()

    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/players/{player_id}",
            auth=("admin", "change-me"),
            data={
                "full_name": "Changed name", "role": PlayerRole.CAPTAIN.value,
                "team_id": team_id, "active": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 409

    with SessionLocal() as db:
        player = db.get(Player, player_id)
        assert player.full_name != "Changed name"
        assert player.role == PlayerRole.PLAYER

    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/players/{player_id}",
            auth=("admin", "change-me"),
            data={
                "full_name": original_name, "role": PlayerRole.CAPTAIN.value,
                "team_id": team_id, "active": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        player = db.get(Player, player_id)
        assert player.full_name == original_name
        assert player.role == PlayerRole.CAPTAIN
