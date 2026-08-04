from sqlalchemy import select

from app.admin import team_slug, unique_team_code
from app.database import Base, SessionLocal, engine
from app.models import Event, Team


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_russian_and_kazakh_team_slugs():
    assert team_slug("Команда мечты") == "KOMANDA-MECHTY"
    assert team_slug("Бірлік") == "BIRLIK"
    assert team_slug("Қырандар") == "QYRANDAR"


def test_duplicate_team_slug_gets_suffix():
    with SessionLocal() as db:
        event = Event(name="Event")
        db.add(event)
        db.flush()
        db.add(Team(event_id=event.id, name="Бірлік", code="BIRLIK"))
        db.commit()
        assert unique_team_code(db, event.id, "", "Бірлік") == "BIRLIK-2"
