from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.fixed_program import ensure_fixed_program
from app.models import Event, EventSlide, GameProgram, GameProgramStage, Question, QuestionType, Stage, StageType


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_fixed_program_preserves_existing_questions_and_is_idempotent():
    with SessionLocal() as db:
        event = Event(name="Assyl 100", registration_code="ACTIVE")
        db.add(event)
        db.flush()
        legacy_stage = Stage(event_id=event.id, title="Старый этап ЧГК", position=1)
        db.add(legacy_stage)
        db.flush()
        db.add(Question(
            stage_id=legacy_stage.id,
            position=1,
            title="Существующий вопрос",
            text="Текст",
            correct_answer="Ответ",
            question_type=QuestionType.TEXT,
        ))
        db.flush()

        first = ensure_fixed_program(db, event)
        db.flush()
        second = ensure_fixed_program(db, event)
        db.commit()

        assert first.id == second.id
        assert legacy_stage.system_key == "what"
        assert db.scalar(select(func.count(Stage.id)).where(Stage.event_id == event.id)) == 7
        assert db.scalar(select(func.count(GameProgram.id)).where(GameProgram.event_id == event.id)) == 1
        assert db.scalar(select(func.count(GameProgramStage.id)).where(GameProgramStage.program_id == first.id)) == 7
        assert db.scalar(select(func.count(Question.id)).where(Question.stage_id == legacy_stage.id)) == 1
        test_stage = db.scalar(select(Stage).where(Stage.event_id == event.id, Stage.system_key == "test"))
        assert len(test_stage.questions) == 1
        assert test_stage.questions[0].team_points == 0
        assert db.scalar(select(func.count(EventSlide.id)).where(EventSlide.event_id == event.id)) == 2


def test_existing_choice_and_detective_stages_are_reused():
    with SessionLocal() as db:
        event = Event(name="Assyl 100", registration_code="ACTIVE")
        db.add(event)
        db.flush()
        choice = Stage(event_id=event.id, title="Выбор", position=1)
        detective = Stage(event_id=event.id, title="Старый детектив", position=2, stage_type=StageType.DETECTIVE)
        db.add_all([choice, detective])
        db.flush()
        db.add(Question(
            stage_id=choice.id,
            position=1,
            title="Выберите",
            text="Текст",
            correct_answer="А",
            question_type=QuestionType.CHOICE,
        ))
        db.flush()

        ensure_fixed_program(db, event)
        db.commit()

        assert choice.system_key == "choice"
        assert detective.system_key == "detective"
