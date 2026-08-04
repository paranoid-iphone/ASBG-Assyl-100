from datetime import datetime
import secrets

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .runtime_state import TEMPORARY_SENDERS

from .models import (
    Answer, AnswerScope, AuditLog, DetectiveCase, DetectiveClue, DetectiveStatus,
    DetectiveSubmission, Event, Player, PlayerRole, Question, QuestionStatus,
    ScoreAdjustment, Stage, StageType, Team,
)


class GameError(ValueError):
    pass


def entity_event_id(entity) -> int | None:
    if isinstance(entity, Event):
        return entity.id
    if isinstance(entity, Stage):
        return entity.event_id
    if isinstance(entity, Question):
        return entity.stage.event_id if entity.stage else None
    return getattr(entity, "event_id", None)


def audit(db: Session, actor: str, action: str, entity, details: str = "") -> None:
    db.add(AuditLog(
        event_id=entity_event_id(entity), actor=actor, action=action,
        entity_type=type(entity).__name__, entity_id=str(getattr(entity, "id", "")) or None,
        details=details,
    ))


def register_player(db: Session, code: str, telegram_user_id: str, username: str | None) -> Player:
    player = db.scalar(select(Player).where(func.upper(Player.registration_code) == code.strip().upper()))
    if not player or not player.active:
        raise GameError("Код регистрации не найден.")
    existing = db.scalar(select(Player).where(Player.telegram_user_id == telegram_user_id))
    if existing and existing.id != player.id:
        raise GameError("Этот Telegram-аккаунт уже привязан к другому игроку.")
    if player.telegram_user_id and player.telegram_user_id != telegram_user_id:
        raise GameError("Код уже использован другим Telegram-аккаунтом.")
    player.telegram_user_id = telegram_user_id
    player.telegram_username = username
    player.registered_at = player.registered_at or datetime.utcnow()
    audit(db, f"tg:{telegram_user_id}", "player.register", player)
    db.commit()
    return player


def self_register_player(
    db: Session,
    event_id: int,
    team_code: str,
    full_name: str,
    telegram_user_id: str,
    username: str | None,
    preferred_language: str = "RU",
) -> Player:
    existing = get_player_by_telegram(db, telegram_user_id)
    if existing:
        raise GameError("Этот Telegram-аккаунт уже зарегистрирован.")
    team = db.scalar(select(Team).where(
        Team.event_id == event_id,
        func.upper(Team.code) == team_code.strip().upper(),
        Team.active.is_(True),
    ))
    if not team:
        raise GameError("Код команды не найден.")
    if not full_name.strip():
        raise GameError("Имя не может быть пустым.")
    player = Player(
        team_id=team.id,
        full_name=full_name.strip(),
        registration_code=f"AUTO-{secrets.token_hex(8).upper()}",
        role=PlayerRole.PLAYER,
        telegram_user_id=telegram_user_id,
        telegram_username=username,
        registered_at=datetime.utcnow(),
        preferred_language=preferred_language if preferred_language in {"RU", "KK"} else "RU",
    )
    db.add(player)
    db.flush()
    audit(db, f"tg:{telegram_user_id}", "player.self_register", player)
    db.commit()
    return player


def get_player_by_telegram(db: Session, telegram_user_id: str) -> Player | None:
    return db.scalar(select(Player).where(Player.telegram_user_id == telegram_user_id, Player.active.is_(True)))


def active_question(db: Session, event_id: int) -> Question | None:
    return db.scalar(
        select(Question).join(Stage).where(
            Stage.event_id == event_id, Question.status == QuestionStatus.OPEN
        ).order_by(Question.opened_at.desc(), Question.id.desc())
    )


active_round = active_question


def active_detective_case(db: Session, player: Player) -> DetectiveCase | None:
    if not player.team:
        return None
    return db.scalar(
        select(DetectiveCase).join(Stage).where(
            Stage.event_id == player.team.event_id,
            Stage.stage_type == StageType.DETECTIVE,
            Stage.detective_status == DetectiveStatus.RUNNING,
            DetectiveCase.team_id == player.team_id,
            DetectiveCase.approved.is_(True),
        )
    )


def detective_clue_for_player(db: Session, player: Player) -> DetectiveClue | None:
    case = active_detective_case(db, player)
    if not case:
        return None
    return db.scalar(select(DetectiveClue).where(
        DetectiveClue.case_id == case.id, DetectiveClue.player_id == player.id
    ))


