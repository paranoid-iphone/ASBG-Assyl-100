from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Event, Player, PlayerRole, Question, Team, TeamQuestionPrompt
from .runtime_state import mark_team_delivery


async def notify_team_chats(
    db: Session, event: Event, question: Question, mode: str, team_id: int | None = None
) -> tuple[int, int]:
    """Keep one private answer prompt for each team's captain."""
    token = get_settings().telegram_bot_token
    if not token:
        return 0, 0
    teams = db.scalars(
        select(Team).where(
            Team.event_id == event.id,
            Team.active.is_(True),
            *((Team.id == team_id,) if team_id is not None else ()),
        )
    ).all()
    if not teams:
        return 0, 0

    if mode == "QUESTION":
        # The question is intentionally shown and read only once on the big screen.
        return 0, 0
    if mode not in {"SUBMISSION", "ANSWER"}:
        return 0, 0

    delivered = failed = 0
    bot = Bot(token)
    try:
        for team in teams:
            try:
                captain = db.scalar(select(Player).where(
                    Player.team_id == team.id,
                    Player.active.is_(True),
                    Player.role == PlayerRole.CAPTAIN,
                    Player.telegram_user_id.is_not(None),
                ))
                if not captain:
                    failed += 1
                    mark_team_delivery(question.id, team.id, "failed", "captain is not connected")
                    continue
                old_prompt = db.scalar(select(TeamQuestionPrompt).where(
                    TeamQuestionPrompt.question_id == question.id,
                    TeamQuestionPrompt.team_id == team.id,
                ))
                if mode == "ANSWER":
                    if old_prompt:
                        try:
                            await bot.delete_message(old_prompt.telegram_chat_id, int(old_prompt.telegram_message_id))
                        finally:
                            db.delete(old_prompt)
                            db.commit()
                    delivered += 1
                    mark_team_delivery(question.id, team.id, "closed")
                    continue

                teammates = db.scalars(select(Player).where(
                    Player.team_id == team.id,
                    Player.active.is_(True),
                ).order_by(Player.full_name)).all()
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=player.full_name,
                        callback_data=f"respondent:{question.id}:{player.id}",
                    )]
                    for player in teammates
                ])
                text = (
                    f"⏱ Жауап қабылдау ашылды. {question.submission_seconds} секунд қалды.\n\n"
                    "Капитан, команда атынан жауап беретін қатысушыны таңдаңыз:"
                    if captain.preferred_language == "KK" else
                    f"⏱ Открыт приём ответа. Осталось {question.submission_seconds} секунд.\n\n"
                    "Капитан, выберите участника, который будет отвечать от команды:"
                )
                if old_prompt:
                    await bot.edit_message_text(
                        text, old_prompt.telegram_chat_id, int(old_prompt.telegram_message_id), reply_markup=keyboard
                    )
                else:
                    sent = await bot.send_message(captain.telegram_user_id, text, reply_markup=keyboard)
                    db.add(TeamQuestionPrompt(
                        question_id=question.id,
                        team_id=team.id,
                        telegram_chat_id=str(captain.telegram_user_id),
                        telegram_message_id=str(sent.message_id),
                    ))
                    db.commit()
                delivered += 1
                mark_team_delivery(question.id, team.id, "delivered")
            except Exception as exc:
                failed += 1
                mark_team_delivery(question.id, team.id, "failed", str(exc))
    finally:
        await bot.session.close()
    return delivered, failed
