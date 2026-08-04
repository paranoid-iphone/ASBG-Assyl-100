from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import Event, GameProgram, GameProgramStage, QuestionType, Stage, StageType


FIXED_PROGRAM_TITLE = "Интеллектуальная игра"


@dataclass(frozen=True)
class StageDefinition:
    key: str
    title: str
    title_kk: str
    stage_type: StageType
    description: str
    description_kk: str
    duration: int = 60
    submission: int = 30
    points: float = 5


FIXED_STAGES = (
    StageDefinition(
        "test", "Тестовый вопрос", "Сынақ сұрағы", StageType.QUIZ,
        "Полный тренировочный цикл без начисления баллов.",
        "Ұпай берілмейтін толық жаттығу кезеңі.",
        points=0,
    ),
    StageDefinition(
        "what", "1 этап · Что?", "1 кезең · Не?", StageType.QUIZ,
        "Вопросы блока «Что?» со свободным командным ответом.",
        "«Не?» блогының еркін командалық жауабы бар сұрақтары.",
    ),
    StageDefinition(
        "where", "1 этап · Где?", "1 кезең · Қайда?", StageType.QUIZ,
        "Вопросы блока «Где?» со свободным командным ответом.",
        "«Қайда?» блогының еркін командалық жауабы бар сұрақтары.",
    ),
    StageDefinition(
        "when", "1 этап · Когда?", "1 кезең · Қашан?", StageType.QUIZ,
        "Вопросы блока «Когда?» со свободным командным ответом.",
        "«Қашан?» блогының еркін командалық жауабы бар сұрақтары.",
    ),
    StageDefinition(
        "choice", "2 этап · Выбор решения", "2 кезең · Шешімді таңдау", StageType.QUIZ,
        "Задачи с выбором одного варианта и, при необходимости, объяснением.",
        "Бір нұсқаны және қажет болса түсіндірмені таңдауға арналған тапсырмалар.",
    ),
    StageDefinition(
        "detective", "3 этап · Детектив", "3 кезең · Детектив", StageType.DETECTIVE,
        "Один общий детективный кейс для всех команд с персональными уликами.",
        "Барлық командаларға ортақ, жеке айғақтары бар бір детективтік іс.",
        duration=1200,
    ),
)


def _legacy_matches(stages: list[Stage]) -> dict[str, Stage]:
    """Attach existing content to the fixed structure without deleting or renaming it."""
    matches: dict[str, Stage] = {stage.system_key: stage for stage in stages if stage.system_key}
    unassigned = [stage for stage in stages if not stage.system_key]

    detective = next((stage for stage in unassigned if stage.stage_type == StageType.DETECTIVE), None)
    if detective and "detective" not in matches:
        matches["detective"] = detective
        unassigned.remove(detective)

    choice = next((
        stage for stage in unassigned
        if any(question.question_type in {QuestionType.CHOICE, QuestionType.CHOICE_EXPLANATION} for question in stage.questions)
    ), None)
    if choice and "choice" not in matches:
        matches["choice"] = choice
        unassigned.remove(choice)

    first_quiz = next((stage for stage in unassigned if stage.stage_type == StageType.QUIZ), None)
    if first_quiz and "what" not in matches:
        matches["what"] = first_quiz

    return matches


def ensure_fixed_program(db: Session, event: Event) -> GameProgram:
    """Create the fixed event structure once and preserve all existing questions."""
    stages = list(db.scalars(
        select(Stage).where(Stage.event_id == event.id).order_by(Stage.position)
    ).all())
    matches = _legacy_matches(stages)
    next_position = (db.scalar(select(func.max(Stage.position)).where(Stage.event_id == event.id)) or 0) + 1

    ordered_stages: list[Stage] = []
    for definition in FIXED_STAGES:
        stage = matches.get(definition.key)
        if stage is None:
            stage = Stage(
                event_id=event.id,
                system_key=definition.key,
                position=next_position,
                stage_type=definition.stage_type,
                title=definition.title,
                title_kk=definition.title_kk,
                description=definition.description,
                description_kk=definition.description_kk,
                default_duration_seconds=definition.duration,
                default_submission_seconds=definition.submission,
                default_team_points=definition.points,
                detective_duration_seconds=definition.duration if definition.stage_type == StageType.DETECTIVE else 1200,
            )
            next_position += 1
            db.add(stage)
            db.flush()
        else:
            stage.system_key = definition.key
        ordered_stages.append(stage)

    program = db.scalar(
        select(GameProgram).where(GameProgram.event_id == event.id).order_by(GameProgram.id)
    )
    if program is None:
        program = GameProgram(
            event_id=event.id,
            title=FIXED_PROGRAM_TITLE,
            description="Тестовый вопрос → Что? → Где? → Когда? → выбор решения → детектив",
        )
        db.add(program)
        db.flush()

    linked_stage_ids = set(db.scalars(
        select(GameProgramStage.stage_id).where(GameProgramStage.program_id == program.id)
    ).all())
    expected_stage_ids = {stage.id for stage in ordered_stages}
    if linked_stage_ids != expected_stage_ids:
        db.execute(delete(GameProgramStage).where(GameProgramStage.program_id == program.id))
        db.flush()
        for position, stage in enumerate(ordered_stages, 1):
            db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=position))

    return program
