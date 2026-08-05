from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import Player


COMMUNICATION_QUESTION_RU = (
    "Кто был самым коммуникативным в команде, чьи решения и идеи помогали "
    "выполнять задания легко и качественно?"
)
COMMUNICATION_QUESTION_KK = (
    "Командаңызда ең белсенді болып, шешімдер мен идеялар ұсынып әрі "
    "сапалы орындауға көмектескен кім?"
)
FEEDBACK_QUESTION_RU = "Оцените мероприятие и оставьте отзыв — нам будет приятно."
FEEDBACK_QUESTION_KK = "Іс-шараны бағалап, пікіріңізді қалдырыңыз. Пікіріңіз біз үшін маңызды!"


def communication_markup(event_id: int, voter: Player, teammates: list[Player]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=candidate.full_name,
            callback_data=f"commvote:{event_id}:{candidate.id}",
        )]
        for candidate in teammates
        if candidate.id != voter.id
    ])


def feedback_markup(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{rating} {'⭐' * rating}", callback_data=f"eventrating:{event_id}:{rating}")
        for rating in range(1, 6)
    ]])
