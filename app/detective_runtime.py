from datetime import datetime

from aiogram import Bot
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .models import (
    DetectiveCase, DetectiveClue, DetectiveStatus, Event, Stage, StageType,
)
from .services import audit
from .runtime_state import mark_clue_delivery


def clue_message(clue: DetectiveClue) -> str:
    case = clue.case
    player = clue.player
    kk = player.preferred_language == "KK"
    title = case.title_kk if kk and case.title_kk else case.title_ru
    story = case.story_kk if kk and case.story_kk else case.story_ru
    clue_text = clue.text_kk if kk and clue.text_kk else clue.text_ru
    personal_label = "Сіздің жеке айғағыңыз" if kk else "Ваша личная улика"
    instruction = (
        "Оны командамен талқылаңыз. Соңғы жауапты капитан жібереді."
        if kk else
        "Обсудите её с командой. Окончательный ответ отправляет капитан."
    )
    return f"🕵️ {title}\n\n{story}\n\n{personal_label}:\n{clue_text}\n\n{instruction}"


async def send_detective_clue(bot: Bot, stage_id: int, clue: DetectiveClue) -> bool:
    if not clue.player.telegram_user_id:
        mark_clue_delivery(stage_id, clue.player.id, "failed", "Telegram не подключён")
        return False
    try:
        await bot.send_message(clue.player.telegram_user_id, clue_message(clue))
        mark_clue_delivery(stage_id, clue.player.id, "delivered")
        return True
    except Exception as exc:
        mark_clue_delivery(stage_id, clue.player.id, "failed", str(exc))
        return False


def prepared_cases(db: Session, event: Event, stage: Stage) -> list[DetectiveCase]:
    cases = db.scalars(
        select(DetectiveCase)
        .options(selectinload(DetectiveCase.clues).selectinload(DetectiveClue.player))
        .where(DetectiveCase.stage_id == stage.id, DetectiveCase.approved.is_(True))
    ).all()
    active_team_count = len([team for team in event.teams if team.active])
    if len(cases) != active_team_count:
        raise HTTPException(
            409,
            f"Этап «{stage.title}» не готов: создайте и проверьте кейсы для всех активных команд.",
        )
    return cases


async def start_detective_stage(
    db: Session, event: Event, stage: Stage, actor: str = "screen"
) -> None:
    if stage.stage_type != StageType.DETECTIVE:
        raise HTTPException(400, "Выбранный этап не является детективной игрой")
    cases = prepared_cases(db, event, stage)
    stage.detective_status = DetectiveStatus.RUNNING
    stage.detective_started_at = datetime.utcnow()
    event.current_detective_stage_id = stage.id
    event.current_question_id = None
    event.display_mode = "DETECTIVE"
    event.timer_duration_seconds = stage.detective_duration_seconds
    event.timer_started_at = stage.detective_started_at
    audit(db, actor, "detective.start", stage, f"cases={len(cases)}")
    db.commit()

    token = get_settings().telegram_bot_token
    if not token:
        return
    bot = Bot(token)
    try:
        for case in cases:
            for clue in case.clues:
                await send_detective_clue(bot, stage.id, clue)
    finally:
        await bot.session.close()


def finish_detective_stage(
    db: Session, event: Event, stage: Stage, actor: str = "screen"
) -> None:
    stage.detective_status = DetectiveStatus.FINISHED
    event.timer_started_at = None
    event.current_detective_stage_id = None
    audit(db, actor, "detective.finish", stage)
    db.commit()
