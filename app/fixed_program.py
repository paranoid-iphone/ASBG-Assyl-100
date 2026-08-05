from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import (
    Answer, Event, EventSlide, GameProgram, GameProgramStage, Question,
    QuestionType, Stage, StageType, TeamQuestionPrompt,
)


FIXED_PROGRAM_TITLE = "Интеллектуальная игра"


@dataclass(frozen=True)
class StageDefinition:
    key: str
    title: str
    title_kk: str
    stage_type: StageType
    description: str
    description_kk: str
    duration: int = 180
    submission: int = 60
    points: float = 5


FIXED_STAGES = (
    StageDefinition(
        "test", "Этап 0 · Тестовый раунд", "0 кезең · Сынақ раунды", StageType.QUIZ,
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
        submission=60,
    ),
    StageDefinition(
        "detective", "3 этап · Детектив", "3 кезең · Детектив", StageType.DETECTIVE,
        "Один общий детективный кейс для всех команд с персональными уликами.",
        "Барлық командаларға ортақ, жеке айғақтары бар бір детективтік іс.",
        duration=1200,
    ),
    StageDefinition(
        "reserve", "Резервный раунд", "Қосымша раунд", StageType.QUIZ,
        "Дополнительные вопросы на случай, если основная программа закончится раньше запланированного времени.",
        "Негізгі бағдарлама жоспарланған уақыттан ерте аяқталған жағдайда қойылатын қосымша сұрақтар.",
    ),
)


FIRST_STAGE_QUESTIONS = {
    "what": {
        "title": "Фунт мяса",
        "title_kk": "Бір фунт ет",
        "text": (
            "В пьесе Уильяма Шекспира «Венецианский купец» обвинитель потребовал вырезать из тела купца "
            "ровно один фунт мяса — не больше и не меньше. Молодой юрист Бальтазар нашёл в формулировке закона "
            "деталь, которая спасла купца. Что он заметил?"
        ),
        "text_kk": (
            "Уильям Шекспирдің «Венециялық саудагер» шығармасында айыптаушы саудагердің денесінен дәл бір фунт "
            "ет кесіп алуды талап етеді: артық та, кем де емес. Бальтазар есімді жас заңгер заңның бір егжей-тегжейін "
            "тауып, саудагерді аман алып қалады. Ол нені байқады?"
        ),
        "answer": "Закон разрешал забрать только мясо, но не кровь: вырезать мясо без кровопролития невозможно.",
        "answer_kk": "Заң тек ет алуға рұқсат берді, бірақ қан туралы айтылмады: етті қан шығармай кесу мүмкін емес.",
        "explanation": "Обвинитель мог потребовать ровно фунт мяса, но не имел права пролить ни капли крови.",
        "explanation_kk": "Айыптаушы дәл бір фунт етті талап ете алды, бірақ бір тамшы да қан төгуге құқығы болмады.",
    },
    "where": {
        "title": "Исчезнувшие следы",
        "title_kk": "Жоғалған іздер",
        "text": (
            "Войско днём перешло воду и подошло к крепости. Вокруг был ровный песок, поэтому после отступления "
            "остались отчётливые следы. За одну ночь все следы исчезли. Где находилась крепость?"
        ),
        "text_kk": (
            "Әскер күндіз суды кесіп өтіп, қамалға жетеді. Қамалдың айналасы тегіс құм болғандықтан, кейін шегінген "
            "әскердің іздері анық көрінеді. Бір түннен кейін барлық із жоғалып кетеді. Қамал қайда орналасқан?"
        ),
        "answer": "На острове.",
        "answer_kk": "Аралда.",
        "explanation": "Ночью прилив поднял уровень воды и смыл следы на песчаном берегу.",
        "explanation_kk": "Түнгі толысу кезінде су көтеріліп, құмды жағалаудағы іздерді шайып кеткен.",
    },
    "when": {
        "title": "Братья и сёстры",
        "title_kk": "Ұлдар мен қыздар",
        "text": (
            "У одной девочки число братьев равно числу сестёр. У каждого мальчика братьев в два раза меньше, "
            "чем сестёр. Сколько в семье мальчиков и девочек?"
        ),
        "text_kk": (
            "Бір қыздың ұл бауырлары мен қыз бауырларының саны бірдей. Әр ұлдың ұл бауырларының саны қыз "
            "бауырларының санынан екі есе аз. Отбасында неше ұл және неше қыз бар?"
        ),
        "answer": "3 мальчика и 4 девочки.",
        "answer_kk": "3 ұл және 4 қыз.",
        "explanation": "У девочки три брата и три другие сестры; у мальчика два брата и четыре сестры.",
        "explanation_kk": "Қыздың үш ағасы не інісі және өзінен басқа үш әпкесі не сіңлісі бар; ұлдың екі бауыры және төрт қыз бауыры бар.",
    },
}

LEGACY_DEMO_TITLES = {
    "Три выключателя", "Бой часов", "Бутылка и пробка", "Две верёвки", "Три таблетки",
}


