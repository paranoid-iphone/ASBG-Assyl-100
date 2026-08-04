from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, GameProgram, GameProgramStage, Question, Stage


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_program_starts_with_intro_then_captain_stage_and_rules():
    with SessionLocal() as db:
        event = Event(name="Prologue")
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="Test", position=1)
        db.add(stage); db.flush()
        question = Question(stage_id=stage.id, title="Q1", text="Question", correct_answer="Answer", position=1)
        program = GameProgram(event_id=event.id, title="Main")
        db.add_all([question, program]); db.flush()
        db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=1))
        db.commit()
        event_id, program_id, token = event.id, program.id, event.display_token

    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/programs/{program_id}/launch",
            auth=("admin", "change-me"), follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get(f"/api/screen/{token}").json()["mode"] == "INTRO"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "captain_election_ready"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "captain_election_complete"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "rules"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "question"
