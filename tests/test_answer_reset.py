from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Answer, AnswerScope, Event, Question, Stage, Team


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_admin_can_reset_one_answer_and_all_question_answers():
    with SessionLocal() as db:
        event = Event(name="Answer reset")
        db.add(event); db.flush()
        stage = Stage(event_id=event.id, title="What", position=1)
        db.add(stage); db.flush()
        question = Question(stage_id=stage.id, title="Q", text="?", correct_answer="A", position=1)
        db.add(question); db.flush()
        first = Team(event_id=event.id, name="Alpha", code="alpha")
        second = Team(event_id=event.id, name="Beta", code="beta")
        db.add_all([first, second]); db.flush()
        a1 = Answer(question_id=question.id, team_id=first.id, scope=AnswerScope.TEAM, text="one")
        a2 = Answer(question_id=question.id, team_id=second.id, scope=AnswerScope.TEAM, text="two")
        db.add_all([a1, a2]); db.commit()
        event_id, question_id, answer_id = event.id, question.id, a1.id

    with TestClient(app) as client:
        auth = ("admin", "change-me")
        response = client.post(
            f"/admin/events/{event_id}/answers/{answer_id}/reset", auth=auth,
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            remaining = list(db.scalars(select(Answer).where(Answer.question_id == question_id)))
            assert len(remaining) == 1

        response = client.post(
            f"/admin/events/{event_id}/questions/{question_id}/answers/reset", auth=auth,
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.scalar(select(Answer).where(Answer.question_id == question_id)) is None
