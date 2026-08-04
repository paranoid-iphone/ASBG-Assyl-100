from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, Question, Stage


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def create_questions() -> tuple[int, int, int, int]:
    with SessionLocal() as db:
        event = Event(name="Assyl 100", registration_code="ACTIVE")
        db.add(event)
        db.flush()
        stage = Stage(event_id=event.id, system_key="what", title="Что?", position=1)
        db.add(stage)
        db.flush()
        first = Question(stage_id=stage.id, position=1, title="Первый", text="1", correct_answer="1")
        second = Question(stage_id=stage.id, position=2, title="Второй", text="2", correct_answer="2")
        db.add_all([first, second])
        db.commit()
        return event.id, stage.id, first.id, second.id


def test_question_can_be_moved_and_copied():
    event_id, stage_id, first_id, second_id = create_questions()
    with TestClient(app) as client:
        moved = client.post(
            f"/admin/events/{event_id}/questions/{second_id}/move",
            auth=("admin", "change-me"),
            data={"direction": "up"},
            follow_redirects=False,
        )
        assert moved.status_code == 303
        copied = client.post(
            f"/admin/events/{event_id}/questions/{first_id}/copy",
            auth=("admin", "change-me"),
            follow_redirects=False,
        )
        assert copied.status_code == 303

    with SessionLocal() as db:
        questions = db.scalars(select(Question).where(
            Question.stage_id == stage_id
        ).order_by(Question.position)).all()
        assert [question.title for question in questions[:2]] == ["Второй", "Первый"]
        assert questions[-1].title == "Первый — копия"
