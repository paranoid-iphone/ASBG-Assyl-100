from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, Question, Stage


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_public_screen_and_state_api():
    with SessionLocal() as db:
        event = Event(name="Screen test", description="Hello")
        db.add(event)
        db.commit()
        token = event.display_token
    with TestClient(app) as client:
        page = client.get(f"/screen/{token}")
        state = client.get(f"/api/screen/{token}")
        assert page.status_code == 200
        assert 'id="previousSlide"' in page.text
        assert 'id="nextSlide"' in page.text
        assert "ArrowRight" in page.text
        assert "ArrowLeft" in page.text
        assert state.status_code == 200
        assert state.json()["event"] == "Screen test"


def test_click_flow_question_timer_answer_next():
    with SessionLocal() as db:
        event = Event(name="Flow")
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="Stage", position=1)
        db.add(stage); db.flush()
        db.add_all([
            Question(stage_id=stage.id, title="Q1", text="One?", correct_answer="One", position=1, duration_seconds=30),
            Question(stage_id=stage.id, title="Q2", text="Two?", correct_answer="Two", position=2, duration_seconds=45),
        ])
        db.commit()
        token = event.display_token
    with TestClient(app) as client:
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "question"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "discussion_ready"
        assert client.post(f"/api/screen/{token}/timer/start").json()["mode"] == "TIMER"
        assert client.post(f"/api/screen/{token}/timer-adjust?seconds=-300").status_code == 200
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "submission_ready"
        assert client.post(f"/api/screen/{token}/timer/start").json()["mode"] == "SUBMISSION"
        assert client.post(f"/api/screen/{token}/timer-adjust?seconds=-300").status_code == 200
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "answer"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "team_answers"
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "question"
        state = client.get(f"/api/screen/{token}").json()
        assert state["question"]["ru"]["title"] == "Q2"


def test_ready_timers_at_zero_can_be_skipped_without_starting():
    with SessionLocal() as db:
        event = Event(name="Skip zero")
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="Stage", position=1)
        db.add(stage); db.flush()
        db.add(Question(
            stage_id=stage.id, title="Q", text="Question", correct_answer="Answer",
            position=1, duration_seconds=30, submission_seconds=20,
        ))
        db.commit()
        token = event.display_token
    with TestClient(app) as client:
        client.post(f"/api/screen/{token}/advance")
        client.post(f"/api/screen/{token}/advance")
        client.post(f"/api/screen/{token}/timer-adjust?seconds=-300")
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "submission_ready"
        client.post(f"/api/screen/{token}/timer-adjust?seconds=-300")
        assert client.post(f"/api/screen/{token}/advance").json()["action"] == "answer"
