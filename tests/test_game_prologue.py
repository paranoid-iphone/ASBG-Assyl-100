from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, GameProgram, GameProgramStage, Question, Stage
from app.runtime_state import CUSTOM_SLIDES


def setup_function():
    CUSTOM_SLIDES.clear()
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
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "stage_intro"
        assert client.get(f"/api/screen/{token}").json()["mode"] == "STAGE_INTRO"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "question_intro"
        assert client.get(f"/api/screen/{token}").json()["mode"] == "QUESTION_INTRO"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "question"


def test_reserve_stage_requires_an_explicit_decision():
    with SessionLocal() as db:
        event = Event(name="Reserve", display_mode="ANSWER")
        db.add(event); db.flush()
        main = Stage(event_id=event.id, title="Main", position=1, system_key="what")
        reserve = Stage(event_id=event.id, title="Reserve", position=2, system_key="reserve")
        db.add_all([main, reserve]); db.flush()
        main_question = Question(stage_id=main.id, title="Q1", text="Question", correct_answer="Answer", position=1)
        reserve_question = Question(stage_id=reserve.id, title="Extra", text="Extra question", correct_answer="Extra answer", position=1)
        program = GameProgram(event_id=event.id, title="Main", status="RUNNING")
        db.add_all([main_question, reserve_question, program]); db.flush()
        db.add_all([
            GameProgramStage(program_id=program.id, stage_id=main.id, position=1),
            GameProgramStage(program_id=program.id, stage_id=reserve.id, position=2),
        ])
        event.current_question_id = main_question.id
        db.commit()
        token, reserve_question_id = event.display_token, reserve_question.id

    with TestClient(app) as client:
        response = client.post(f"/api/screen/{token}/next")
        assert response.status_code == 200
        assert response.json()["action"] == "stage_complete"
        assert client.get(f"/api/screen/{token}").json()["mode"] == "STAGE_COMPLETE"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "reserve_ready"
        assert client.get(f"/api/screen/{token}").json()["mode"] == "RESERVE_READY"
        reserve_intro = client.post(f"/api/screen/{token}/advance").json()
        assert reserve_intro["action"] == "question_intro"
        assert reserve_intro["question_id"] == reserve_question_id
        assert client.post(f"/api/screen/{token}/advance").json()["question_id"] == reserve_question_id


def test_back_falls_back_to_previous_program_question_when_history_is_empty():
    with SessionLocal() as db:
        event = Event(name="Back", display_mode="QUESTION")
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="Round", position=1)
        db.add(stage); db.flush()
        first = Question(stage_id=stage.id, title="Q1", text="First", correct_answer="A", position=1)
        second = Question(stage_id=stage.id, title="Q2", text="Second", correct_answer="B", position=2)
        program = GameProgram(event_id=event.id, title="Main", status="RUNNING")
        db.add_all([first, second, program]); db.flush()
        db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=1))
        event.current_question_id = second.id
        db.commit()
        token = event.display_token

    with TestClient(app) as client:
        response = client.post(f"/api/screen/{token}/back")
        assert response.status_code == 200
        assert response.json()["action"] == "back"
        state = client.get(f"/api/screen/{token}").json()
        assert state["mode"] == "TEAM_ANSWERS"
        assert state["question"]["ru"]["text"] == "First"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "forward"
        assert client.get(f"/api/screen/{token}").json()["question"]["ru"]["text"] == "Second"
