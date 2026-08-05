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
        assert legacy_stage.system_key == "main"
        assert db.scalar(select(func.count(Stage.id)).where(Stage.event_id == event.id)) == 5
        assert db.scalar(select(func.count(GameProgram.id)).where(GameProgram.event_id == event.id)) == 1
        assert db.scalar(select(func.count(GameProgramStage.id)).where(GameProgramStage.program_id == first.id)) == 5
        assert db.scalar(select(func.count(Question.id)).where(Question.stage_id == legacy_stage.id)) == 1
        assert db.scalar(select(Question).where(
            Question.stage_id == legacy_stage.id,
            Question.title == "Существующий вопрос",
        )) is not None


def test_combined_demo_stage_becomes_one_question_stage():
    with SessionLocal() as db:
        event = Event(name="Assyl 100")
        db.add(event); db.flush()
        combined = Stage(event_id=event.id, title="1 этап · Что? Где? Когда?", position=1)
        db.add(combined); db.flush()
        for position, title in enumerate((
            "Три выключателя", "Бой часов", "Бутылка и пробка", "Две верёвки", "Три таблетки",
        ), 1):
            db.add(Question(
                stage_id=combined.id, position=position, title=title,
                text=title, correct_answer=title,
            ))
        db.flush()

        ensure_fixed_program(db, event)
        db.flush()

        stages = {stage.system_key: stage for stage in db.scalars(
            select(Stage).where(Stage.event_id == event.id)
        ).all()}
        assert stages["main"].title == "1 этап · Командные вопросы"
        question_titles = list(db.scalars(
            select(Question.title).where(Question.stage_id == stages["main"].id).order_by(Question.position)
        ).all())
        assert question_titles == ["Фунт мяса", "Исчезнувшие следы", "Братья и сёстры"]
        assert all(
            question.submission_seconds == 60
            for question in db.scalars(select(Question).where(Question.stage_id == stages["main"].id)).all()
        )
        test_stage = db.scalar(select(Stage).where(Stage.event_id == event.id, Stage.system_key == "test"))
        assert test_stage.title == "Этап 0 · Тестовый раунд"
        assert test_stage.title_kk == "0 кезең · Сынақ раунды"
        assert len(test_stage.questions) == 1
        assert test_stage.questions[0].team_points == 0
        assert test_stage.questions[0].duration_seconds == 180
        assert test_stage.questions[0].submission_seconds == 60
        choice_stage = db.scalar(select(Stage).where(Stage.event_id == event.id, Stage.system_key == "choice"))
        assert choice_stage.default_submission_seconds == 60
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
        assert choice.default_submission_seconds == 60
        assert choice.questions[0].submission_seconds == 60
        assert detective.system_key == "detective"


def test_fixed_stages_are_linked_to_the_active_program_in_legacy_database():
    with SessionLocal() as db:
        event = Event(name="Assyl 100", registration_code="ACTIVE")
        db.add(event)
        db.flush()
        old_program = GameProgram(event_id=event.id, title="Old draft", status="DRAFT")
        active_program = GameProgram(event_id=event.id, title="Current game", status="RUNNING")
        db.add_all([old_program, active_program])
        db.flush()

        selected = ensure_fixed_program(db, event)
        db.commit()

        assert selected.id == active_program.id
        links = db.scalars(
            select(GameProgramStage)
            .where(GameProgramStage.program_id == active_program.id)
            .order_by(GameProgramStage.position)
        ).all()
        assert len(links) == 5
        assert links[0].stage.system_key == "test"
        assert len(links[0].stage.questions) == 1