def submit_detective_answer(db: Session, player: Player, option: str) -> DetectiveSubmission:
    if player.role not in {PlayerRole.CAPTAIN, PlayerRole.ADMIN}:
        raise GameError("Окончательный ответ детектива может отправить только капитан.")
    case = active_detective_case(db, player)
    if not case:
        raise GameError("Сейчас детективная игра не запущена.")
    if case.submission:
        raise GameError("Ответ вашей команды уже зафиксирован и не может быть изменён.")
    options = __import__("json").loads(case.options_json)
    if option not in options:
        raise GameError("Такого варианта ответа нет.")
    now = datetime.utcnow()
    elapsed = max(0, int((now - case.stage.detective_started_at).total_seconds())) if case.stage.detective_started_at else 0
    correct = option == case.correct_option
    rank = None
    points = 0.0
    if correct:
        rank = (db.scalar(select(func.count(DetectiveSubmission.id)).where(
            DetectiveSubmission.stage_id == case.stage_id,
            DetectiveSubmission.is_correct.is_(True),
        )) or 0) + 1
        schedule = [float(x.strip()) for x in case.stage.detective_points.split(",") if x.strip()]
        points = schedule[min(rank - 1, len(schedule) - 1)] if schedule else 0
    submission = DetectiveSubmission(
        case_id=case.id, stage_id=case.stage_id, team_id=player.team_id,
        captain_id=player.id, selected_option=option, is_correct=correct,
        submitted_at=now, elapsed_seconds=elapsed, rank=rank, points_awarded=points,
    )
    db.add(submission)
    audit(db, f"player:{player.id}", "detective.submit", case, f"correct={correct}; rank={rank}; points={points}")
    db.commit()
    return submission


def submit_answer(
    db: Session, player: Player, text: str, scope: AnswerScope,
    respondent_player_id: int | None = None, explanation: str = "",
) -> Answer:
    if not player.team:
        raise GameError("Игрок не привязан к команде.")
    question = active_question(db, player.team.event_id)
    if not question:
        raise GameError("Сейчас нет открытого вопроса.")
    if scope == AnswerScope.PERSONAL and not question.personal_answers_enabled:
        raise GameError("Личные ответы на этот вопрос отключены.")
    if scope == AnswerScope.TEAM and not question.team_answers_enabled:
        raise GameError("Командные ответы на этот вопрос отключены.")
    temporary_sender = TEMPORARY_SENDERS.get((question.id, player.team_id)) == player.id
    if scope == AnswerScope.TEAM and player.role not in {PlayerRole.CAPTAIN, PlayerRole.ADMIN} and not temporary_sender:
        raise GameError("Официальный ответ команды отправляет капитан.")
    event = player.team.event
    if event.current_question_id != question.id or event.display_mode != "SUBMISSION":
        raise GameError("Сейчас ответы не принимаются.")
    if not event.timer_started_at:
        raise GameError("Окно отправки ответа ещё не открыто.")
    elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds()
    if elapsed >= event.timer_duration_seconds:
        raise GameError("Время отправки ответа истекло.")
    if not text.strip():
        raise GameError("Ответ не может быть пустым.")
    query = select(Answer).where(Answer.question_id == question.id, Answer.scope == scope)
    query = query.where(Answer.player_id == player.id) if scope == AnswerScope.PERSONAL else query.where(Answer.team_id == player.team_id)
    answer = db.scalar(query)
    if answer:
        raise GameError("Ответ уже зафиксирован и не может быть изменён.")
    if respondent_player_id:
        respondent = db.get(Player, respondent_player_id)
        if not respondent or respondent.team_id != player.team_id or not respondent.active:
            raise GameError("Выбранный отвечающий не состоит в вашей команде.")
    answer = Answer(
        question_id=question.id, scope=scope,
        player_id=player.id if scope == AnswerScope.PERSONAL else None,
        team_id=player.team_id, text=text.strip(), explanation=explanation.strip(),
        respondent_player_id=respondent_player_id,
    )
    db.add(answer)
    audit(db, f"player:{player.id}", "answer.submit", question, scope.value)
    db.commit()
    return answer


def set_question_status(db: Session, question: Question, status: QuestionStatus, actor: str = "admin") -> None:
    if status == QuestionStatus.OPEN:
        # There can be only one live question per game.
        others = db.scalars(
            select(Question).join(Stage).where(
                Stage.event_id == question.stage.event_id,
                Question.status == QuestionStatus.OPEN,
                Question.id != question.id,
            )
        ).all()
        for other in others:
            other.status = QuestionStatus.LOCKED
            other.closed_at = datetime.utcnow()
        question.opened_at = datetime.utcnow()
    elif status in {QuestionStatus.LOCKED, QuestionStatus.PUBLISHED}:
        question.closed_at = datetime.utcnow()
    question.status = status
    audit(db, actor, "question.status", question, status.value)
    db.commit()


