import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, Question, Stage


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_export_then_import_content():
    with SessionLocal() as db:
        source = Event(name="Source", registration_code="SOURCE")
        target = Event(name="Target", registration_code="TARGET")
        db.add_all([source, target]); db.flush()
        stage = Stage(event_id=source.id, title="Логика", title_kk="Логика", position=1)
        db.add(stage); db.flush()
        db.add(Question(
            stage_id=stage.id, position=1, title="Что?", title_kk="Не?",
            text="Вопрос", text_kk="Сұрақ", correct_answer="Ответ",
            correct_answer_kk="Жауап", duration_seconds=75,
        ))
        db.commit()
        source_id, target_id = source.id, target.id

    with TestClient(app) as client:
        auth = ("admin", "change-me")
        exported = client.get(f"/admin/events/{source_id}/content/export", auth=auth)
        assert exported.status_code == 200
        payload = exported.json()
        assert payload["stages"][0]["questions"][0]["duration_seconds"] == 75

        imported = client.post(
            f"/admin/events/{target_id}/content/import",
            auth=auth,
            files={"content_file": ("content.json", json.dumps(payload, ensure_ascii=False), "application/json")},
            follow_redirects=False,
        )
        assert imported.status_code == 303

    with SessionLocal() as db:
        stage = db.scalar(select(Stage).where(Stage.event_id == target_id))
        question = db.scalar(select(Question).where(Question.stage_id == stage.id))
        assert stage.title_kk == "Логика"
        assert question.text_kk == "Сұрақ"
        assert question.duration_seconds == 180
        assert question.submission_seconds == 60
