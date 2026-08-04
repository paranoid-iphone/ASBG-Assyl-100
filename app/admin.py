import secrets
import json
import re
from datetime import date, datetime
from pathlib import Path
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .database import get_db
from .detective import generate_cases_for_stage
from .detective_runtime import prepared_cases, send_detective_clue, start_detective_stage
from .models import (
    Answer, AnswerScope, AuditLog, CaptainElection, CaptainVote, DetectiveCase, DetectiveClue, DetectiveSubmission,
    DetectiveStatus, Event, GameProgram, GameProgramStage, PendingRegistration, Player, PlayerRole, Question, QuestionStatus,
    QuestionType, ScoreAdjustment, Stage, StageType, Team, TeamQuestionPrompt,
)
from .services import adjust_score, audit, grade_all_answers, grade_answer, leaderboard, set_question_status, submit_detective_answer
from .runtime_state import CLUE_DELIVERY, CUSTOM_SLIDES, SCREEN_HEARTBEATS, SCREEN_HISTORY, TEAM_DELIVERY, TEMPORARY_SENDERS, mark_team_delivery
from .public_url import public_base_url
from .seed_game_content import seed_game_content_for_event
from .fixed_program import ensure_fixed_program

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()


def telegram_html(text: str) -> str:
    """Keep composer tags while making ordinary ampersands and angle brackets safe."""
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)
    return re.sub(r"<(?!/?(?:b|i|u|s|code|a)(?:\s|>))", "&lt;", text)


def admin_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    cfg = get_settings()
    valid = secrets.compare_digest(credentials.username, cfg.admin_username) and secrets.compare_digest(
        credentials.password, cfg.admin_password
    )
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль", {"WWW-Authenticate": "Basic"})
    return credentials.username


def go(event_id: int | None = None, tab: str = "overview"):
    url = "/admin" if event_id is None else f"/admin/events/{event_id}?tab={tab}"
    return RedirectResponse(url, 303)