set_round_status = set_question_status


def normalize(value: str) -> str:
    return " ".join(value.casefold().strip().replace("ё", "е").split()).strip(" .,!?:;\"'")


def grade_answer(
    db: Session, answer: Answer, correct: bool | None = None,
    actor: str = "admin", points: float | None = None,
) -> None:
    question = answer.question
    result = normalize(answer.text) == normalize(question.correct_answer) if correct is None else correct
    answer.is_correct = result
    answer.points_awarded = max(0, points) if points is not None else (
        (question.personal_points if answer.scope == AnswerScope.PERSONAL else question.team_points)
        if result else 0
    )
    audit(db, actor, "answer.grade", question, f"answer={answer.id}; correct={result}; points={answer.points_awarded}")
    db.commit()


def grade_all_answers(db: Session, question: Question, actor: str = "admin") -> None:
    for answer in db.scalars(select(Answer).where(Answer.question_id == question.id)).all():
        grade_answer(db, answer, None, actor)


def adjust_score(db: Session, event_id: int, points: float, reason: str, player_id=None, team_id=None, actor="admin"):
    if (player_id is None) == (team_id is None):
        raise GameError("Нужно выбрать либо игрока, либо команду.")
    item = ScoreAdjustment(event_id=event_id, player_id=player_id, team_id=team_id, points=points, reason=reason)
    db.add(item)
    audit(db, actor, "score.adjust", item, f"points={points}; reason={reason}")
    db.commit()
    return item


def leaderboard(db: Session, event_id: int) -> dict:
    team_answers = (
        select(Answer.team_id.label("id"), func.sum(Answer.points_awarded).label("points"))
        .join(Question).join(Stage).where(Stage.event_id == event_id, Answer.scope == AnswerScope.TEAM)
        .group_by(Answer.team_id).subquery()
    )
    team_manual = (
        select(ScoreAdjustment.team_id.label("id"), func.sum(ScoreAdjustment.points).label("points"))
        .where(ScoreAdjustment.event_id == event_id, ScoreAdjustment.team_id.is_not(None))
        .group_by(ScoreAdjustment.team_id).subquery()
    )
    detective_points = (
        select(DetectiveSubmission.team_id.label("id"), func.sum(DetectiveSubmission.points_awarded).label("points"))
        .join(Stage, Stage.id == DetectiveSubmission.stage_id)
        .where(Stage.event_id == event_id).group_by(DetectiveSubmission.team_id).subquery()
    )
    total = (
        func.coalesce(team_answers.c.points, 0)
        + func.coalesce(team_manual.c.points, 0)
        + func.coalesce(detective_points.c.points, 0)
    )
    teams = db.execute(
        select(Team.id, Team.name, total.label("points"))
        .outerjoin(team_answers, team_answers.c.id == Team.id).outerjoin(team_manual, team_manual.c.id == Team.id)
        .outerjoin(detective_points, detective_points.c.id == Team.id)
        .where(Team.event_id == event_id, Team.active.is_(True)).order_by(total.desc(), Team.name)
    ).mappings().all()
    player_manual = (
        select(ScoreAdjustment.player_id.label("id"), func.sum(ScoreAdjustment.points).label("points"))
        .where(ScoreAdjustment.event_id == event_id, ScoreAdjustment.player_id.is_not(None))
        .group_by(ScoreAdjustment.player_id).subquery()
    )
    ptotal = (
        func.coalesce(func.sum(case((Answer.scope == AnswerScope.PERSONAL, Answer.points_awarded), else_=0)), 0)
        + func.coalesce(player_manual.c.points, 0)
    )
    players = db.execute(
        select(Player.id, Player.full_name, Team.name.label("team_name"), ptotal.label("points"))
        .select_from(Player)
        .join(Team, Team.id == Player.team_id).outerjoin(Answer, Answer.player_id == Player.id)
        .outerjoin(player_manual, player_manual.c.id == Player.id)
        .where(Team.event_id == event_id, Player.active.is_(True))
        .group_by(Player.id, Team.name, player_manual.c.points).order_by(ptotal.desc(), Player.full_name)
    ).mappings().all()
    return {"teams": [dict(x) for x in teams], "players": [dict(x) for x in players]}
