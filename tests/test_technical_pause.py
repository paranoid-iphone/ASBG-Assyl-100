from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, GameProgram, Player, PlayerRole, Question, Stage, Team


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_technical_pause_unlocks_player_edit_and_resumes_timer():
    with SessionLocal() as db:
        event = Event(
            name="Pause", display_mode="TIMER", timer_duration_seconds=60,
            timer_started_at=datetime.utcnow() - timedelta(seconds=10),
        )
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="Round", position=1)
        team = Team(event_id=event.id, name="Alpha", code="ALPHA")
        program = GameProgram(event_id=event.id, title="Main", status="RUNNING")
        db.add_all([stage, team, program]); db.flush()
        question = Question(stage_id=stage.id, title="Q", text="Text", correct_answer="A")
        player = Player(
            team_id=team.id, full_name="Old name", registration_code="PLAYER",
            role=PlayerRole.PLAYER, active=True,
        )
        db.add_all([question, player]); db.flush()
        event.current_question_id = question.id
        db.commit()
        event_id, team_id, player_id = event.id, team.id, player.id

    auth = ("admin", "change-me")
    with TestClient(app) as client:
        assert client.post(f"/admin/events/{event_id}/live/pause", auth=auth).status_code == 200
        response = client.post(
            f"/admin/events/{event_id}/players/{player_id}", auth=auth,
            data={
                "full_name": "Correct name", "role": "PLAYER",
                "team_id": team_id, "active": "on",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.post(f"/admin/events/{event_id}/live/resume", auth=auth).status_code == 200

    with SessionLocal() as db:
        event = db.get(Event, event_id)
        player = db.get(Player, player_id)
        program = db.scalar(select(GameProgram).where(GameProgram.event_id == event_id))
        assert player.full_name == "Correct name"
        assert program.status == "RUNNING"
        assert event.display_mode == "TIMER"
        assert 45 <= event.timer_duration_seconds <= 55
        assert event.timer_started_at is not None