def _install_first_stage_questions(db: Session, stages: list[Stage]) -> None:
    """Replace only the known demo pack and install the approved bilingual questions."""
    by_key = {stage.system_key: stage for stage in stages}
    for stage in stages:
        for question in list(stage.questions):
            if stage.system_key in {"what", "where", "when"} and question.title in LEGACY_DEMO_TITLES:
                # Production databases can already contain rehearsal answers
                # and Telegram prompt references for the demo pack. Remove
                # those dependants first to satisfy PostgreSQL foreign keys.
                db.execute(delete(Answer).where(Answer.question_id == question.id))
                db.execute(delete(TeamQuestionPrompt).where(TeamQuestionPrompt.question_id == question.id))
                if stage.event.current_question_id == question.id:
                    stage.event.current_question_id = None
                db.delete(question)
        db.flush()

    for key, data in FIRST_STAGE_QUESTIONS.items():
        stage = by_key[key]
        if db.scalar(select(Question).where(Question.stage_id == stage.id, Question.title == data["title"])):
            continue
        next_position = (db.scalar(select(func.max(Question.position)).where(Question.stage_id == stage.id)) or 0) + 1
        db.add(Question(
            stage_id=stage.id,
            position=next_position,
            title=data["title"], title_kk=data["title_kk"],
            text=data["text"], text_kk=data["text_kk"],
            correct_answer=data["answer"], correct_answer_kk=data["answer_kk"],
            explanation=data["explanation"], explanation_kk=data["explanation_kk"],
            duration_seconds=180, submission_seconds=60,
            personal_answers_enabled=False, team_answers_enabled=True,
            personal_points=0, team_points=5, show_anonymous_answers=True,
        ))
    db.flush()


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
            if stage.title in {"1 этап · Что? Где? Когда?", "1 этап · Что? Где? Когда"}:
                stage.title = definition.title
                stage.title_kk = definition.title_kk
                stage.description = definition.description
                stage.description_kk = definition.description_kk
            if definition.key == "detective":
                # Remove the obsolete wording about separate team cases from
                # installations created by the old generator.
                stage.title = definition.title
                stage.title_kk = definition.title_kk
                stage.description = definition.description
                stage.description_kk = definition.description_kk
        ordered_stages.append(stage)

    _install_first_stage_questions(db, ordered_stages)

    test_stage = next(stage for stage in ordered_stages if stage.system_key == "test")
    # Stage zero is a fixed training step, not a scored game round.
    test_stage.title = "Этап 0 · Тестовый раунд"
    test_stage.title_kk = "0 кезең · Сынақ раунды"
    test_stage.default_team_points = 0
    if not test_stage.questions:
        test_stage.questions.append(Question(
            position=1,
            title="Тестовый вопрос",
            title_kk="Сынақ сұрағы",
            text="Часы отбивают шесть ударов за пять секунд. За сколько секунд те же часы отобьют двенадцать ударов?",
            text_kk="Сағат алты рет бес секундта соғады. Сол сағат он екі рет неше секундта соғады?",
            correct_answer="11 секунд",
            correct_answer_kk="11 секунд",
            explanation="Между шестью ударами пять интервалов. Один интервал длится секунду, а между двенадцатью ударами — одиннадцать интервалов.",
            explanation_kk="Алты соққының арасында бес аралық бар. Бір аралық бір секундқа созылады, ал он екі соққының арасында он бір аралық бар.",
            personal_answers_enabled=False,
            team_answers_enabled=True,
            personal_points=0,
            team_points=0,
            duration_seconds=180,
            submission_seconds=60,
            show_anonymous_answers=True,
        ))
    for practice_question in test_stage.questions:
        practice_question.personal_points = 0
        practice_question.team_points = 0

    choice_stage = next(stage for stage in ordered_stages if stage.system_key == "choice")
    # Upgrade the old short default while preserving any custom value that an
    # organizer has already deliberately configured.
    if choice_stage.default_submission_seconds in {20, 30}:
        choice_stage.default_submission_seconds = 60
    for choice_question in choice_stage.questions:
        if choice_question.submission_seconds in {20, 30}:
            choice_question.submission_seconds = 60

    # Give the captain enough time to choose a respondent and type the team's
    # answer in every block of stage one. Upgrade only old standard values so
    # an organizer's deliberate custom timing remains untouched.
    for first_stage in (stage for stage in ordered_stages if stage.system_key in {"what", "where", "when"}):
        if first_stage.default_submission_seconds in {20, 30}:
            first_stage.default_submission_seconds = 40
        for question in first_stage.questions:
            if question.submission_seconds in {20, 30}:
                question.submission_seconds = 40

    if not event.slides_initialized:
        if not event.slides:
            db.add_all([
                EventSlide(
                    event_id=event.id, position=1,
                    title="Сегодня вас ждут три этапа",
                    text="Командные вопросы, задачи с выбором решения и финальная детективная игра.",
                    title_kk="Бүгін сіздерді үш кезең күтеді",
                    text_kk="Командалық сұрақтар, шешім таңдау тапсырмалары және финалдық детектив ойыны.",
                ),
                EventSlide(
                    event_id=event.id, position=2,
                    title="Сначала выберем капитана",
                    text="Зайдите в Telegram-бот и проголосуйте. За себя голосовать нельзя.",
                    title_kk="Алдымен капитанды таңдаймыз",
                    text_kk="Telegram-ботқа кіріп, дауыс беріңіз. Өзіңізге дауыс беруге болмайды.",
                ),
            ])
        event.slides_initialized = True

    # Older installations may contain several legacy programs. The editor
    # displays the newest one, while a currently running/paused program must
    # remain authoritative. Previously we updated the oldest program, so the
    # practice stage was created successfully but stayed invisible in the UI.
    programs = list(db.scalars(
        select(GameProgram).where(GameProgram.event_id == event.id)
        .order_by(GameProgram.created_at.desc(), GameProgram.id.desc())
    ).all())
    program = next((item for item in programs if item.status in {"RUNNING", "PAUSED"}), None)
    if program is None and programs:
        program = programs[0]
    if program is None:
        program = GameProgram(
            event_id=event.id,
            title=FIXED_PROGRAM_TITLE,
            description="Тестовый вопрос → Что? → Где? → Когда? → выбор решения → детектив → резервный раунд",
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
