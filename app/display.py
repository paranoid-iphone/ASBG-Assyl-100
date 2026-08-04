from datetime import datetime
from pathlib import Path

from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .detective_runtime import finish_detective_stage, start_detective_stage
from .models import Answer, AnswerScope, CaptainElection, DetectiveSubmission, Event, GameProgram, GameProgramStage, PlayerRole, Question, QuestionStatus, Stage, StageType
from .config import get_settings
from .captain_elections import ELECTION_DURATION_SECONDS, start_captain_election_for_team
from .services import leaderboard, set_question_status
from .telegram_sync import notify_team_chats
from .runtime_state import (
    CUSTOM_SLIDES, SCREEN_HEARTBEATS, navigation_stack, push_persistent_history,
    save_navigation_stack, screen_snapshot,
)

router = APIRouter(tags=["display"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def event_by_token(db: Session, token: str) -> Event:
    event = db.scalar(select(Event).where(Event.display_token == token))
    if not event:
        raise HTTPException(404, "Экран ивента не найден")
    return event


@router.get("/screen/{token}", response_class=HTMLResponse)
def screen(token: str, request: Request, db: Session = Depends(get_db)):
    event_by_token(db, token)
    return templates.TemplateResponse(request, "screen.html", {"token": token})


@router.get("/api/screen/{token}")
def screen_state(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    SCREEN_HEARTBEATS[event.id] = datetime.utcnow()
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else None
    remaining = max(0, event.timer_duration_seconds - int(elapsed)) if elapsed is not None else (
        event.timer_duration_seconds
        if event.display_mode in {"TIMER_READY", "TIMER_PAUSED", "SUBMISSION_READY", "SUBMISSION_PAUSED", "CAPTAIN_ELECTION_READY"}
        else None
    )
    active_teams = [team for team in event.teams if team.active]
    captain_teams = len(active_teams) - len(teams_without_captain(event))
    board = leaderboard(db, event.id)
    anonymous_answers = []
    if question and event.display_mode == "TEAM_ANSWERS":
        submitted = db.scalars(select(Answer).where(
            Answer.question_id == question.id, Answer.scope == AnswerScope.TEAM
        ).order_by(Answer.id)).all()
        anonymous_answers = [
            {"text": answer.text, "explanation": answer.explanation}
            for answer in sorted(submitted, key=lambda item: hash(f"{question.id}:{item.id}"))
        ]
    detective_stage = db.get(Stage, event.current_detective_stage_id) if event.current_detective_stage_id else None
    detective_answered = db.scalar(
        select(__import__("sqlalchemy").func.count(DetectiveSubmission.id))
        .where(DetectiveSubmission.stage_id == detective_stage.id)
    ) if detective_stage else 0
    return {
        "event": event.name,
        "description": event.description,
        "mode": event.display_mode,
        "display_language": event.display_language,
        "question": None if not question else {
            "type": question.question_type.value,
            "options": __import__("json").loads(question.options_json or "[]"),
            "ru": {
                "title": question.title,
                "text": question.text,
                "answer": question.correct_answer,
                "explanation": question.explanation,
            },
            "kk": {
                "title": question.title_kk,
                "text": question.text_kk,
                "answer": question.correct_answer_kk,
                "explanation": question.explanation_kk,
            },
        },
        "timer": {
            "running": elapsed is not None and remaining > 0,
            "paused": event.display_mode in {"TIMER_PAUSED", "SUBMISSION_PAUSED"},
            "ready": event.display_mode in {"TIMER_READY", "SUBMISSION_READY"},
            "phase": "submission" if event.display_mode.startswith("SUBMISSION") else "discussion",
            "remaining": remaining,
        },
        "captain_election": {
            "selected": captain_teams,
            "total": len(active_teams),
            "missing": teams_without_captain(event),
        },
        "timer_sound_enabled": event.timer_sound_enabled,
        "anonymous_answers": anonymous_answers,
        "detective": None if not detective_stage else {
            "title_ru": detective_stage.title,
            "title_kk": detective_stage.title_kk,
            "rules_ru": detective_stage.description or "У каждой команды отдельное дело. Обсудите личные улики и передайте капитану один окончательный ответ.",
            "rules_kk": detective_stage.description_kk or "Әр командаға жеке іс беріледі. Жеке айғақтарды талқылап, капитан арқылы бір соңғы жауап жіберіңіз.",
            "answered": detective_answered,
            "total": len([team for team in event.teams if team.active]),
        },
        "teams": board["teams"][:10],
        "server_time": datetime.utcnow().isoformat(),
        "slide": CUSTOM_SLIDES.get(event.id),
    }


def push_screen_history(event: Event) -> None:
    push_persistent_history(event)


def restore_screen_snapshot(db: Session, event: Event, snapshot: dict) -> None:
    event.display_mode = snapshot["display_mode"]
    event.current_question_id = snapshot["current_question_id"]
    event.current_detective_stage_id = snapshot["current_detective_stage_id"]
    remaining = snapshot.get("timer_remaining")
    event.timer_duration_seconds = remaining if remaining is not None else snapshot["timer_duration_seconds"]
    event.timer_started_at = datetime.utcnow() if remaining is not None else None
    if snapshot.get("slide"):
        CUSTOM_SLIDES[event.id] = snapshot["slide"]
    else:
        CUSTOM_SLIDES.pop(event.id, None)
    if event.current_question_id:
        for other in db.scalars(
            select(Question).join(Stage).where(
                Stage.event_id == event.id,
                Question.status == QuestionStatus.OPEN,
                Question.id != event.current_question_id,
            )
        ).all():
            other.status = QuestionStatus.LOCKED
        question = db.get(Question, event.current_question_id)
        if question:
            question.status = QuestionStatus.OPEN
    db.commit()


def ordered_questions(db: Session, event_id: int) -> list[Question]:
    running_program = db.scalar(select(GameProgram).where(
        GameProgram.event_id == event_id, GameProgram.status == "RUNNING"
    ).order_by(GameProgram.started_at.desc()))
    if running_program:
        return db.scalars(
            select(Question)
            .join(Stage, Question.stage_id == Stage.id)
            .join(GameProgramStage, GameProgramStage.stage_id == Stage.id)
            .where(GameProgramStage.program_id == running_program.id)
            .order_by(GameProgramStage.position, Question.position)
        ).all()
    return db.scalars(
        select(Question).join(Stage).where(Stage.event_id == event_id)
        .order_by(Stage.position, Question.position)
    ).all()


def ordered_program_items(db: Session, event_id: int) -> list[tuple[str, Question | Stage]]:
    running_program = db.scalar(select(GameProgram).where(
        GameProgram.event_id == event_id, GameProgram.status == "RUNNING"
    ).order_by(GameProgram.started_at.desc()))
    if running_program:
        links = db.scalars(
            select(GameProgramStage)
            .where(GameProgramStage.program_id == running_program.id)
            .order_by(GameProgramStage.position)
        ).all()
        items: list[tuple[str, Question | Stage]] = []
        for link in links:
            stage = db.get(Stage, link.stage_id)
            if stage.stage_type == StageType.DETECTIVE:
                items.append(("detective", stage))
            else:
                questions = db.scalars(
                    select(Question).where(Question.stage_id == stage.id).order_by(Question.position)
                ).all()
                items.extend(("question", question) for question in questions)
        return items
    return [("question", question) for question in ordered_questions(db, event_id)]


async def activate_program_item(
    db: Session, event: Event, item: tuple[str, Question | Stage]
) -> dict:
    kind, content = item
    if kind == "detective":
        await start_detective_stage(db, event, content, "screen")
        return {"action": "detective", "stage_id": content.id}
    question = content
    question.stage
    event.current_question_id = question.id
    event.current_detective_stage_id = None
    event.display_mode = "QUESTION"
    event.timer_started_at = None
    event.timer_duration_seconds = question.duration_seconds
    set_question_status(db, question, QuestionStatus.OPEN, "screen")
    await notify_team_chats(db, event, question, "QUESTION")
    return {"action": "question", "question_id": question.id}


def teams_without_captain(event: Event) -> list[str]:
    return [
        team.name for team in event.teams if team.active
        and len([
            player for player in team.players
            if player.active and player.role == PlayerRole.CAPTAIN
        ]) != 1
    ]


@router.post("/api/screen/{token}/advance")
async def advance_screen(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    future = navigation_stack(event, "screen_future_json")
    if future:
        snapshot = future.pop()
        push_persistent_history(event, clear_future=False)
        save_navigation_stack(event, "screen_future_json", future)
        restore_screen_snapshot(db, event, snapshot)
        return {"action": "forward", "mode": event.display_mode}
    if event.display_mode == "SLIDE":
        history = navigation_stack(event, "screen_history_json")
        if history:
            snapshot = history.pop()
            save_navigation_stack(event, "screen_history_json", history)
            restore_screen_snapshot(db, event, snapshot)
        return {"action": "resume"}
    if event.display_mode == "INTRO":
        push_screen_history(event)
        event.display_mode = "CAPTAIN_ELECTION_READY"
        event.timer_duration_seconds = ELECTION_DURATION_SECONDS
        event.timer_started_at = None
        db.commit()
        return {"action": "captain_election_ready"}
    if event.display_mode in {"CAPTAIN_ELECTION_READY", "CAPTAIN_ELECTION_RUNNING"}:
        missing = teams_without_captain(event)
        if missing:
            raise HTTPException(409, "Сначала завершите выбор капитанов: " + ", ".join(missing))
        push_screen_history(event)
        event.display_mode = "CAPTAIN_ELECTION_COMPLETE"
        event.timer_started_at = None
        db.commit()
        return {"action": "captain_election_complete"}
    if event.display_mode == "CAPTAIN_ELECTION_COMPLETE":
        push_screen_history(event)
        event.display_mode = "RULES"
        db.commit()
        return {"action": "rules"}
    if event.display_mode == "RULES":
        items = ordered_program_items(db, event.id)
        if not items:
            raise HTTPException(409, "В игре нет вопросов или детективного этапа")
        push_screen_history(event)
        return await activate_program_item(db, event, items[0])
    if event.display_mode == "RESERVE_READY":
        items = ordered_program_items(db, event.id)
        reserve_item = next((item for item in items if (
            item[0] == "question" and item[1].stage.system_key == "reserve"
        )), None)
        if not reserve_item:
            raise HTTPException(409, "В резервном раунде пока нет вопросов")
        push_screen_history(event)
        return await activate_program_item(db, event, reserve_item)
    if event.display_mode == "DETECTIVE":
        stage = db.get(Stage, event.current_detective_stage_id) if event.current_detective_stage_id else None
        if not stage:
            raise HTTPException(409, "Активный детективный этап не найден")
        elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else 0
        answered = db.scalar(select(__import__("sqlalchemy").func.count(DetectiveSubmission.id)).where(
            DetectiveSubmission.stage_id == stage.id
        )) or 0
        active_teams = len([team for team in event.teams if team.active])
        if elapsed < event.timer_duration_seconds and answered < active_teams:
            return {"action": "detective_running", "answered": answered, "total": active_teams}
        push_screen_history(event)
        completed_stage_id = stage.id
        finish_detective_stage(db, event, stage, "screen")
        return await next_question(token, db, "detective", completed_stage_id)
    items = ordered_program_items(db, event.id)
    current = db.get(Question, event.current_question_id) if event.current_question_id else None

    if not current:
        missing_captains = teams_without_captain(event)
        if missing_captains:
            raise HTTPException(
                409,
                "Сначала выберите капитанов команд: " + ", ".join(missing_captains),
            )
        if not items:
            raise HTTPException(409, "В ивенте нет вопросов")
        push_screen_history(event)
        return await activate_program_item(db, event, items[0])

    if event.display_mode == "QUESTION":
        push_screen_history(event)
        event.timer_duration_seconds = current.duration_seconds
        event.timer_started_at = None
        event.display_mode = "TIMER_READY"
        db.commit()
        return {"action": "discussion_ready", "question_id": current.id}

    if event.display_mode == "TIMER":
        elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else 0
        if elapsed < event.timer_duration_seconds:
            raise HTTPException(409, "Сначала завершите таймер обсуждения")
        push_screen_history(event)
        event.timer_duration_seconds = current.submission_seconds
        event.timer_started_at = None
        event.display_mode = "SUBMISSION_READY"
        db.commit()
        return {"action": "submission_ready", "question_id": current.id}

    if event.display_mode in {"TIMER_READY", "TIMER_PAUSED"} and event.timer_duration_seconds <= 0:
        push_screen_history(event)
        event.timer_duration_seconds = current.submission_seconds
        event.timer_started_at = None
        event.display_mode = "SUBMISSION_READY"
        db.commit()
        return {"action": "submission_ready", "question_id": current.id}

    if event.display_mode in {"SUBMISSION_READY", "SUBMISSION_PAUSED"} and event.timer_duration_seconds <= 0:
        push_screen_history(event)
        event.display_mode = "ANSWER"
        event.timer_started_at = None
        current.status = QuestionStatus.LOCKED
        db.commit()
        await notify_team_chats(db, event, current, "ANSWER")
        return {"action": "answer", "question_id": current.id}

    if event.display_mode in {"TIMER_READY", "TIMER_PAUSED", "SUBMISSION_READY", "SUBMISSION_PAUSED"}:
        phase = "приёма ответа" if event.display_mode.startswith("SUBMISSION") else "обсуждения"
        raise HTTPException(409, f"Таймер {phase} ещё не запущен. Нажмите «Старт» или уменьшите время до нуля")

    if event.display_mode == "SUBMISSION":
        elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else 0
        if elapsed < event.timer_duration_seconds:
            raise HTTPException(409, "Сначала завершите таймер приёма ответов")
        push_screen_history(event)
        event.display_mode = "ANSWER"
        event.timer_started_at = None
        current.status = QuestionStatus.LOCKED
        db.commit()
        await notify_team_chats(db, event, current, "ANSWER")
        return {"action": "answer", "question_id": current.id}

    if event.display_mode == "ANSWER" and current.show_anonymous_answers:
        push_screen_history(event)
        event.display_mode = "TEAM_ANSWERS"
        db.commit()
        return {"action": "team_answers", "question_id": current.id}

    if event.display_mode in {"ANSWER", "TEAM_ANSWERS"}:
        push_screen_history(event)
        return await next_question(token, db)

    event.display_mode = "QUESTION"
    event.timer_started_at = None
    db.commit()
    return {"action": "question", "question_id": current.id}


@router.post("/api/screen/{token}/back")
def previous_screen(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    history = navigation_stack(event, "screen_history_json")
    if not history:
        items = ordered_program_items(db, event.id)
        current_index = next((
            index for index, (kind, content) in enumerate(items)
            if kind == "question" and content.id == event.current_question_id
        ), -1)
        if current_index < 0:
            raise HTTPException(409, "Предыдущего слайда нет")
        history = [
            {"display_mode": "INTRO", "current_question_id": None, "current_detective_stage_id": None, "timer_duration_seconds": 60, "timer_remaining": None, "slide": {}},
            {"display_mode": "CAPTAIN_ELECTION_COMPLETE", "current_question_id": None, "current_detective_stage_id": None, "timer_duration_seconds": 60, "timer_remaining": None, "slide": {}},
            {"display_mode": "RULES", "current_question_id": None, "current_detective_stage_id": None, "timer_duration_seconds": 60, "timer_remaining": None, "slide": {}},
        ]
        for kind, content in items[:current_index]:
            if kind == "detective":
                history.append({"display_mode": "DETECTIVE", "current_question_id": None, "current_detective_stage_id": content.id, "timer_duration_seconds": content.detective_duration_seconds, "timer_remaining": 0, "slide": {}})
                continue
            modes = [
                ("QUESTION", content.duration_seconds),
                ("TIMER_READY", content.duration_seconds),
                ("SUBMISSION_READY", content.submission_seconds),
                ("ANSWER", content.submission_seconds),
            ]
            if content.show_anonymous_answers:
                modes.append(("TEAM_ANSWERS", content.submission_seconds))
            history.extend({
                "display_mode": mode,
                "current_question_id": content.id,
                "current_detective_stage_id": None,
                "timer_duration_seconds": duration,
                "timer_remaining": duration if mode.endswith("READY") else None,
                "slide": {},
            } for mode, duration in modes)
        save_navigation_stack(event, "screen_history_json", history)
    snapshot = history.pop()
    future = navigation_stack(event, "screen_future_json")
    future.append(screen_snapshot(event))
    save_navigation_stack(event, "screen_history_json", history)
    save_navigation_stack(event, "screen_future_json", future)
    restore_screen_snapshot(db, event, snapshot)
    return {"action": "back", "mode": event.display_mode}


@router.post("/api/screen/{token}/timer-adjust")
def adjust_screen_timer(token: str, seconds: int, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    if event.display_mode not in {"TIMER_READY", "TIMER", "TIMER_PAUSED", "SUBMISSION_READY", "SUBMISSION", "SUBMISSION_PAUSED", "DETECTIVE"}:
        raise HTTPException(409, "На текущем слайде нет таймера")
    event.timer_duration_seconds = max(0, min(7200, event.timer_duration_seconds + max(-300, min(seconds, 1800))))
    db.commit()
    return {"action": "timer_adjust", "seconds": seconds}


@router.post("/api/screen/{token}/timer/start")
async def start_screen_timer(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    if event.display_mode == "CAPTAIN_ELECTION_READY":
        missing_teams = [team for team in event.teams if team.active and team.name in teams_without_captain(event)]
        if not missing_teams:
            event.display_mode = "CAPTAIN_ELECTION_COMPLETE"
            event.timer_started_at = None
            db.commit()
            return {"action": "captain_election_complete", "mode": event.display_mode}
        token_value = get_settings().telegram_bot_token
        if not token_value:
            raise HTTPException(409, "Telegram-бот не настроен")
        bot = Bot(token_value)
        try:
            for team in missing_teams:
                active = db.scalar(select(CaptainElection).where(
                    CaptainElection.team_id == team.id, CaptainElection.active.is_(True)
                ))
                if not active:
                    try:
                        await start_captain_election_for_team(db, team, bot, "screen")
                    except ValueError as exc:
                        raise HTTPException(409, str(exc))
        finally:
            await bot.session.close()
        event.display_mode = "CAPTAIN_ELECTION_RUNNING"
        event.timer_duration_seconds = ELECTION_DURATION_SECONDS
        event.timer_started_at = datetime.utcnow()
        db.commit()
        return {"action": "captain_election_start", "mode": event.display_mode}
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    transitions = {
        "TIMER_READY": "TIMER",
        "TIMER_PAUSED": "TIMER",
        "SUBMISSION_READY": "SUBMISSION",
        "SUBMISSION_PAUSED": "SUBMISSION",
    }
    next_mode = transitions.get(event.display_mode)
    if not next_mode:
        raise HTTPException(409, "Таймер уже запущен или недоступен")
    event.display_mode = next_mode
    event.timer_started_at = datetime.utcnow()
    db.commit()
    if next_mode == "SUBMISSION" and question:
        await notify_team_chats(db, event, question, "SUBMISSION")
    return {"action": "timer_start", "mode": next_mode}


@router.post("/api/screen/{token}/timer/pause")
def pause_screen_timer(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    if event.display_mode not in {"TIMER", "SUBMISSION"} or not event.timer_started_at:
        raise HTTPException(409, "Запущенного таймера нет")
    elapsed = int((datetime.utcnow() - event.timer_started_at).total_seconds())
    event.timer_duration_seconds = max(0, event.timer_duration_seconds - elapsed)
    event.timer_started_at = None
    event.display_mode = "TIMER_PAUSED" if event.display_mode == "TIMER" else "SUBMISSION_PAUSED"
    db.commit()
    return {"action": "timer_pause", "remaining": event.timer_duration_seconds}


@router.post("/api/screen/{token}/timer/reset")
def reset_screen_timer(token: str, db: Session = Depends(get_db)):
    event = event_by_token(db, token)
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    if not question:
        raise HTTPException(409, "Активного вопроса нет")
    if event.display_mode.startswith("TIMER"):
        event.display_mode = "TIMER_READY"
        event.timer_duration_seconds = question.duration_seconds
    elif event.display_mode.startswith("SUBMISSION"):
        event.display_mode = "SUBMISSION_READY"
        event.timer_duration_seconds = question.submission_seconds
    else:
        raise HTTPException(409, "На текущем слайде нет таймера")
    event.timer_started_at = None
    db.commit()
    return {"action": "timer_reset", "remaining": event.timer_duration_seconds}


@router.post("/api/screen/{token}/next")
async def next_question(
    token: str,
    db: Session = Depends(get_db),
    completed_kind: str | None = None,
    completed_id: int | None = None,
):
    event = event_by_token(db, token)
    items = ordered_program_items(db, event.id)
    if completed_kind is None:
        completed_kind = "question"
        completed_id = event.current_question_id
    current_index = next(
        (i for i, (kind, content) in enumerate(items) if kind == completed_kind and content.id == completed_id),
        -1,
    )
    if current_index + 1 >= len(items):
        running_program = db.scalar(select(GameProgram).where(
            GameProgram.event_id == event.id, GameProgram.status == "RUNNING"
        ).order_by(GameProgram.started_at.desc()))
        if running_program:
            running_program.status = "FINISHED"
            running_program.finished_at = datetime.utcnow()
        event.display_mode = "WELCOME"
        event.current_question_id = None
        event.timer_started_at = None
        db.commit()
        return {"action": "finished"}
    next_item = items[current_index + 1]
    completed_stage_key = None
    if completed_kind == "question" and completed_id:
        completed_question = db.get(Question, completed_id)
        completed_stage_key = completed_question.stage.system_key if completed_question else None
    if (
        next_item[0] == "question"
        and next_item[1].stage.system_key == "reserve"
        and completed_stage_key != "reserve"
    ):
        event.display_mode = "RESERVE_READY"
        event.current_question_id = None
        event.current_detective_stage_id = None
        event.timer_started_at = None
        db.commit()
        return {"action": "reserve_ready", "stage_id": next_item[1].stage_id}
    return await activate_program_item(db, event, next_item)