def require_event(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Ивент не найден")
    return event


def require_roster_unlocked(db: Session, event_id: int) -> None:
    running_program = db.scalar(select(GameProgram.id).where(
        GameProgram.event_id == event_id,
        GameProgram.status == "RUNNING",
    ))
    if running_program:
        raise HTTPException(
            409,
            "Составы заблокированы на время игры. Используйте временного отправителя на пульте проведения.",
        )


def teams_without_captain(event: Event) -> list[Team]:
    """Active teams must have exactly one active captain."""
    return [
        team for team in event.teams if team.active
        and len([
            player for player in team.players
            if player.active and player.role == PlayerRole.CAPTAIN
        ]) != 1
    ]


async def require_captains_before_game(event: Event, db: Session, actor: str) -> bool:
    """Start missing captain elections; return True only when play may begin."""
    missing = teams_without_captain(event)
    if not missing:
        return True
    if not get_settings().telegram_bot_token:
        raise HTTPException(409, "Нельзя начать игру: Telegram-бот не настроен")

    problems: list[str] = []
    for team in missing:
        candidates = [p for p in team.players if p.active and p.telegram_user_id]
        if not team.telegram_chat_id:
            problems.append(f"{team.name}: не подключена Telegram-беседа")
        elif len(candidates) < 2:
            problems.append(f"{team.name}: нужны минимум два зарегистрированных участника")
    if problems:
        raise HTTPException(409, "Сначала выберите капитанов. " + "; ".join(problems))

    started = 0
    for team in missing:
        active_election = db.scalar(select(CaptainElection).where(
            CaptainElection.team_id == team.id,
            CaptainElection.active.is_(True),
        ))
        if not active_election:
            await start_captain_election(event.id, team.id, db, actor)
            started += 1
    audit(db, actor, "game.captain_preflight", event, f"teams={len(missing)}; started={started}")
    db.commit()
    return False


CYRILLIC_TO_LATIN = {
    **dict(zip("абвгдежзийклмнопрстуфхцчшщъыьэюя", [
        "a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m",
        "n", "o", "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh",
        "sch", "", "y", "", "e", "yu", "ya",
    ])),
    "ё": "e", "ә": "a", "ғ": "g", "қ": "q", "ң": "n",
    "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}


def team_slug(name: str) -> str:
    transliterated = "".join(CYRILLIC_TO_LATIN.get(char, char) for char in name.casefold())
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-").upper()
    return slug[:32] or "TEAM"


def unique_team_code(db: Session, event_id: int, requested: str, name: str) -> str:
    base = team_slug(requested) if requested.strip() else team_slug(name)
    candidate, suffix = base, 2
    existing = set(db.scalars(select(Team.code).where(Team.event_id == event_id)).all())
    while candidate in existing:
        tail = f"-{suffix}"
        candidate = f"{base[:40-len(tail)]}{tail}"
        suffix += 1
    return candidate


def clear_detective_cases_for_teams(db: Session, team_ids: list[int]) -> None:
    """Invalidate generated cases after a team roster changes."""
    case_ids = list(db.scalars(select(DetectiveCase.id).where(DetectiveCase.team_id.in_(team_ids))).all())
    if not case_ids:
        return
    db.execute(delete(DetectiveSubmission).where(DetectiveSubmission.case_id.in_(case_ids)))
    db.execute(delete(DetectiveClue).where(DetectiveClue.case_id.in_(case_ids)))
    db.execute(delete(DetectiveCase).where(DetectiveCase.id.in_(case_ids)))


def return_player_to_registration_pool(db: Session, player: Player, event_id: int) -> bool:
    """Return a Telegram-registered participant to the unassigned pool."""
    if not player.telegram_user_id:
        return False
    existing = db.scalar(select(PendingRegistration).where(
        PendingRegistration.telegram_user_id == player.telegram_user_id
    ))
    if not existing:
        db.add(PendingRegistration(
            event_id=event_id,
            full_name=player.full_name,
            telegram_user_id=player.telegram_user_id,
            telegram_username=player.telegram_username,
            preferred_language=player.preferred_language,
            created_at=player.registered_at or datetime.utcnow(),
        ))
    return True


def delete_player_dependencies(db: Session, player: Player) -> None:
    db.execute(delete(CaptainVote).where(
        (CaptainVote.voter_player_id == player.id) | (CaptainVote.candidate_player_id == player.id)
    ))
    db.execute(delete(DetectiveSubmission).where(DetectiveSubmission.captain_id == player.id))
    db.execute(delete(DetectiveClue).where(DetectiveClue.player_id == player.id))
    db.execute(delete(Answer).where(
        (Answer.player_id == player.id) | (Answer.respondent_player_id == player.id)
    ))
    db.execute(delete(ScoreAdjustment).where(ScoreAdjustment.player_id == player.id))


@router.get("", response_class=HTMLResponse)
def event_list(request: Request, db: Session = Depends(get_db), actor: str = Depends(admin_auth)):
    primary_event = db.scalar(select(Event).order_by(Event.id))
    if primary_event:
        return RedirectResponse(f"/admin/events/{primary_event.id}", 303)
    events = []
    stats = {}
    for event in events:
        stats[event.id] = {
            "teams": db.scalar(select(func.count(Team.id)).where(Team.event_id == event.id)) or 0,
            "players": db.scalar(select(func.count(Player.id)).join(Team).where(Team.event_id == event.id)) or 0,
        }
    return templates.TemplateResponse(request, "events.html", {"events": events, "stats": stats})


@router.post("/events")
def create_event(
    name: str = Form(...), description: str = Form(""), event_date: date | None = Form(None),
    db: Session = Depends(get_db), actor: str = Depends(admin_auth),
):
    existing = db.scalar(select(Event).order_by(Event.id))
    if existing:
        return go(existing.id)
    event = Event(
        name=name, description=description, event_date=event_date,
        registration_code=f"EVENT-{secrets.token_hex(6).upper()}",
        display_token=secrets.token_urlsafe(10),
    )
    db.add(event); db.flush(); audit(db, actor, "event.create", event, name); db.commit()
    return go(event.id)


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_dashboard(
    event_id: int, request: Request, tab: str = "overview",
    db: Session = Depends(get_db), actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    ensure_fixed_program(db, event)
    db.commit()
    teams = db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.name)).all()
    players = db.scalars(select(Player).join(Team).where(Team.event_id == event.id).order_by(Team.name, Player.full_name)).all()
    pending_registrations = db.scalars(
        select(PendingRegistration).where(PendingRegistration.event_id == event.id)
        .order_by(PendingRegistration.created_at)
    ).all()
    stages = db.scalars(
        select(Stage).options(
            selectinload(Stage.questions),
            selectinload(Stage.detective_cases).selectinload(DetectiveCase.clues),
            selectinload(Stage.detective_cases).selectinload(DetectiveCase.submission),
        ).where(Stage.event_id == event.id).order_by(Stage.position)
    ).all()
    programs = db.scalars(
        select(GameProgram).options(
            selectinload(GameProgram.stage_links).selectinload(GameProgramStage.stage).selectinload(Stage.questions)
        ).where(GameProgram.event_id == event.id).order_by(GameProgram.created_at.desc())
    ).all()
    active_program = next((program for program in programs if program.status == "RUNNING"), None)
    answers = db.scalars(
        select(Answer).join(Question).join(Stage).where(Stage.event_id == event.id).order_by(Answer.id.desc()).limit(150)
    ).all()
    logs = db.scalars(select(AuditLog).where(AuditLog.event_id == event.id).order_by(AuditLog.id.desc()).limit(150)).all()
    active_question = db.get(Question, event.current_question_id) if event.current_question_id else None
    base_url, public_url_source = public_base_url(str(request.base_url))
    public_screen_url = f"{base_url}/screen/{event.display_token}"
    template_name = "content_page.html" if tab == "content" else "event_dashboard.html"
    return templates.TemplateResponse(request, template_name, {
        "tab": tab, "event": event, "teams": teams, "players": players,
        "pending_registrations": pending_registrations, "stages": stages, "programs": programs,
        "active_program": active_program,
        "answers": answers, "logs": logs, "active_question": active_question,
        "board": leaderboard(db, event.id), "roles": PlayerRole,
        "stage_types": StageType, "detective_statuses": DetectiveStatus,
        "question_types": QuestionType,
        "public_screen_url": public_screen_url,
        "public_url_source": public_url_source,
        "question_options": {
            question.id: "\n".join(json.loads(question.options_json or "[]"))
            for stage in stages for question in stage.questions
        },
        "program_question_counts": {
            program.id: sum(len(link.stage.questions) for link in program.stage_links)
            for program in programs
        },
    })


@router.get("/events/{event_id}/live/state")
def live_control_state(
    event_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    program = db.scalar(select(GameProgram).options(
        selectinload(GameProgram.stage_links)
        .selectinload(GameProgramStage.stage)
        .selectinload(Stage.questions)
    ).where(
        GameProgram.event_id == event.id,
        GameProgram.status == "RUNNING",
    ).order_by(GameProgram.started_at.desc()))
    items: list[tuple[str, Question | Stage, str]] = []
    if program:
        for link in program.stage_links:
            stage = link.stage
            if stage.stage_type == StageType.DETECTIVE:
                items.append(("detective", stage, stage.title))
            else:
                for question in stage.questions:
                    items.append(("question", question, stage.title))

    current_index = -1
    for index, (kind, item, _) in enumerate(items):
        if kind == "question" and item.id == event.current_question_id:
            current_index = index
        elif kind == "detective" and item.id == event.current_detective_stage_id:
            current_index = index
    timeline = []
    for index, (kind, item, stage_title) in enumerate(items):
        state = "current" if index == current_index else ("done" if current_index >= 0 and index < current_index else "upcoming")
        timeline.append({
            "kind": kind,
            "id": item.id,
            "stage": stage_title,
            "title": item.title,
            "state": state,
        })

    elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else None
    remaining = max(0, event.timer_duration_seconds - int(elapsed)) if elapsed is not None else None
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    team_answers = {}
    if question:
        team_answers = {
            answer.team_id: answer for answer in db.scalars(select(Answer).where(
                Answer.question_id == question.id,
                Answer.scope == AnswerScope.TEAM,
            )).all()
        }
    detective_stage = db.get(Stage, event.current_detective_stage_id) if event.current_detective_stage_id else None
    detective_submissions = {}
    if detective_stage:
        detective_submissions = {
            submission.team_id: submission for submission in db.scalars(select(DetectiveSubmission).where(
                DetectiveSubmission.stage_id == detective_stage.id
            )).all()
        }
    teams = []
    for team in db.scalars(select(Team).where(
        Team.event_id == event.id, Team.active.is_(True)
    ).order_by(Team.name)).all():
        captain = next((p for p in team.players if p.active and p.role == PlayerRole.CAPTAIN), None)
        answer = team_answers.get(team.id)
        submission = detective_submissions.get(team.id)
        temporary_sender_id = TEMPORARY_SENDERS.get((question.id, team.id)) if question else None
        temporary_sender = db.get(Player, temporary_sender_id) if temporary_sender_id else None
        delivery = TEAM_DELIVERY.get((question.id, team.id), {}) if question else {}
        clue_rows = []
        detective_options = []
        if detective_stage:
            detective_case = db.scalar(select(DetectiveCase).where(
                DetectiveCase.stage_id == detective_stage.id,
                DetectiveCase.team_id == team.id,
            ))
            if detective_case:
                detective_options = json.loads(detective_case.options_json or "[]")
                for clue in detective_case.clues:
                    clue_status = CLUE_DELIVERY.get((detective_stage.id, clue.player_id), {})
                    clue_rows.append({
                        "id": clue.id,
                        "player": clue.player.full_name,
                        "status": clue_status.get("status", "unknown"),
                        "error": clue_status.get("error", ""),
                    })
        teams.append({
            "id": team.id,
            "name": team.name,
            "captain": captain.full_name if captain else None,
            "temporary_sender": temporary_sender.full_name if temporary_sender else None,
            "players": [{"id": p.id, "name": p.full_name} for p in team.players if p.active],
            "respondent": answer.respondent.full_name if answer and answer.respondent else None,
            "answer_id": answer.id if answer else None,
            "answer": answer.text if answer else None,
            "graded": answer.is_correct if answer else None,
            "points": answer.points_awarded if answer else None,
            "detective_answer": submission.selected_option if submission else None,
            "detective_correct": submission.is_correct if submission else None,
            "detective_points": submission.points_awarded if submission else None,
            "delivery": {
                "status": "disconnected" if not team.telegram_chat_id else delivery.get("status", "unknown"),
                "error": delivery.get("error", ""),
            },
            "clues": clue_rows,
            "detective_options": detective_options,
        })

    next_labels = {
        "QUESTION": "Запустить таймер обсуждения",
        "TIMER": "Открыть приём ответов",
        "SUBMISSION": "Закрыть ответы и показать правильный ответ",
        "ANSWER": "Показать ответы команд" if question and question.show_anonymous_answers else "Перейти дальше",
        "TEAM_ANSWERS": "Следующий вопрос или этап",
        "DETECTIVE": "Завершить детектив и перейти дальше",
        "WELCOME": "Показать первый вопрос",
        "SLIDE": "Вернуться к игре",
    }
    detective_answered = len(detective_submissions)
    detective_total = len(teams)
    detective_can_finish = not detective_stage or remaining == 0 or detective_answered >= detective_total
    heartbeat = SCREEN_HEARTBEATS.get(event.id)
    heartbeat_age = int((datetime.utcnow() - heartbeat).total_seconds()) if heartbeat else None
    return {
        "program": None if not program else {"id": program.id, "title": program.title},
        "mode": event.display_mode,
        "mode_label": {
            "WELCOME": "Заставка", "QUESTION": "Вопрос показан", "TIMER": "Обсуждение",
            "SUBMISSION": "Приём ответов", "ANSWER": "Правильный ответ",
            "TEAM_ANSWERS": "Ответы команд", "DETECTIVE": "Детективная игра",
            "PAUSED": "Пауза",
            "SLIDE": "Служебный слайд",
        }.get(event.display_mode, event.display_mode),
        "question": None if not question else {
            "id": question.id, "title": question.title, "text": question.text,
            "answer": question.correct_answer, "stage": question.stage.title,
        },
        "detective": None if not detective_stage else {
            "id": detective_stage.id, "title": detective_stage.title,
            "answered": detective_answered, "total": detective_total,
        },
        "timer": {"remaining": remaining, "running": remaining is not None and remaining > 0},
        "timeline": timeline,
        "current_index": current_index,
        "teams": teams,
        "next_label": next_labels.get(event.display_mode, "Продолжить"),
        "can_advance": bool(program or question) and detective_can_finish,
        "screen": {
            "status": "offline" if heartbeat_age is None or heartbeat_age > 30 else ("stale" if heartbeat_age > 10 else "online"),
            "age": heartbeat_age,
        },
    }


@router.post("/events/{event_id}/live/add-time")
def add_live_time(
    event_id: int,
    seconds: int = Form(60),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    if not event.timer_started_at:
        raise HTTPException(409, "Сейчас таймер не запущен")
    seconds = max(-300, min(seconds, 1800))
    event.timer_duration_seconds = max(0, event.timer_duration_seconds + seconds)
    audit(db, actor, "live.timer_adjust", event, f"seconds={seconds}")
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/live/slide")
def show_service_slide(
    event_id: int,
    title: str = Form(...), text: str = Form(""),
    title_kk: str = Form(""), text_kk: str = Form(""),
    db: Session = Depends(get_db), actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else None
    remaining = max(0, event.timer_duration_seconds - int(elapsed)) if elapsed is not None else None
    history = SCREEN_HISTORY.setdefault(event.id, [])
    history.append({
        "display_mode": event.display_mode,
        "current_question_id": event.current_question_id,
        "current_detective_stage_id": event.current_detective_stage_id,
        "timer_duration_seconds": event.timer_duration_seconds,
        "timer_remaining": remaining,
        "slide": dict(CUSTOM_SLIDES.get(event.id) or {}),
    })
    del history[:-30]
    CUSTOM_SLIDES[event.id] = {
        "title": title.strip(), "text": text.strip(),
        "title_kk": title_kk.strip(), "text_kk": text_kk.strip(),
    }
    event.display_mode = "SLIDE"
    event.timer_started_at = None
    audit(db, actor, "live.slide", event, title.strip())
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/live/teams/{team_id}/temporary-sender")
def set_temporary_sender(
    event_id: int,
    team_id: int,
    player_id: int = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    team = db.get(Team, team_id)
    player = db.get(Player, player_id)
    if not question:
        raise HTTPException(409, "Сейчас нет активного вопроса")
    if not team or team.event_id != event.id or not player or player.team_id != team.id or not player.active:
        raise HTTPException(400, "Выбранный участник недоступен")
    TEMPORARY_SENDERS[(question.id, team.id)] = player.id
    audit(db, actor, "live.temporary_sender", team, f"question={question.id}; player={player.id}")
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/live/teams/{team_id}/manual-answer")
def create_manual_team_answer(
    event_id: int,
    team_id: int,
    text: str = Form(...),
    respondent_player_id: int | None = Form(None),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    team = db.get(Team, team_id)
    if not question or not team or team.event_id != event.id:
        raise HTTPException(409, "Нет активного вопроса или команда недоступна")
    existing = db.scalar(select(Answer).where(
        Answer.question_id == question.id,
        Answer.scope == AnswerScope.TEAM,
        Answer.team_id == team.id,
    ))
    if existing:
        raise HTTPException(409, "Ответ этой команды уже зафиксирован")
    respondent = db.get(Player, respondent_player_id) if respondent_player_id else None
    if respondent_player_id and (not respondent or respondent.team_id != team.id or not respondent.active):
        raise HTTPException(400, "Выбранный отвечающий недоступен")
    answer = Answer(
        question_id=question.id,
        scope=AnswerScope.TEAM,
        team_id=team.id,
        text=text.strip(),
        respondent_player_id=respondent_player_id,
    )
    if not answer.text:
        raise HTTPException(400, "Ответ не может быть пустым")
    db.add(answer)
    audit(db, actor, "answer.manual_submit", question, f"team={team.id}")
    db.commit()
    return {"ok": True, "answer_id": answer.id}


@router.post("/events/{event_id}/live/teams/{team_id}/manual-detective-answer")
def create_manual_detective_answer(
    event_id: int,
    team_id: int,
    option: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    team = db.get(Team, team_id)
    if not event.current_detective_stage_id or not team or team.event_id != event.id:
        raise HTTPException(409, "Детективная игра сейчас не проводится")
    captain = next((p for p in team.players if p.active and p.role == PlayerRole.CAPTAIN), None)
    if not captain:
        raise HTTPException(409, "У команды не выбран капитан")
    try:
        submission = submit_detective_answer(db, captain, option)
    except Exception as exc:
        raise HTTPException(409, str(exc))
    audit(db, actor, "detective.manual_submit", submission.case, f"team={team.id}")
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/live/teams/{team_id}/resend-answer-form")
async def resend_team_answer_form(
    event_id: int,
    team_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    team = db.get(Team, team_id)
    if not question or event.display_mode != "SUBMISSION":
        raise HTTPException(409, "Форму ответа можно отправить только во время приёма ответов")
    if not team or team.event_id != event.id or not team.telegram_chat_id:
        raise HTTPException(409, "У команды не подключена Telegram-беседа")
    from .telegram_sync import notify_team_chats
    delivered, failed = await notify_team_chats(db, event, question, "SUBMISSION", team.id)
    audit(db, actor, "live.answer_form_resend", team, f"delivered={delivered}; failed={failed}")
    db.commit()
    if failed or not delivered:
        raise HTTPException(502, "Telegram не принял сообщение. Проверьте беседу и права бота")
    return {"ok": True}


@router.post("/events/{event_id}/live/teams/{team_id}/send-answer-form-private")
async def send_team_answer_form_private(
    event_id: int,
    team_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    question = db.get(Question, event.current_question_id) if event.current_question_id else None
    team = db.get(Team, team_id)
    if not question or event.display_mode != "SUBMISSION" or not team or team.event_id != event.id:
        raise HTTPException(409, "Личную форму можно отправить только во время приёма ответов")
    temporary_id = TEMPORARY_SENDERS.get((question.id, team.id))
    target = db.get(Player, temporary_id) if temporary_id else next(
        (p for p in team.players if p.active and p.role == PlayerRole.CAPTAIN), None
    )
    if not target or not target.telegram_user_id:
        raise HTTPException(409, "У капитана или временного отправителя не подключён Telegram")
    if not get_settings().telegram_bot_token:
        raise HTTPException(409, "Telegram-бот не настроен")
    teammates = [p for p in team.players if p.active]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=player.full_name,
            callback_data=f"respondent:{question.id}:{player.id}",
        )]
        for player in teammates
    ])
    bot = Bot(get_settings().telegram_bot_token)
    try:
        await bot.send_message(
            target.telegram_user_id,
            f"⏱ Открыт приём ответа. Осталось {question.submission_seconds} секунд.\n\n"
            "Выберите участника, который будет отвечать от команды:",
            reply_markup=keyboard,
        )
        mark_team_delivery(question.id, team.id, "private")
    except Exception as exc:
        mark_team_delivery(question.id, team.id, "failed", str(exc))
        raise HTTPException(502, "Telegram не принял личное сообщение")
    finally:
        await bot.session.close()
    audit(db, actor, "live.answer_form_private", team, f"player={target.id}")
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/live/clues/{clue_id}/resend")
async def resend_detective_clue(
    event_id: int,
    clue_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    clue = db.get(DetectiveClue, clue_id)
    if not clue or clue.case.stage.event_id != event.id:
        raise HTTPException(404, "Улика не найдена")
    if not get_settings().telegram_bot_token:
        raise HTTPException(409, "Telegram-бот не настроен")
    bot = Bot(get_settings().telegram_bot_token)
    try:
        delivered = await send_detective_clue(bot, clue.case.stage_id, clue)
    finally:
        await bot.session.close()
    audit(db, actor, "detective.clue_resend", clue.case, f"clue={clue.id}; delivered={delivered}")
    db.commit()
    if not delivered:
        raise HTTPException(502, "Не удалось отправить улику участнику")
    return {"ok": True}


@router.post("/events/{event_id}/live/finish")
def finish_live_program(
    event_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    program = db.scalar(select(GameProgram).where(
        GameProgram.event_id == event.id,
        GameProgram.status == "RUNNING",
    ))
    if program:
        program.status = "FINISHED"
        program.finished_at = datetime.utcnow()
    if event.current_detective_stage_id:
        stage = db.get(Stage, event.current_detective_stage_id)
        if stage:
            stage.detective_status = DetectiveStatus.FINISHED
    event.display_mode = "WELCOME"
    event.current_question_id = None
    event.current_detective_stage_id = None
    event.timer_started_at = None
    audit(db, actor, "live.finish", event, f"program={program.id if program else 'none'}")
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/teams")
def create_team(event_id: int, name: str = Form(...), code: str = Form(""), capacity: int = Form(10), db: Session = Depends(get_db), actor=Depends(admin_auth)):
    require_event(db, event_id)
    require_roster_unlocked(db, event_id)
    team = Team(event_id=event_id, name=name, code=unique_team_code(db, event_id, code, name), capacity=max(1, min(capacity, 100)))
    db.add(team); db.flush(); audit(db, actor, "team.create", team, name); db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}")
def edit_team(
    event_id: int, team_id: int, name: str = Form(...), code: str = Form(""), capacity: int = Form(10), telegram_chat_id: str = Form(""),
    telegram_invite_url: str = Form(""), gathering_riddle_ru: str = Form(""),
    gathering_riddle_kk: str = Form(""), active: bool = Form(False),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id: raise HTTPException(404, "Команда не найдена")
    normalized_code = team_slug(code or name)
    duplicate = db.scalar(select(Team).where(
        Team.event_id == event_id, Team.code == normalized_code, Team.id != team.id
    ))
    if duplicate:
        raise HTTPException(409, "Этот код уже используется другой командой")
    team.name, team.code, team.telegram_chat_id, team.active = name.strip(), normalized_code, telegram_chat_id or None, active
    team.capacity = max(1, min(capacity, 100))
    team.telegram_invite_url = telegram_invite_url.strip()
    team.gathering_riddle_ru = gathering_riddle_ru.strip()
    team.gathering_riddle_kk = gathering_riddle_kk.strip()
    audit(db, actor, "team.edit", team, name); db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/telegram/unbind")
def unbind_team_telegram_chat(
    event_id: int,
    team_id: int,
    db: Session = Depends(get_db),
    actor=Depends(admin_auth),
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    old_chat_id = team.telegram_chat_id
    team.telegram_chat_id = None
    team.telegram_invite_url = ""
    for election in db.scalars(select(CaptainElection).where(
        CaptainElection.team_id == team.id,
        CaptainElection.active.is_(True),
    )).all():
        election.active = False
        election.finished_at = datetime.utcnow()
    db.execute(delete(TeamQuestionPrompt).where(TeamQuestionPrompt.team_id == team.id))
    audit(db, actor, "team.telegram_unbind", team, f"chat_id={old_chat_id or 'none'}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/delete")
def delete_team(
    event_id: int, team_id: int, confirmation: str = Form(...),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_roster_unlocked(db, event_id)
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    if confirmation.strip() != team.name:
        raise HTTPException(400, "Для удаления введите точное название команды")
    team_players = list(db.scalars(select(Player).where(Player.team_id == team.id)).all())
    player_ids = [player.id for player in team_players]
    returned_to_pool = sum(return_player_to_registration_pool(db, player, event_id) for player in team_players)
    case_ids = list(db.scalars(select(DetectiveCase.id).where(DetectiveCase.team_id == team.id)).all())
    election_ids = list(db.scalars(select(CaptainElection.id).where(CaptainElection.team_id == team.id)).all())
    if election_ids or player_ids:
        db.execute(delete(CaptainVote).where(
            (CaptainVote.election_id.in_(election_ids or [-1])) |
            (CaptainVote.voter_player_id.in_(player_ids or [-1])) |
            (CaptainVote.candidate_player_id.in_(player_ids or [-1]))
        ))
    db.execute(delete(DetectiveSubmission).where(
        (DetectiveSubmission.team_id == team.id) |
        (DetectiveSubmission.captain_id.in_(player_ids or [-1]))
    ))
    db.execute(delete(DetectiveClue).where(
        (DetectiveClue.case_id.in_(case_ids or [-1])) |
        (DetectiveClue.player_id.in_(player_ids or [-1]))
    ))
    db.execute(delete(DetectiveCase).where(DetectiveCase.team_id == team.id))
    db.execute(delete(Answer).where(
        (Answer.team_id == team.id) |
        (Answer.player_id.in_(player_ids or [-1])) |
        (Answer.respondent_player_id.in_(player_ids or [-1]))
    ))
    db.execute(delete(TeamQuestionPrompt).where(TeamQuestionPrompt.team_id == team.id))
    db.execute(delete(ScoreAdjustment).where(
        (ScoreAdjustment.team_id == team.id) |
        (ScoreAdjustment.player_id.in_(player_ids or [-1]))
    ))
    db.execute(delete(CaptainElection).where(CaptainElection.team_id == team.id))
    db.execute(delete(Player).where(Player.team_id == team.id))
    name = team.name
    audit(
        db, actor, "team.delete", require_event(db, event_id),
        f"{name}; players={len(player_ids)}; returned_to_pool={returned_to_pool}",
    )
    db.delete(team)
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/players")
def create_player(event_id: int, team_id: int = Form(...), full_name: str = Form(...), registration_code: str = Form(""), role: PlayerRole = Form(...), db: Session = Depends(get_db), actor=Depends(admin_auth)):
    require_roster_unlocked(db, event_id)
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id: raise HTTPException(400, "Некорректная команда")
    running = db.scalar(select(Stage).where(
        Stage.event_id == event_id, Stage.stage_type == StageType.DETECTIVE,
        Stage.detective_status == DetectiveStatus.RUNNING,
    ))
    if running:
        raise HTTPException(409, "Нельзя менять состав во время запущенного детектива")
    if role == PlayerRole.CAPTAIN:
        for captain in db.scalars(select(Player).where(
            Player.team_id == team.id, Player.role == PlayerRole.CAPTAIN
        )).all():
            captain.role = PlayerRole.PLAYER
    code = registration_code.strip().upper() or f"MANUAL-{secrets.token_hex(8).upper()}"
    player = Player(team_id=team_id, full_name=full_name, registration_code=code, role=role)
    db.add(player); db.flush(); clear_detective_cases_for_teams(db, [team.id]); audit(db, actor, "player.create", player, full_name); db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/players/{player_id}")
def edit_player(
    event_id: int, player_id: int, full_name: str = Form(...), role: PlayerRole = Form(...),
    team_id: int = Form(...), active: bool = Form(False), reset_telegram: bool = Form(False),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_roster_unlocked(db, event_id)
    player = db.get(Player, player_id)
    if not player or not player.team or player.team.event_id != event_id: raise HTTPException(404, "Игрок не найден")
    target_team = db.get(Team, team_id)
    if not target_team or target_team.event_id != event_id:
        raise HTTPException(400, "Команда не найдена")
    old_team_id, old_active = player.team_id, player.active
    if role == PlayerRole.CAPTAIN:
        for teammate in db.scalars(select(Player).where(
            Player.team_id == target_team.id, Player.role == PlayerRole.CAPTAIN, Player.id != player.id
        )).all():
            teammate.role = PlayerRole.PLAYER
    player.team_id = target_team.id
    player.full_name, player.role, player.active = full_name, role, active
    if reset_telegram: player.telegram_user_id = player.telegram_username = player.registered_at = None
    if old_team_id != target_team.id or old_active != active:
        affected_teams = [old_team_id, target_team.id]
        running = db.scalar(select(Stage).where(
            Stage.event_id == event_id, Stage.stage_type == StageType.DETECTIVE,
            Stage.detective_status == DetectiveStatus.RUNNING,
        ))
        if running:
            raise HTTPException(409, "Нельзя перемещать игрока во время запущенного детектива")
        clear_detective_cases_for_teams(db, list(dict.fromkeys(affected_teams)))
    audit(db, actor, "player.edit", player, f"{full_name}; team={target_team.id}; role={role.value}"); db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/players/{player_id}/delete")
def delete_player(
    event_id: int, player_id: int, confirmation: str = Form(...),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_roster_unlocked(db, event_id)
    player = db.get(Player, player_id)
    if not player or not player.team or player.team.event_id != event_id:
        raise HTTPException(404, "Игрок не найден")
    if confirmation.strip() != player.full_name:
        raise HTTPException(400, "Введите точное имя участника для удаления")
    running = db.scalar(select(Stage).where(
        Stage.event_id == event_id, Stage.stage_type == StageType.DETECTIVE,
        Stage.detective_status == DetectiveStatus.RUNNING,
    ))
    if running:
        raise HTTPException(409, "Нельзя удалять участников во время запущенного детектива")
    team_id = player.team_id
    delete_player_dependencies(db, player)
    clear_detective_cases_for_teams(db, [team_id])
    name = player.full_name
    db.delete(player)
    audit(db, actor, "player.delete", require_event(db, event_id), f"{name}; team={team_id}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/players/{player_id}/unassign")
def unassign_player(
    event_id: int, player_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_roster_unlocked(db, event_id)
    player = db.get(Player, player_id)
    if not player or not player.team or player.team.event_id != event_id:
        raise HTTPException(404, "Участник не найден")
    if not player.telegram_user_id:
        raise HTTPException(409, "Участник без Telegram не может быть возвращён в регистрационный пул")
    team_id = player.team_id
    name = player.full_name
    return_player_to_registration_pool(db, player, event_id)
    delete_player_dependencies(db, player)
    clear_detective_cases_for_teams(db, [team_id])
    db.delete(player)
    audit(db, actor, "player.unassign", require_event(db, event_id), f"{name}; team={team_id}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/registrations/{registration_id}/assign")
async def assign_pending_registration(
    event_id: int, registration_id: int, team_id: int = Form(...),
    role: PlayerRole = Form(PlayerRole.PLAYER),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_roster_unlocked(db, event_id)
    pending = db.get(PendingRegistration, registration_id)
    team = db.get(Team, team_id)
    if not pending or pending.event_id != event_id:
        raise HTTPException(404, "Заявка на регистрацию не найдена")
    if not team or team.event_id != event_id:
        raise HTTPException(400, "Некорректная команда")
    current_size = db.scalar(select(func.count(Player.id)).where(Player.team_id == team.id, Player.active.is_(True))) or 0
    if current_size >= team.capacity:
        raise HTTPException(409, "В команде нет свободных мест. Увеличьте вместимость или выберите другую команду")
    running = db.scalar(select(Stage).where(
        Stage.event_id == event_id, Stage.stage_type == StageType.DETECTIVE,
        Stage.detective_status == DetectiveStatus.RUNNING,
    ))
    if running:
        raise HTTPException(409, "Нельзя менять состав во время запущенного детектива")
    if role == PlayerRole.CAPTAIN:
        for captain in db.scalars(select(Player).where(
            Player.team_id == team.id, Player.role == PlayerRole.CAPTAIN
        )).all():
            captain.role = PlayerRole.PLAYER
    player = Player(
        team_id=team.id,
        full_name=pending.full_name,
        registration_code=f"AUTO-{secrets.token_hex(8).upper()}",
        role=role,
        telegram_user_id=pending.telegram_user_id,
        telegram_username=pending.telegram_username,
        registered_at=datetime.utcnow(),
        preferred_language=pending.preferred_language,
    )
    db.add(player)
    db.delete(pending)
    db.flush()
    clear_detective_cases_for_teams(db, [team.id])
    audit(db, actor, "registration.assign", player, f"team={team.id}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/registrations/assign-bulk")
async def assign_pending_registrations_bulk(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    require_roster_unlocked(db, event_id)
    form = await request.form()
    assignments: list[tuple[PendingRegistration, Team, PlayerRole]] = []
    for key, raw_team_id in form.multi_items():
        if not key.startswith("team_") or not str(raw_team_id).strip():
            continue
        try:
            registration_id = int(key.removeprefix("team_"))
            team_id = int(raw_team_id)
            role = PlayerRole(str(form.get(f"role_{registration_id}", PlayerRole.PLAYER.value)))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректные данные распределения")
        pending = db.get(PendingRegistration, registration_id)
        team = db.get(Team, team_id)
        if not pending or pending.event_id != event.id:
            raise HTTPException(404, "Одна из регистраций больше не существует. Обновите страницу")
        if not team or team.event_id != event.id or not team.active:
            raise HTTPException(400, "Выбрана недоступная команда")
        assignments.append((pending, team, role))

    if not assignments:
        raise HTTPException(400, "Выберите команду хотя бы для одного участника")
    running = db.scalar(select(Stage).where(
        Stage.event_id == event_id,
        Stage.stage_type == StageType.DETECTIVE,
        Stage.detective_status == DetectiveStatus.RUNNING,
    ))
    if running:
        raise HTTPException(409, "Нельзя менять состав во время запущенного детектива")

    additions: dict[int, int] = {}
    teams_by_id: dict[int, Team] = {}
    for _, team, _ in assignments:
        additions[team.id] = additions.get(team.id, 0) + 1
        teams_by_id[team.id] = team
    for team_id, amount in additions.items():
        team = teams_by_id[team_id]
        current_size = db.scalar(select(func.count(Player.id)).where(
            Player.team_id == team_id, Player.active.is_(True)
        )) or 0
        if current_size + amount > team.capacity:
            free = max(0, team.capacity - current_size)
            raise HTTPException(
                409,
                f"В команде «{team.name}» свободно мест: {free}, выбрано участников: {amount}",
            )

    affected_team_ids: set[int] = set()
    created_players: list[Player] = []
    for pending, team, role in assignments:
        if role == PlayerRole.CAPTAIN:
            for captain in db.scalars(select(Player).where(
                Player.team_id == team.id, Player.role == PlayerRole.CAPTAIN
            )).all():
                captain.role = PlayerRole.PLAYER
            for created in created_players:
                if created.team_id == team.id and created.role == PlayerRole.CAPTAIN:
                    created.role = PlayerRole.PLAYER
        player = Player(
            team_id=team.id,
            full_name=pending.full_name,
            registration_code=f"AUTO-{secrets.token_hex(8).upper()}",
            role=role,
            telegram_user_id=pending.telegram_user_id,
            telegram_username=pending.telegram_username,
            registered_at=datetime.utcnow(),
            preferred_language=pending.preferred_language,
        )
        db.add(player)
        db.delete(pending)
        created_players.append(player)
        affected_team_ids.add(team.id)
    db.flush()
    clear_detective_cases_for_teams(db, list(affected_team_ids))
    audit(
        db, actor, "registration.assign_bulk", event,
        f"players={len(created_players)}; teams={sorted(affected_team_ids)}",
    )
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/registrations/{registration_id}/delete")
def delete_pending_registration(
    event_id: int, registration_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    pending = db.get(PendingRegistration, registration_id)
    if not pending or pending.event_id != event_id:
        raise HTTPException(404, "Заявка на регистрацию не найдена")
    details = pending.full_name
    db.delete(pending)
    audit(db, actor, "registration.delete", require_event(db, event_id), details)
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/stages")
def create_stage(
    event_id: int, title: str = Form(...), description: str = Form(""),
    title_kk: str = Form(""), description_kk: str = Form(""),
    program_id: int | None = Form(None),
    stage_type: StageType = Form(StageType.QUIZ),
    detective_duration_seconds: int = Form(1200),
    detective_points: str = Form("30,25,20,17,14,10"),
    detective_point_1: float | None = Form(None), detective_point_2: float | None = Form(None),
    detective_point_3: float | None = Form(None), detective_point_4: float | None = Form(None),
    detective_point_5: float | None = Form(None), detective_point_6: float | None = Form(None),
    default_duration_seconds: int | None = Form(None),
    default_submission_seconds: int | None = Form(None),
    default_team_points: float | None = Form(None),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    position = (db.scalar(select(func.max(Stage.position)).where(Stage.event_id == event_id)) or 0) + 1
    point_fields = [detective_point_1, detective_point_2, detective_point_3, detective_point_4, detective_point_5, detective_point_6]
    if any(value is not None for value in point_fields):
        detective_points = ",".join(str(max(0, value or 0)) for value in point_fields)
    stage = Stage(
        event_id=event_id, title=title, description=description,
        title_kk=title_kk, description_kk=description_kk, position=position,
        stage_type=stage_type,
        detective_duration_seconds=max(60, min(detective_duration_seconds, 7200)),
        detective_points=detective_points,
        default_duration_seconds=max(5, min(default_duration_seconds or event.default_question_duration, 3600)),
        default_submission_seconds=max(5, min(default_submission_seconds or 20, 300)),
        default_team_points=max(0, min(default_team_points if default_team_points is not None else event.default_team_points, 10000)),
    )
    db.add(stage); db.flush()
    if program_id is not None:
        program = db.get(GameProgram, program_id)
        if not program or program.event_id != event_id:
            raise HTTPException(404, "Игра не найдена")
        link_position = (
            db.scalar(select(func.max(GameProgramStage.position)).where(
                GameProgramStage.program_id == program.id
            )) or 0
        ) + 1
        db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=link_position))
    audit(db, actor, "stage.create", stage, f"{title}; program_id={program_id}"); db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/stages/{stage_id}/defaults")
def update_stage_defaults(
    event_id: int, stage_id: int,
    default_duration_seconds: int = Form(...),
    default_submission_seconds: int = Form(...),
    default_team_points: float = Form(...),
    apply_to_all: bool = Form(False),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id or stage.stage_type != StageType.QUIZ:
        raise HTTPException(404, "Этап с вопросами не найден")
    stage.default_duration_seconds = max(5, min(default_duration_seconds, 3600))
    stage.default_submission_seconds = max(5, min(default_submission_seconds, 300))
    stage.default_team_points = max(0, min(default_team_points, 10000))
    if apply_to_all:
        for question in stage.questions:
            question.duration_seconds = stage.default_duration_seconds
            question.submission_seconds = stage.default_submission_seconds
            question.team_points = stage.default_team_points
    audit(
        db, actor, "stage.defaults", stage,
        f"duration={stage.default_duration_seconds}; submission={stage.default_submission_seconds}; "
        f"points={stage.default_team_points}; apply_to_all={apply_to_all}",
    )
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/stages/{stage_id}/edit")
def edit_stage(
    event_id: int, stage_id: int,
    title: str = Form(...), title_kk: str = Form(""),
    description: str = Form(""), description_kk: str = Form(""),
    detective_duration_seconds: int | None = Form(None),
    detective_points: str | None = Form(None),
    detective_point_1: float | None = Form(None), detective_point_2: float | None = Form(None),
    detective_point_3: float | None = Form(None), detective_point_4: float | None = Form(None),
    detective_point_5: float | None = Form(None), detective_point_6: float | None = Form(None),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id:
        raise HTTPException(404, "Этап не найден")
    stage.title = title.strip()
    stage.title_kk = title_kk.strip()
    stage.description = description.strip()
    stage.description_kk = description_kk.strip()
    if stage.stage_type == StageType.DETECTIVE:
        if detective_duration_seconds is not None:
            stage.detective_duration_seconds = max(60, min(detective_duration_seconds, 7200))
        point_fields = [detective_point_1, detective_point_2, detective_point_3, detective_point_4, detective_point_5, detective_point_6]
        if any(value is not None for value in point_fields):
            stage.detective_points = ",".join(str(max(0, value or 0)) for value in point_fields)
        elif detective_points is not None:
            stage.detective_points = detective_points.strip()
    audit(db, actor, "stage.edit", stage, stage.title)
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/programs")
def create_program(
    event_id: int, title: str = Form(...), description: str = Form(""),
    stage_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    require_event(db, event_id)
    unique_stage_ids = list(dict.fromkeys(stage_ids))
    stages = db.scalars(select(Stage).where(
        Stage.event_id == event_id, Stage.id.in_(unique_stage_ids)
    )).all()
    stage_map = {stage.id: stage for stage in stages}
    if len(stage_map) != len(unique_stage_ids):
        raise HTTPException(400, "Один из этапов не найден")
    program = GameProgram(event_id=event_id, title=title.strip(), description=description.strip())
    db.add(program); db.flush()
    for position, stage_id in enumerate(unique_stage_ids, 1):
        db.add(GameProgramStage(program_id=program.id, stage_id=stage_id, position=position))
    audit(db, actor, "program.create", program, f"stages={unique_stage_ids}")
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/programs/{program_id}/launch")
async def launch_program(
    event_id: int, program_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    program = db.scalar(select(GameProgram).options(
        selectinload(GameProgram.stage_links).selectinload(GameProgramStage.stage).selectinload(Stage.questions)
    ).where(GameProgram.id == program_id, GameProgram.event_id == event_id))
    if not program:
        raise HTTPException(404, "Игра не найдена")
    active_teams = db.scalars(select(Team).where(
        Team.event_id == event_id, Team.active.is_(True)
    ).order_by(Team.name)).all()
    teams_needing_captain = [
        team for team in active_teams
        if len([player for player in team.players if player.active and player.role == PlayerRole.CAPTAIN]) != 1
    ]
    if teams_needing_captain:
        if not get_settings().telegram_bot_token:
            raise HTTPException(409, "Нельзя провести выбор капитанов: Telegram-бот не настроен")
        problems = []
        for team in teams_needing_captain:
            candidates = [player for player in team.players if player.active and player.telegram_user_id]
            if len(candidates) < 2:
                problems.append(f"{team.name}: меньше двух зарегистрированных участников")
        if problems:
            raise HTTPException(409, "Нельзя начать выбор капитанов. " + "; ".join(problems))
        started = 0
        for team in teams_needing_captain:
            active_election = db.scalar(select(CaptainElection).where(
                CaptainElection.team_id == team.id, CaptainElection.active.is_(True)
            ))
            if not active_election:
                await start_captain_election(event_id, team.id, db, actor)
                started += 1
        audit(db, actor, "program.captain_preflight", program, f"teams={len(teams_needing_captain)}; started={started}")
        db.commit()
        return go(event_id, "people")
    program_items: list[tuple[str, Question | Stage]] = []
    for link in program.stage_links:
        stage = link.stage
        if stage.stage_type == StageType.DETECTIVE:
            prepared_cases(db, event, stage)
            program_items.append(("detective", stage))
        else:
            program_items.extend(("question", question) for question in stage.questions)
    if not program_items:
        raise HTTPException(409, "В выбранной игре нет вопросов или детективных этапов")
    questions = [item for kind, item in program_items if kind == "question"]
    for other in db.scalars(select(GameProgram).where(GameProgram.event_id == event_id)).all():
        if other.status == "RUNNING":
            other.status = "FINISHED"
            other.finished_at = datetime.utcnow()
    for question in questions:
        question.status = QuestionStatus.DRAFT
        question.opened_at = None
        question.closed_at = None
    program.status = "RUNNING"
    program.started_at = datetime.utcnow()
    program.finished_at = None
    first_kind, first = program_items[0]
    if first_kind == "detective":
        await start_detective_stage(db, event, first, actor)
        first_detail = f"first_detective={first.id}"
    else:
        event.current_question_id = first.id
        event.current_detective_stage_id = None
        event.display_mode = "QUESTION"
        event.timer_started_at = None
        event.timer_duration_seconds = first.duration_seconds
        set_question_status(db, first, QuestionStatus.OPEN, actor)
        first_detail = f"first_question={first.id}"
    audit(db, actor, "program.launch", program, first_detail)
    db.commit()
    return go(event_id, "live")


@router.post("/events/{event_id}/stages/{stage_id}/launch")
async def launch_single_stage(
    event_id: int, stage_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id:
        raise HTTPException(404, "Этап не найден")
    program = GameProgram(
        event_id=event_id,
        title=f"Этап: {stage.title}",
        description="Разовый запуск отдельного этапа",
    )
    db.add(program)
    db.flush()
    db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=1))
    audit(db, actor, "stage.launch_prepare", stage, f"program={program.id}")
    db.commit()
    return await launch_program(event_id, program.id, db, actor)


@router.post("/events/{event_id}/programs/{program_id}/delete")
def delete_program(
    event_id: int, program_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    program = db.get(GameProgram, program_id)
    if not program or program.event_id != event_id:
        raise HTTPException(404, "Игра не найдена")
    if program.status == "RUNNING":
        raise HTTPException(409, "Сначала завершите запущенную игру")
    title = program.title
    db.delete(program)
    audit(db, actor, "program.delete", require_event(db, event_id), title)
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/detective/{stage_id}/generate")
def generate_detective(
    event_id: int, stage_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id or stage.stage_type != StageType.DETECTIVE:
        raise HTTPException(404, "Детективный этап не найден.")
    if stage.detective_status == DetectiveStatus.RUNNING:
        raise HTTPException(409, "Нельзя пересоздавать кейсы во время игры.")
    try:
        cases = generate_cases_for_stage(db, stage)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    stage.detective_status = DetectiveStatus.READY
    audit(db, actor, "detective.generate", stage, f"cases={len(cases)}")
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/detective/{stage_id}/start")
async def start_detective(
    event_id: int, stage_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    event = require_event(db, event_id)
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id or stage.stage_type != StageType.DETECTIVE:
        raise HTTPException(404, "Детективный этап не найден.")
    teams_needing_captain = [
        team for team in event.teams if team.active
        and len([player for player in team.players if player.active and player.role == PlayerRole.CAPTAIN]) != 1
    ]
    if teams_needing_captain:
        problems = []
        for team in teams_needing_captain:
            candidates = [player for player in team.players if player.active and player.telegram_user_id]
            if not team.telegram_chat_id:
                problems.append(f"{team.name}: не подключена Telegram-беседа")
            elif len(candidates) < 2:
                problems.append(f"{team.name}: недостаточно зарегистрированных участников")
        if problems:
            raise HTTPException(409, "Сначала выберите капитанов. " + "; ".join(problems))
        for team in teams_needing_captain:
            active_election = db.scalar(select(CaptainElection).where(
                CaptainElection.team_id == team.id, CaptainElection.active.is_(True)
            ))
            if not active_election:
                await start_captain_election(event_id, team.id, db, actor)
        return go(event_id, "people")
    await start_detective_stage(db, event, stage, actor)
    return go(event_id, "content")


@router.post("/events/{event_id}/detective/{stage_id}/finish")
def finish_detective(
    event_id: int, stage_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    event = require_event(db, event_id)
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id:
        raise HTTPException(404, "Этап не найден.")
    stage.detective_status = DetectiveStatus.FINISHED
    event.display_mode = "LEADERBOARD"
    event.timer_started_at = None
    event.current_detective_stage_id = None
    audit(db, actor, "detective.finish", stage)
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions")
def create_question(
    event_id: int, stage_id: int = Form(...), title: str = Form(...), text: str = Form(...),
    correct_answer: str = Form(...), explanation: str = Form(""), duration_seconds: int = Form(60),
    title_kk: str = Form(""), text_kk: str = Form(""),
    correct_answer_kk: str = Form(""), explanation_kk: str = Form(""),
    personal_points: float = Form(1), team_points: float = Form(5),
    personal_answers_enabled: bool = Form(False), team_answers_enabled: bool = Form(False),
    submission_seconds: int = Form(20), question_type: QuestionType = Form(QuestionType.TEXT),
    options_text: str = Form(""), show_anonymous_answers: bool = Form(False),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    stage = db.get(Stage, stage_id)
    if not stage or stage.event_id != event_id: raise HTTPException(400, "Некорректный этап")
    options = [x.strip() for x in options_text.splitlines() if x.strip()]
    if stage.system_key == "choice":
        if question_type not in {QuestionType.CHOICE, QuestionType.CHOICE_EXPLANATION}:
            question_type = QuestionType.CHOICE
        if len(options) < 2:
            raise HTTPException(400, "Для второго этапа добавьте минимум два варианта ответа")
    else:
        question_type = QuestionType.TEXT
        options = []
    if stage.system_key == "test":
        personal_points = team_points = 0
    position = (db.scalar(select(func.max(Question.position)).where(Question.stage_id == stage_id)) or 0) + 1
    question = Question(
        stage_id=stage_id, title=title, text=text, correct_answer=correct_answer, explanation=explanation,
        title_kk=title_kk, text_kk=text_kk, correct_answer_kk=correct_answer_kk, explanation_kk=explanation_kk,
        position=position, personal_points=personal_points, team_points=team_points,
        personal_answers_enabled=personal_answers_enabled, team_answers_enabled=team_answers_enabled,
        duration_seconds=max(5, min(duration_seconds, 3600)),
        submission_seconds=max(5, min(submission_seconds, 300)), question_type=question_type,
        options_json=json.dumps(options, ensure_ascii=False),
        show_anonymous_answers=show_anonymous_answers,
    )
    db.add(question); db.flush(); audit(db, actor, "question.create", question, title)
    event = require_event(db, event_id)
    event.timer_duration_seconds = max(5, min(duration_seconds, 3600))
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions/{question_id}/edit")
def edit_question(
    event_id: int, question_id: int,
    title: str = Form(...), text: str = Form(...), correct_answer: str = Form(...),
    explanation: str = Form(""), title_kk: str = Form(""), text_kk: str = Form(""),
    correct_answer_kk: str = Form(""), explanation_kk: str = Form(""),
    duration_seconds: int = Form(60), submission_seconds: int = Form(20),
    team_points: float = Form(5), question_type: QuestionType = Form(QuestionType.TEXT),
    options_text: str = Form(""), team_answers_enabled: bool = Form(False),
    show_anonymous_answers: bool = Form(False),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    question = db.get(Question, question_id)
    if not question or question.stage.event_id != event_id:
        raise HTTPException(404, "Вопрос не найден")
    options = [line.strip() for line in options_text.splitlines() if line.strip()]
    if question.stage.system_key == "choice":
        if question_type not in {QuestionType.CHOICE, QuestionType.CHOICE_EXPLANATION}:
            question_type = QuestionType.CHOICE
        if len(options) < 2:
            raise HTTPException(400, "Для второго этапа добавьте минимум два варианта ответа")
    else:
        question_type = QuestionType.TEXT
        options = []
    if question.stage.system_key == "test":
        team_points = 0
    question.title = title.strip()
    question.text = text.strip()
    question.correct_answer = correct_answer.strip()
    question.explanation = explanation.strip()
    question.title_kk = title_kk.strip()
    question.text_kk = text_kk.strip()
    question.correct_answer_kk = correct_answer_kk.strip()
    question.explanation_kk = explanation_kk.strip()
    question.duration_seconds = max(5, min(duration_seconds, 3600))
    question.submission_seconds = max(5, min(submission_seconds, 300))
    question.team_points = max(0, min(team_points, 10000))
    question.question_type = question_type
    question.options_json = json.dumps(options, ensure_ascii=False)
    question.team_answers_enabled = team_answers_enabled
    question.show_anonymous_answers = show_anonymous_answers
    audit(db, actor, "question.edit", question, question.title)
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions/{question_id}/delete")
def delete_question(
    event_id: int, question_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    question = db.get(Question, question_id)
    if not question or question.stage.event_id != event_id:
        raise HTTPException(404, "Вопрос не найден")
    title = question.title
    if event.current_question_id == question.id:
        event.current_question_id = None
        event.display_mode = "WELCOME"
        event.timer_started_at = None
    db.execute(delete(Answer).where(Answer.question_id == question.id))
    db.execute(delete(TeamQuestionPrompt).where(TeamQuestionPrompt.question_id == question.id))
    db.delete(question)
    audit(db, actor, "question.delete", event, title)
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions/{question_id}/copy")
def copy_question(
    event_id: int, question_id: int,
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    source = db.get(Question, question_id)
    if not source or source.stage.event_id != event_id:
        raise HTTPException(404, "Вопрос не найден")
    position = (db.scalar(select(func.max(Question.position)).where(
        Question.stage_id == source.stage_id
    )) or 0) + 1
    duplicate = Question(
        stage_id=source.stage_id,
        position=position,
        title=f"{source.title} — копия",
        text=source.text,
        correct_answer=source.correct_answer,
        explanation=source.explanation,
        title_kk=f"{source.title_kk} — көшірме" if source.title_kk else "",
        text_kk=source.text_kk,
        correct_answer_kk=source.correct_answer_kk,
        explanation_kk=source.explanation_kk,
        personal_answers_enabled=source.personal_answers_enabled,
        team_answers_enabled=source.team_answers_enabled,
        personal_points=source.personal_points,
        team_points=source.team_points,
        duration_seconds=source.duration_seconds,
        submission_seconds=source.submission_seconds,
        question_type=source.question_type,
        options_json=source.options_json,
        show_anonymous_answers=source.show_anonymous_answers,
        status=QuestionStatus.DRAFT,
    )
    db.add(duplicate)
    db.flush()
    audit(db, actor, "question.copy", duplicate, f"source={source.id}")
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions/{question_id}/move")
def move_question(
    event_id: int, question_id: int, direction: str = Form(...),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    question = db.get(Question, question_id)
    if not question or question.stage.event_id != event_id:
        raise HTTPException(404, "Вопрос не найден")
    if direction not in {"up", "down"}:
        raise HTTPException(400, "Неизвестное направление")
    ordering = Question.position.desc() if direction == "up" else Question.position.asc()
    comparison = Question.position < question.position if direction == "up" else Question.position > question.position
    neighbour = db.scalar(select(Question).where(
        Question.stage_id == question.stage_id,
        comparison,
    ).order_by(ordering))
    if neighbour:
        original_position = question.position
        neighbour_position = neighbour.position
        temporary_position = (db.scalar(select(func.max(Question.position)).where(
            Question.stage_id == question.stage_id
        )) or 0) + 1
        question.position = temporary_position
        db.flush()
        neighbour.position = original_position
        db.flush()
        question.position = neighbour_position
        audit(db, actor, "question.move", question, f"direction={direction}; from={original_position}; to={neighbour_position}")
        db.commit()
    return go(event_id, "content")


@router.get("/events/{event_id}/content/export")
def export_content(event_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)):
    event = require_event(db, event_id)
    stages = db.scalars(
        select(Stage).options(selectinload(Stage.questions))
        .where(Stage.event_id == event_id).order_by(Stage.position)
    ).all()
    payload = {
        "format": "intellectual-game-content",
        "version": 1,
        "event": {"name": event.name},
        "stages": [{
            "title_ru": stage.title,
            "title_kk": stage.title_kk,
            "description_ru": stage.description,
            "description_kk": stage.description_kk,
            "default_duration_seconds": stage.default_duration_seconds,
            "default_submission_seconds": stage.default_submission_seconds,
            "default_team_points": stage.default_team_points,
            "questions": [{
                "title_ru": q.title,
                "title_kk": q.title_kk,
                "text_ru": q.text,
                "text_kk": q.text_kk,
                "correct_answer_ru": q.correct_answer,
                "correct_answer_kk": q.correct_answer_kk,
                "explanation_ru": q.explanation,
                "explanation_kk": q.explanation_kk,
                "duration_seconds": q.duration_seconds,
                "submission_seconds": q.submission_seconds,
                "question_type": q.question_type.value,
                "options": json.loads(q.options_json or "[]"),
                "show_anonymous_answers": q.show_anonymous_answers,
                "personal_points": q.personal_points,
                "team_points": q.team_points,
                "personal_answers_enabled": q.personal_answers_enabled,
                "team_answers_enabled": q.team_answers_enabled,
            } for q in stage.questions],
        } for stage in stages],
    }
    audit(db, actor, "content.export", event, f"stages={len(stages)}")
    db.commit()
    filename = f"event-{event_id}-content.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/events/{event_id}/content/fill-default")
def fill_default_content(event_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)):
    event = require_event(db, event_id)
    program, created = seed_game_content_for_event(db, event)
    audit(
        db,
        actor,
        "content.default_created" if created else "content.default_skipped",
        program,
        "3 stages and 10 questions" if created else "content already exists",
    )
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/content/import")
async def import_content(
    event_id: int, content_file: UploadFile = File(...),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    raw = await content_file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Файл больше 2 МБ")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Не удалось прочитать JSON-файл")
    if payload.get("format") != "intellectual-game-content" or payload.get("version") != 1:
        raise HTTPException(400, "Неподдерживаемый формат файла")
    stages_data = payload.get("stages")
    if not isinstance(stages_data, list) or len(stages_data) > 100:
        raise HTTPException(400, "Некорректный список этапов")
    next_stage_position = (db.scalar(select(func.max(Stage.position)).where(Stage.event_id == event_id)) or 0) + 1
    imported_questions = 0
    for stage_index, stage_data in enumerate(stages_data):
        if not isinstance(stage_data, dict):
            raise HTTPException(400, "Некорректные данные этапа")
        questions_data = stage_data.get("questions", [])
        if not isinstance(questions_data, list) or len(questions_data) > 500:
            raise HTTPException(400, "Некорректный список вопросов")
        title_ru = str(stage_data.get("title_ru", "")).strip()
        title_kk = str(stage_data.get("title_kk", "")).strip()
        if not title_ru and not title_kk:
            raise HTTPException(400, "У этапа отсутствует название")
        stage = Stage(
            event_id=event_id,
            position=next_stage_position + stage_index,
            title=title_ru or title_kk,
            title_kk=title_kk,
            description=str(stage_data.get("description_ru", "")),
            description_kk=str(stage_data.get("description_kk", "")),
            default_duration_seconds=max(5, min(int(stage_data.get("default_duration_seconds", event.default_question_duration)), 3600)),
            default_submission_seconds=max(5, min(int(stage_data.get("default_submission_seconds", 20)), 300)),
            default_team_points=max(0, min(float(stage_data.get("default_team_points", event.default_team_points)), 10000)),
        )
        db.add(stage); db.flush()
        for question_index, item in enumerate(questions_data, 1):
            if not isinstance(item, dict):
                raise HTTPException(400, "Некорректные данные вопроса")
            text_ru, text_kk = str(item.get("text_ru", "")).strip(), str(item.get("text_kk", "")).strip()
            answer_ru = str(item.get("correct_answer_ru", "")).strip()
            answer_kk = str(item.get("correct_answer_kk", "")).strip()
            if not (text_ru or text_kk) or not (answer_ru or answer_kk):
                raise HTTPException(400, "У вопроса отсутствует текст или правильный ответ")
            duration = max(5, min(int(item.get("duration_seconds", event.default_question_duration)), 3600))
            db.add(Question(
                stage_id=stage.id, position=question_index,
                title=str(item.get("title_ru", "")).strip() or str(item.get("title_kk", "")).strip() or f"Вопрос {question_index}",
                title_kk=str(item.get("title_kk", "")),
                text=text_ru or text_kk, text_kk=text_kk,
                correct_answer=answer_ru or answer_kk, correct_answer_kk=answer_kk,
                explanation=str(item.get("explanation_ru", "")),
                explanation_kk=str(item.get("explanation_kk", "")),
                duration_seconds=duration,
                submission_seconds=max(5, min(int(item.get("submission_seconds", 20)), 300)),
                question_type=QuestionType(item.get("question_type", "TEXT")),
                options_json=json.dumps(item.get("options", []), ensure_ascii=False),
                show_anonymous_answers=bool(item.get("show_anonymous_answers", True)),
                personal_points=float(item.get("personal_points", event.default_personal_points)),
                team_points=float(item.get("team_points", event.default_team_points)),
                personal_answers_enabled=bool(item.get("personal_answers_enabled", True)),
                team_answers_enabled=bool(item.get("team_answers_enabled", True)),
            ))
            imported_questions += 1
    audit(db, actor, "content.import", event, f"stages={len(stages_data)}; questions={imported_questions}")
    db.commit()
    return go(event_id, "content")


@router.post("/events/{event_id}/questions/{question_id}/show")
async def show_question(event_id: int, question_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)):
    event, question = require_event(db, event_id), db.get(Question, question_id)
    if not question or question.stage.event_id != event_id: raise HTTPException(404, "Вопрос не найден")
    if not await require_captains_before_game(event, db, actor):
        return go(event_id, "people")
    event.current_question_id, event.display_mode = question.id, "QUESTION"
    event.timer_started_at = None
    event.timer_duration_seconds = question.duration_seconds
    set_question_status(db, question, QuestionStatus.OPEN, actor)
    audit(db, actor, "display.question", event, str(question.id)); db.commit()
    from .telegram_sync import notify_team_chats
    await notify_team_chats(db, event, question, "QUESTION")
    return go(event_id, "live")


@router.post("/events/{event_id}/display")
def control_display(
    event_id: int, mode: str = Form(...), duration_seconds: int | None = Form(None),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    event = require_event(db, event_id)
    allowed = {"WELCOME", "QUESTION", "ANSWER", "LEADERBOARD", "PAUSED"}
    if mode == "TIMER":
        event.timer_duration_seconds = max(5, min(duration_seconds or event.timer_duration_seconds, 3600))
        event.timer_started_at = datetime.utcnow()
        event.display_mode = "QUESTION"
    elif mode in allowed:
        event.display_mode = mode
        if mode != "QUESTION": event.timer_started_at = None
    else:
        raise HTTPException(400, "Неизвестный режим экрана")
    audit(db, actor, "display.mode", event, mode); db.commit()
    return go(event_id, "live")


@router.post("/events/{event_id}/questions/{question_id}/grade-all")
def grade_all(event_id: int, question_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)):
    question = db.get(Question, question_id)
    if not question or question.stage.event_id != event_id: raise HTTPException(404, "Вопрос не найден")
    grade_all_answers(db, question, actor)
    return go(event_id, "answers")


@router.post("/events/{event_id}/answers/{answer_id}/grade")
def answer_grade(
    event_id: int, answer_id: int,
    correct: bool = Form(...), points: float | None = Form(None),
    db: Session = Depends(get_db), actor=Depends(admin_auth),
):
    answer = db.get(Answer, answer_id)
    if not answer or answer.question.stage.event_id != event_id: raise HTTPException(404, "Ответ не найден")
    grade_answer(db, answer, correct, actor, points)
    return go(event_id, "answers")


@router.post("/events/{event_id}/scores")
def score(event_id: int, target_type: str = Form(...), target_id: int = Form(...), points: float = Form(...), reason: str = Form(...), db: Session = Depends(get_db), actor=Depends(admin_auth)):
    adjust_score(db, event_id, points, reason, player_id=target_id if target_type == "player" else None, team_id=target_id if target_type == "team" else None, actor=actor)
    return go(event_id, "scores")


@router.post("/events/{event_id}/settings")
def update_settings(
    event_id: int,
    default_question_duration: int = Form(...),
    default_personal_points: float = Form(...),
    default_team_points: float = Form(...),
    timer_sound_enabled: bool = Form(False),
    registration_code: str = Form(""),
    display_language: str = Form("BOTH"),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    event.default_question_duration = max(5, min(default_question_duration, 3600))
    event.default_personal_points = max(0, min(default_personal_points, 10000))
    event.default_team_points = max(0, min(default_team_points, 10000))
    event.timer_sound_enabled = timer_sound_enabled
    if registration_code.strip():
        event.registration_code = registration_code.strip().upper()
    event.display_language = display_language if display_language in {"RU", "KK", "BOTH"} else "BOTH"
    audit(
        db, actor, "event.settings", event,
        f"default_duration={event.default_question_duration}; "
        f"personal_points={event.default_personal_points}; team_points={event.default_team_points}; "
        f"sound={timer_sound_enabled}",
    )
    db.commit()
    return go(event_id, "settings")


async def send_to_registered_players(players: list[Player], build_text) -> tuple[int, int]:
    token = get_settings().telegram_bot_token
    if not token:
        raise HTTPException(409, "TELEGRAM_BOT_TOKEN не настроен")
    delivered = failed = 0
    bot = Bot(token)
    try:
        for player in players:
            if not player.telegram_user_id:
                continue
            try:
                await bot.send_message(player.telegram_user_id, build_text(player))
                delivered += 1
            except Exception:
                failed += 1
    finally:
        await bot.session.close()
    return delivered, failed


@router.post("/events/{event_id}/teams/{team_id}/briefing")
async def send_team_briefing(
    event_id: int, team_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    players = [player for player in team.players if player.active]
    roster = "\n".join(f"• {player.full_name}" for player in players)
    def text(player):
        if player.preferred_language == "KK":
            return "Залдан өз командаңыздың мүшелерін табыңыз:\n\n" + roster
        return "Найдите своих сокомандников в зале:\n\n" + roster
    delivered, failed = await send_to_registered_players(players, text)
    audit(db, actor, "team.roster_send", team, f"delivered={delivered}; failed={failed}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/riddle")
async def send_team_name_riddle(
    event_id: int, team_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    if not team.gathering_riddle_ru.strip() and not team.gathering_riddle_kk.strip():
        raise HTTPException(409, "Сначала добавьте загадку названия команды")
    players = [player for player in team.players if player.active]
    def riddle_text(player):
        kk = player.preferred_language == "KK"
        riddle = (
            team.gathering_riddle_kk
            if kk and team.gathering_riddle_kk.strip()
            else (team.gathering_riddle_ru or team.gathering_riddle_kk)
        )
        if kk:
            return (
                "Командаңыздың атауы туралы жұмбақ:\n\n"
                f"{riddle}\n\nЖауабын тапқаннан кейін команда атауын жүргізушіге айтыңыз."
            )
        return (
            "Загадка о названии вашей команды:\n\n"
            f"{riddle}\n\nКогда догадаетесь, сообщите название ведущему."
        )
    delivered, failed = await send_to_registered_players(players, riddle_text)
    audit(db, actor, "team.riddle_send", team, f"delivered={delivered}; failed={failed}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/invite")
async def send_team_invite(
    event_id: int, team_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    if not team.telegram_invite_url:
        raise HTTPException(409, "Сначала укажите ссылку-приглашение команды")
    players = [player for player in team.players if player.active]
    def invite_text(player):
        if player.preferred_language == "KK":
            return f"Командаңыз жиналды. Командалық әңгімеге қосылыңыз:\n{team.telegram_invite_url}"
        return f"Ваша команда собралась. Вступите в командную беседу:\n{team.telegram_invite_url}"

    delivered, failed = await send_to_registered_players(players, invite_text)
    audit(db, actor, "team.invite", team, f"delivered={delivered}; failed={failed}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/election/start")
async def start_captain_election(
    event_id: int, team_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise HTTPException(404, "Команда не найдена")
    if not get_settings().telegram_bot_token:
        raise HTTPException(409, "TELEGRAM_BOT_TOKEN не настроен")
    candidates = [player for player in team.players if player.active and player.telegram_user_id]
    if len(candidates) < 2:
        raise HTTPException(409, "Для голосования нужны минимум два зарегистрированных игрока")
    for old in db.scalars(select(CaptainElection).where(CaptainElection.team_id == team.id, CaptainElection.active.is_(True))).all():
        old.active = False
        old.finished_at = datetime.utcnow()
    election = CaptainElection(team_id=team.id)
    db.add(election); db.flush()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=player.full_name, callback_data=f"captainvote:{election.id}:{player.id}")]
        for player in candidates
    ])
    bot = Bot(get_settings().telegram_bot_token)
    delivered = 0
    failed = 0
    try:
        for player in candidates:
            text = (
                "Команда капитанын таңдаңыз. Өзіңізге дауыс беруге болмайды — басқа қатысушыны таңдаңыз.\n\n"
                if player.preferred_language == "KK" else
                "Выберите капитана команды. Голосовать за себя нельзя — выберите другого участника.\n\n"
            ) + f"Проголосовало: 0 из {len(candidates)}."
            try:
                await bot.send_message(player.telegram_user_id, text, reply_markup=keyboard)
                delivered += 1
            except Exception:
                failed += 1
    finally:
        await bot.session.close()
    if delivered < 2:
        db.delete(election)
        db.rollback()
        raise HTTPException(409, "Не удалось доставить голосование минимум двум участникам команды")
    audit(db, actor, "captain_election.start", team, f"election={election.id}; delivered={delivered}; failed={failed}")
    db.commit()
    return go(event_id, "people")


@router.post("/events/{event_id}/teams/{team_id}/election/finish")
async def finish_captain_election(
    event_id: int, team_id: int, db: Session = Depends(get_db), actor=Depends(admin_auth)
):
    team = db.get(Team, team_id)
    election = db.scalar(select(CaptainElection).where(
        CaptainElection.team_id == team_id, CaptainElection.active.is_(True)
    ).order_by(CaptainElection.id.desc()))
    if not team or team.event_id != event_id or not election:
        raise HTTPException(404, "Активное голосование не найдено")
    counts = db.execute(
        select(CaptainVote.candidate_player_id, func.count(CaptainVote.id).label("votes"))
        .where(CaptainVote.election_id == election.id)
        .group_by(CaptainVote.candidate_player_id).order_by(func.count(CaptainVote.id).desc())
    ).all()
    if not counts:
        raise HTTPException(409, "Пока никто не проголосовал")
    if len(counts) > 1 and counts[0].votes == counts[1].votes:
        raise HTTPException(409, "Ничья: продолжите голосование или назначьте капитана вручную")
    winner = db.get(Player, counts[0].candidate_player_id)
    for player in team.players:
        if player.role != PlayerRole.ADMIN:
            player.role = PlayerRole.CAPTAIN if player.id == winner.id else PlayerRole.PLAYER
    election.active = False
    election.finished_at = datetime.utcnow()
    audit(db, actor, "captain_election.finish", team, f"winner={winner.id}; votes={counts[0].votes}")
    db.commit()
    recipients = [player for player in team.players if player.active and player.telegram_user_id]
    if recipients and get_settings().telegram_bot_token:
        bot = Bot(get_settings().telegram_bot_token)
        try:
            for player in recipients:
                try:
                    text = (
                        f"✅ Дауыс беру аяқталды.\nКоманда капитаны: {winner.full_name}"
                        if player.preferred_language == "KK" else
                        f"✅ Голосование завершено.\nКапитан команды: {winner.full_name}"
                    )
                    await bot.send_message(player.telegram_user_id, text)
                except Exception:
                    pass
        finally:
            await bot.session.close()
    return go(event_id, "people")


@router.post("/events/{event_id}/messages")
async def send_message(
    event_id: int,
    target_type: str = Form(...),
    message_text: str = Form(""),
    player_id: int | None = Form(None),
    team_id: int | None = Form(None),
    image_file: UploadFile | None = File(None),
    audio_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    actor: str = Depends(admin_auth),
):
    event = require_event(db, event_id)
    message_text = telegram_html(message_text.strip())
    token = get_settings().telegram_bot_token
    if not token:
        raise HTTPException(409, "TELEGRAM_BOT_TOKEN не настроен")
    bot = Bot(token)
    delivered, failed = 0, 0
    image_bytes: bytes | None = None
    image_name = "image.jpg"
    if image_file and image_file.filename:
        if not (image_file.content_type or "").startswith("image/"):
            raise HTTPException(400, "Можно загрузить только изображение")
        image_bytes = await image_file.read()
        if len(image_bytes) > 10_000_000:
            raise HTTPException(413, "Изображение больше 10 МБ")
        image_name = image_file.filename
    audio_bytes: bytes | None = None
    audio_name = "audio.mp3"
    if audio_file and audio_file.filename:
        if not (audio_file.content_type or "").startswith("audio/"):
            raise HTTPException(400, "Можно загрузить только аудиофайл")
        audio_bytes = await audio_file.read()
        if len(audio_bytes) > 20_000_000:
            raise HTTPException(413, "Аудиофайл больше 20 МБ")
        audio_name = audio_file.filename
    if not message_text and not image_bytes and not audio_bytes:
        raise HTTPException(400, "Добавьте текст, изображение или аудиофайл")
    targets: list[str] = []
    if target_type == "all":
        targets = [
            p.telegram_user_id for p in db.scalars(
                select(Player).join(Team).where(
                    Team.event_id == event_id,
                    Player.role != PlayerRole.ADMIN,
                    Player.active.is_(True),
                    Player.telegram_user_id.is_not(None),
                )
            ).all()
        ]
    elif target_type == "player":
        player = db.get(Player, player_id)
        if not player or not player.team or player.team.event_id != event_id or not player.telegram_user_id:
            raise HTTPException(400, "Участник не подключён к Telegram")
        targets = [player.telegram_user_id]
    elif target_type == "team_chat":
        team = db.get(Team, team_id)
        if not team or team.event_id != event_id or not team.telegram_chat_id:
            raise HTTPException(400, "У команды не указан Telegram chat ID")
        targets = [team.telegram_chat_id]
    else:
        raise HTTPException(400, "Неизвестный получатель")
    try:
        for chat_id in targets:
            try:
                text_sent = False
                if image_bytes:
                    photo = BufferedInputFile(image_bytes, filename=image_name)
                    if len(message_text) <= 1024:
                        await bot.send_photo(
                            chat_id=chat_id, photo=photo, caption=message_text, parse_mode="HTML"
                        )
                        text_sent = bool(message_text)
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=photo)
                        await bot.send_message(chat_id=chat_id, text=message_text, parse_mode="HTML")
                        text_sent = True
                if audio_bytes:
                    audio = BufferedInputFile(audio_bytes, filename=audio_name)
                    caption = message_text if message_text and not image_bytes and len(message_text) <= 1024 else None
                    await bot.send_audio(chat_id=chat_id, audio=audio, caption=caption, parse_mode="HTML")
                    text_sent = text_sent or bool(caption)
                if message_text and not image_bytes and not audio_bytes:
                    await bot.send_message(chat_id=chat_id, text=message_text, parse_mode="HTML")
                elif message_text and not text_sent and (image_bytes or audio_bytes):
                    await bot.send_message(chat_id=chat_id, text=message_text, parse_mode="HTML")
                delivered += 1
            except Exception:
                failed += 1
    finally:
        await bot.session.close()
    audit(db, actor, "message.send", event, f"target={target_type}; delivered={delivered}; failed={failed}")
    db.commit()
    return go(event_id, "messages")
