import asyncio
from contextlib import suppress
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import func, select

from .config import get_settings
from .database import SessionLocal
from .models import Answer, AnswerScope, CaptainElection, CaptainVote, DetectiveSubmission, Event, PendingRegistration, Player, PlayerRole, Question, QuestionType, Team, TeamQuestionPrompt
from .services import (
    GameError, active_detective_case, active_question, detective_clue_for_player,
    get_player_by_telegram, leaderboard,
    register_player, self_register_player, submit_answer,
    submit_detective_answer,
)
from .runtime_state import TEMPORARY_SENDERS
from .captain_elections import captain_election_watchdog, election_deadline, finalize_captain_election

router = Router()
BOT_RUNTIME = {"configured": False, "connected": False, "username": None, "error": None}


class InputState(StatesGroup):
    language = State()
    registration_code = State()
    registration_name = State()
    registration_confirm = State()
    registration_team = State()
    personal_answer = State()
    team_answer = State()
    team_explanation = State()
    team_confirm = State()


def main_keyboard(role: PlayerRole, language: str = "RU") -> ReplyKeyboardMarkup:
    kk = language == "KK"
    rows = [
        [
            KeyboardButton(text="👤 Менің профилім" if kk else "👤 Мой профиль"),
            KeyboardButton(text="🌐 Тіл / Язык"),
        ],
        [KeyboardButton(text="🕘 Жауаптар тарихы" if kk else "🕘 История ответов")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Команданы таңдаңыз" if kk else "Выберите действие",
    )


async def player_info(message: Message):
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(message.from_user.id))
        if not player:
            return None
        return {
            "id": player.id, "name": player.full_name, "role": player.role,
            "team_id": player.team_id, "team_name": player.team.name, "event_id": player.team.event_id,
            "event_name": player.team.event.name,
            "language": player.preferred_language,
        }


async def pending_info(message: Message):
    with SessionLocal() as db:
        pending = db.scalar(select(PendingRegistration).where(
            PendingRegistration.telegram_user_id == str(message.from_user.id)
        ))
        if not pending:
            return None
        return {
            "name": pending.full_name,
            "event_name": pending.event.name,
            "language": pending.preferred_language,
        }


async def show_pending_status(message: Message, pending: dict):
    text = (
        f"{pending['name']}, тіркелу аяқталды. Ұйымдастырушы сізді командаға бөлгеннен кейін бот ойын мәзірін ашады.\nІс-шара: {pending['event_name']}"
        if pending["language"] == "KK" else
        f"{pending['name']}, регистрация завершена. После распределения в команду организатором бот откроет игровое меню.\nМероприятие: {pending['event_name']}"
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


async def require_player(message: Message):
    info = await player_info(message)
    if not info:
        await message.answer("Сначала зарегистрируйтесь: нажмите /start.")
    return info


async def begin_registration(message: Message, state: FSMContext, pending_code: str | None = None):
    await state.clear()
    if pending_code:
        await state.update_data(pending_code=pending_code)
    await state.set_state(InputState.language)
    await message.answer(
        "Добро пожаловать! Сначала выберите язык.\nҚош келдіңіз! Алдымен тілді таңдаңыз.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Русский"), KeyboardButton(text="Қазақша")]],
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder="Выберите язык / Тілді таңдаңыз",
        ),
    )


@router.message(Command("bind_team"))
async def bind_team_chat(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Эту команду нужно отправить внутри беседы команды.")
        return
    if str(message.from_user.id) != get_settings().organizer_telegram_id:
        await message.answer("Привязывать командные беседы может только организатор.")
        return
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(message.from_user.id))
        if not player or not player.team:
            await message.answer("Сначала зарегистрируйтесь в боте через личные сообщения.")
            return
        team = player.team
        command_parts = (message.text or "").split(maxsplit=1)
        if len(command_parts) == 2:
            requested_code = command_parts[1].strip().upper()
            requested_team = db.scalar(select(Team).where(
                Team.event_id == team.event_id,
                func.upper(Team.code) == requested_code,
            ))
            if not requested_team:
                await message.answer("Команда с таким кодом не найдена.")
                return
            team = requested_team
        team.telegram_chat_id = str(message.chat.id)
        invite_created = False
        try:
            invite = await message.bot.create_chat_invite_link(
                chat_id=message.chat.id,
                name=f"{team.name} · интеллектуальная игра",
            )
            team.telegram_invite_url = invite.invite_link
            invite_created = True
        except Exception:
            pass
        db.commit()
        team_name = team.name
    suffix = " Ссылка-приглашение создана автоматически." if invite_created else " Ссылку-приглашение можно указать в админке."
    await message.answer(f"✅ Эта беседа привязана к команде «{team_name}».{suffix}")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    info = await player_info(message)
    if info:
        text = f"Сіз «{info['event_name']}» іс-шарасына қатысасыз." if info["language"] == "KK" else f"Вы участвуете в ивенте «{info['event_name']}»."
        await message.answer(text, reply_markup=main_keyboard(info["role"], info["language"]))
        return
    pending = await pending_info(message)
    if pending:
        await state.clear()
        await show_pending_status(message, pending)
        return
    await begin_registration(message, state)


@router.message(Command("register"))
async def register_command(message: Message, state: FSMContext):
    info = await player_info(message)
    if info:
        await message.answer(
            "Вы уже зарегистрированы.",
            reply_markup=main_keyboard(info["role"], info["language"]),
        )
        return
    pending = await pending_info(message)
    if pending:
        await show_pending_status(message, pending)
        return
    await begin_registration(message, state)


@router.message(F.text == "🌐 Тіл / Язык")
async def choose_language(message: Message, state: FSMContext):
    await state.set_state(InputState.language)
    await message.answer(
        "Выберите язык / Тілді таңдаңыз",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Русский"), KeyboardButton(text="Қазақша")]],
            resize_keyboard=True,
        ),
    )


@router.message(InputState.language, F.text.in_({"Русский", "Қазақша"}))
async def language_selected(message: Message, state: FSMContext):
    language = "KK" if message.text == "Қазақша" else "RU"
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(message.from_user.id))
        if player:
            player.preferred_language = language
            role = player.role
            db.commit()
        else:
            role = None
    if role:
        await state.clear()
        await message.answer(
            "Тіл өзгертілді." if language == "KK" else "Язык изменён.",
            reply_markup=main_keyboard(role, language),
        )
        return
    await state.update_data(language=language)
    with SessionLocal() as db:
        event = db.scalar(select(Event).order_by(Event.id))
    if not event:
        await state.clear()
        await message.answer(
            "Қазір белсенді іс-шара жоқ. Ұйымдастырушыға хабарласыңыз."
            if language == "KK" else
            "Сейчас нет активного мероприятия. Обратитесь к организатору."
        )
        return
    await state.update_data(event_id=event.id, event_name=event.name)
    await state.set_state(InputState.registration_name)
    await message.answer(
        f"Іс-шара: «{event.name}».\nАты-жөніңізді енгізіңіз."
        if language == "KK" else
        f"Мероприятие: «{event.name}».\nВведите имя и фамилию."
    )


async def process_registration_code(message: Message, state: FSMContext, code: str):
    clean = code.strip().upper()
    with SessionLocal() as db:
        personal = db.scalar(select(Player).where(func.upper(Player.registration_code) == clean))
        event = db.scalar(select(Event).where(func.upper(Event.registration_code) == clean, Event.active.is_(True)))
    if personal:
        try:
            with SessionLocal() as db:
                player = register_player(db, clean, str(message.from_user.id), message.from_user.username)
                data = await state.get_data()
                player.preferred_language = data.get("language", "RU")
                db.commit()
                name, role = player.full_name, player.role
            await state.clear()
            await message.answer(
                f"Готово, {name}! Вы зарегистрированы. Состав и загадку вашей команды организатор пришлёт перед началом игры.",
                reply_markup=main_keyboard(role, data.get("language", "RU")),
            )
        except GameError as exc:
            await message.answer(str(exc))
        return
    if event:
        await state.update_data(event_id=event.id, event_name=event.name)
        await state.set_state(InputState.registration_name)
        await message.answer(f"Ивент: «{event.name}».\nВведите имя и фамилию.")
        return
    await state.set_state(InputState.registration_code)
    await message.answer("Код не найден. Проверьте его и попробуйте ещё раз.")


@router.message(InputState.registration_code)
async def registration_code(message: Message, state: FSMContext):
    await process_registration_code(message, state, message.text or "")


@router.message(InputState.registration_name)
async def registration_name(message: Message, state: FSMContext):
    full_name = " ".join((message.text or "").split())
    if len(full_name) < 3:
        data = await state.get_data()
        await message.answer("Аты-жөніңізді мәтінмен енгізіңіз." if data.get("language") == "KK" else "Введите имя и фамилию текстом.")
        return
    data = await state.get_data()
    await state.update_data(full_name=full_name)
    await state.set_state(InputState.registration_confirm)
    kk = data.get("language") == "KK"
    await message.answer(
        (f"Енгізілген аты-жөні: {full_name}\n\nБарлығы дұрыс па?" if kk else
         f"Вы указали: {full_name}\n\nВсё правильно?"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="✅ Иә, дұрыс" if kk else "✅ Да, всё правильно")],
                      [KeyboardButton(text="✏️ Өзгерту" if kk else "✏️ Исправить имя")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(InputState.registration_confirm, F.text.in_({"✏️ Исправить имя", "✏️ Өзгерту"}))
async def registration_name_retry(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(InputState.registration_name)
    await message.answer(
        "Аты-жөніңізді қайта енгізіңіз." if data.get("language") == "KK" else "Введите имя и фамилию ещё раз.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(InputState.registration_confirm, F.text.in_({"✅ Да, всё правильно", "✅ Иә, дұрыс"}))
async def registration_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        with SessionLocal() as db:
            if get_player_by_telegram(db, str(message.from_user.id)):
                raise GameError("Этот Telegram-аккаунт уже зарегистрирован.")
            existing = db.scalar(select(PendingRegistration).where(
                PendingRegistration.telegram_user_id == str(message.from_user.id)
            ))
            if existing:
                raise GameError("Заявка этого Telegram-аккаунта уже создана.")
            pending = PendingRegistration(
                event_id=data["event_id"],
                full_name=data["full_name"],
                telegram_user_id=str(message.from_user.id),
                telegram_username=message.from_user.username,
                preferred_language=data.get("language", "RU"),
            )
            db.add(pending)
            db.commit()
        await state.clear()
        await show_pending_status(message, {
            "name": data["full_name"],
            "event_name": data["event_name"],
            "language": data.get("language", "RU"),
        })
    except GameError as exc:
        await state.clear()
        await message.answer(str(exc))


@router.message(InputState.registration_confirm)
async def registration_confirm_unknown(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer(
        "Төмендегі батырмалардың бірін таңдаңыз." if data.get("language") == "KK" else
        "Выберите одну из кнопок ниже."
    )


@router.message(InputState.registration_team)
async def registration_team(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        with SessionLocal() as db:
            player = self_register_player(
                db, data["event_id"], message.text or "", data["full_name"],
                str(message.from_user.id), message.from_user.username, data.get("language", "RU"),
            )
        await state.clear()
        text = (
            f"Тіркелу аяқталды!\nІс-шара: {data['event_name']}\nКомандаңыз бекітілді. Оның атауын жүргізушіге айтасыз."
            if data.get("language") == "KK" else
            f"Регистрация завершена!\nИвент: {data['event_name']}\nКоманда закреплена. Её название вы будете отгадывать и сообщите ведущему."
        )
        await message.answer(text, reply_markup=main_keyboard(PlayerRole.PLAYER, data.get("language", "RU")))
    except GameError as exc:
        await message.answer(f"{exc}\nВведите код команды ещё раз.")


@router.message(F.text.in_({"👤 Мой профиль", "👤 Менің профилім"}))
async def profile(message: Message):
    info = await require_player(message)
    if info:
        text = (
            f"{info['name']}\nІс-шара: {info['event_name']}\nКоманда бекітілді\nРөл: {info['role'].value}"
            if info["language"] == "KK" else
            f"{info['name']}\nИвент: {info['event_name']}\nКоманда закреплена\nРоль: {info['role'].value}"
        )
        await message.answer(text, reply_markup=main_keyboard(info["role"], info["language"]))


@router.message(F.text.in_({"🕘 История ответов", "🕘 Жауаптар тарихы"}))
async def answer_history(message: Message):
    info = await require_player(message)
    if not info:
        return
    with SessionLocal() as db:
        answers = db.scalars(
            select(Answer)
            .where(Answer.team_id == info["team_id"], Answer.scope == AnswerScope.TEAM)
            .order_by(Answer.submitted_at.desc())
            .limit(15)
        ).all()
        detective_answers = db.scalars(
            select(DetectiveSubmission)
            .where(DetectiveSubmission.team_id == info["team_id"])
            .order_by(DetectiveSubmission.submitted_at.desc())
            .limit(10)
        ).all()

        entries = []
        for answer in answers:
            respondent = answer.respondent.full_name if answer.respondent else info["name"]
            entries.append((
                answer.submitted_at,
                f"{answer.question.title}: {answer.text}",
                respondent,
            ))
        for submission in detective_answers:
            entries.append((
                submission.submitted_at,
                f"{submission.case.title_ru}: {submission.selected_option}",
                submission.captain.full_name,
            ))

    entries.sort(key=lambda item: item[0], reverse=True)
    entries = entries[:15]
    kk = info["language"] == "KK"
    if not entries:
        text = "Команда әлі жауап берген жоқ." if kk else "Команда пока не отправляла ответы."
    else:
        title = "Команданың соңғы жауаптары:" if kk else "Последние ответы команды:"
        lines = [title]
        for submitted_at, answer_text, respondent in entries:
            timestamp = submitted_at.strftime("%d.%m %H:%M")
            lines.append(f"\n{timestamp} · {respondent}\n{answer_text}")
        text = "\n".join(lines)
    await message.answer(text, reply_markup=main_keyboard(info["role"], info["language"]))


@router.message(F.text.in_({"📍 Текущий вопрос", "📍 Ағымдағы сұрақ"}))
async def status(message: Message):
    info = await require_player(message)
    if not info:
        return
    with SessionLocal() as db:
        question = active_question(db, info["event_id"])
        if question:
            title = question.title_kk if info["language"] == "KK" and question.title_kk else question.title
            text = question.text_kk if info["language"] == "KK" and question.text_kk else question.text
    await message.answer(
        f"{title}\n\n{text}" if question else ("Қазір ашық сұрақ жоқ." if info["language"] == "KK" else "Сейчас нет открытого вопроса."),
        reply_markup=main_keyboard(info["role"], info["language"]),
    )


@router.message(F.text.in_({"✍️ Ответить лично", "✍️ Жеке жауап"}))
async def personal_start(message: Message, state: FSMContext):
    info = await require_player(message)
    if info:
        await state.set_state(InputState.personal_answer)
        await message.answer("Жеке жауабыңызды бір хабарламамен енгізіңіз." if info["language"] == "KK" else "Введите ваш личный ответ одним сообщением.")


@router.message(InputState.personal_answer)
async def personal_submit(message: Message, state: FSMContext):
    await save_answer(message, state, AnswerScope.PERSONAL)


@router.message(F.text.in_({"📣 Ответ команды", "📣 Команда жауабы"}))
async def team_start(message: Message, state: FSMContext):
    info = await require_player(message)
    if info and info["role"] in {PlayerRole.CAPTAIN, PlayerRole.ADMIN}:
        await offer_respondent_selection(message, info["event_id"])


@router.callback_query(F.data.startswith("teamanswer:"))
async def team_answer_from_chat(callback: CallbackQuery):
    question_id = int(callback.data.split(":", 1)[1])
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(callback.from_user.id))
        question = active_question(db, player.team.event_id) if player else None
        temporary_sender = bool(
            player and question
            and TEMPORARY_SENDERS.get((question.id, player.team_id)) == player.id
        )
        if not player or (player.role not in {PlayerRole.CAPTAIN, PlayerRole.ADMIN} and not temporary_sender):
            await callback.answer("Ответ отправляет только капитан.", show_alert=True)
            return
        if (
            not question
            or question.id != question_id
            or player.team.event.display_mode != "SUBMISSION"
        ):
            await callback.answer("Приём ответа на этот вопрос уже закрыт.", show_alert=True)
            return
        event_id = player.team.event_id
    await offer_respondent_selection(callback.message, event_id, callback.from_user.id)
    await callback.answer()


async def offer_respondent_selection(message: Message, event_id: int, telegram_user_id: int | None = None):
    user_id = telegram_user_id or message.from_user.id
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(user_id))
        question = active_question(db, event_id)
        if not player or not question or player.team.event.display_mode != "SUBMISSION":
            await message.answer("Сейчас ответы не принимаются.")
            return
        teammates = db.scalars(
            select(Player).where(Player.team_id == player.team_id, Player.active.is_(True)).order_by(Player.full_name)
        ).all()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=teammate.full_name, callback_data=f"respondent:{question.id}:{teammate.id}")]
        for teammate in teammates
    ])
    await message.answer("Капитан, выберите участника, который будет отвечать от команды:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("respondent:"))
async def respondent_selected(callback: CallbackQuery, state: FSMContext):
    _, question_id, respondent_id = callback.data.split(":")
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(callback.from_user.id))
        question = active_question(db, player.team.event_id) if player else None
        temporary_sender = bool(
            player and question
            and TEMPORARY_SENDERS.get((question.id, player.team_id)) == player.id
        )
        if not player or (player.role not in {PlayerRole.CAPTAIN, PlayerRole.ADMIN} and not temporary_sender):
            await callback.answer("Отвечающего выбирает только капитан команды.", show_alert=True)
            return
        if not question or question.id != int(question_id) or player.team.event.display_mode != "SUBMISSION":
            await callback.answer("Время ответа уже закончилось.", show_alert=True)
            return
        options = __import__("json").loads(question.options_json or "[]")
        question_type = question.question_type
    await state.update_data(question_id=int(question_id), respondent_id=int(respondent_id))
    if question_type == QuestionType.TEXT:
        await state.set_state(InputState.team_answer)
        await callback.message.edit_text("Введите окончательный ответ команды. После отправки изменить его нельзя.")
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=option, callback_data=f"quizchoice:{question_id}:{index}")]
            for index, option in enumerate(options)
        ])
        await callback.message.edit_text("Выберите окончательный вариант:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("quizchoice:"))
async def quiz_choice_selected(callback: CallbackQuery, state: FSMContext):
    _, question_id, option_index = callback.data.split(":")
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(callback.from_user.id))
        question = active_question(db, player.team.event_id) if player else None
        if not question or question.id != int(question_id):
            await callback.answer("Вопрос уже закрыт.", show_alert=True)
            return
        option = __import__("json").loads(question.options_json)[int(option_index)]
        needs_explanation = question.question_type == QuestionType.CHOICE_EXPLANATION
    await state.update_data(selected_answer=option)
    if needs_explanation:
        await state.set_state(InputState.team_explanation)
        await callback.message.edit_text(f"Вы выбрали «{option}». Кратко напишите, как команда пришла к решению.")
    else:
        await offer_team_confirmation(callback.message, state, option, "", edit=True)
    await callback.answer()


@router.message(InputState.team_answer)
async def team_submit(message: Message, state: FSMContext):
    await offer_team_confirmation(message, state, message.text or "", "")


@router.message(InputState.team_explanation)
async def team_explanation_submit(message: Message, state: FSMContext):
    data = await state.get_data()
    await offer_team_confirmation(
        message, state, data.get("selected_answer", ""), message.text or ""
    )


async def offer_team_confirmation(
    message: Message, state: FSMContext, text: str, explanation: str, edit: bool = False,
):
    """Show the captain exactly what will be locked before the irreversible submit."""
    if not text.strip():
        await message.answer("Ответ не может быть пустым. Введите окончательный ответ команды:")
        return
    current = await state.get_data()
    with SessionLocal() as db:
        respondent = db.get(Player, current.get("respondent_id"))
        respondent_name = respondent.full_name if respondent else "не выбран"
    await state.update_data(pending_answer=text.strip(), pending_explanation=explanation.strip())
    await state.set_state(InputState.team_confirm)
    details = f"\n\nОбъяснение: {explanation.strip()}" if explanation.strip() else ""
    body = (
        "Проверьте ответ перед отправкой:\n\n"
        f"Отвечающий: {respondent_name}\n"
        f"«{text.strip()}»{details}\n\n"
        "После подтверждения изменить ответ будет нельзя."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить ответ", callback_data="teamconfirm:yes"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data="teamconfirm:edit"),
    ]])
    if edit:
        await message.edit_text(body, reply_markup=keyboard)
    else:
        await message.answer(body, reply_markup=keyboard)


@router.callback_query(InputState.team_confirm, F.data == "teamconfirm:yes")
async def confirm_team_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await submit_team_from_state(
        callback.message, state, callback.from_user.id,
        data.get("pending_answer", ""), data.get("pending_explanation", ""),
    )
    await callback.answer()


@router.callback_query(InputState.team_confirm, F.data == "teamconfirm:edit")
async def edit_team_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    with SessionLocal() as db:
        question = db.get(Question, data.get("question_id"))
        question_type = question.question_type if question else QuestionType.TEXT
        options = __import__("json").loads(question.options_json or "[]") if question else []
    if question_type == QuestionType.TEXT:
        await state.set_state(InputState.team_answer)
        await callback.message.edit_text("Введите исправленный окончательный ответ команды:")
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=option, callback_data=f"quizchoice:{data['question_id']}:{index}")]
            for index, option in enumerate(options)
        ])
        await state.set_state(InputState.team_answer)
        await callback.message.edit_text("Выберите вариант заново:", reply_markup=keyboard)
    await callback.answer()


async def submit_team_from_state(message: Message, state: FSMContext, telegram_user_id: int, text: str, explanation: str):
    try:
        data = await state.get_data()
        with SessionLocal() as db:
            player = get_player_by_telegram(db, str(telegram_user_id))
            answer = submit_answer(
                db, player, text, AnswerScope.TEAM,
                respondent_player_id=data.get("respondent_id"), explanation=explanation,
            )
            prompt = db.scalar(select(TeamQuestionPrompt).where(
                TeamQuestionPrompt.question_id == answer.question_id,
                TeamQuestionPrompt.team_id == answer.team_id,
            ))
            prompt_data = None if not prompt else (
                prompt.telegram_chat_id, int(prompt.telegram_message_id)
            )
            question_title = answer.question.title
            respondent = db.get(Player, answer.respondent_player_id) if answer.respondent_player_id else None
            respondent_name = respondent.full_name if respondent else "не указан"
            current_chat_id = str(message.chat.id)
            current_message_id = int(message.message_id)
            if prompt:
                prompt.telegram_chat_id = current_chat_id
                prompt.telegram_message_id = str(current_message_id)
            else:
                db.add(TeamQuestionPrompt(
                    question_id=answer.question_id, team_id=answer.team_id,
                    telegram_chat_id=current_chat_id, telegram_message_id=str(current_message_id),
                ))
            db.commit()
        explanation_line = f"\nОбъяснение: {answer.explanation}" if answer.explanation else ""
        confirmation = (
            f"✅ Ваш ответ на вопрос «{question_title}» зафиксирован.\n\n"
            f"Отвечающий: {respondent_name}\n"
            f"Ответ: {answer.text}{explanation_line}\n\n"
            "Изменить ответ нельзя."
        )
        try:
            await message.edit_text(confirmation, reply_markup=None)
        except Exception:
            await message.answer(confirmation)
        if prompt_data and (
            str(prompt_data[0]) != str(message.chat.id) or int(prompt_data[1]) != int(message.message_id)
        ):
            try:
                await message.bot.delete_message(prompt_data[0], prompt_data[1])
            except Exception:
                pass
    except GameError as exc:
        await message.answer(str(exc))
    finally:
        await state.clear()


async def save_answer(message: Message, state: FSMContext, scope: AnswerScope):
    try:
        with SessionLocal() as db:
            player = get_player_by_telegram(db, str(message.from_user.id))
            if not player:
                raise GameError("Сначала зарегистрируйтесь.")
            answer = submit_answer(db, player, message.text or "", scope)
        await message.answer(f"Ответ принят: «{answer.text}». Изменить его нельзя.")
    except GameError as exc:
        await message.answer(str(exc))
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("captainvote:"))
async def captain_vote(callback: CallbackQuery):
    _, election_id, candidate_id = callback.data.split(":")
    winner_name = None
    candidate_name = None
    vote_count = eligible_count = 0
    with SessionLocal() as db:
        voter = get_player_by_telegram(db, str(callback.from_user.id))
        election = db.get(CaptainElection, int(election_id))
        candidate = db.get(Player, int(candidate_id))
        if (
            not voter or not election or not election.active or voter.team_id != election.team_id
            or election_deadline(election) <= datetime.utcnow()
        ):
            await callback.answer("Вы не можете голосовать в этой команде.", show_alert=True)
            return
        if not candidate or candidate.team_id != election.team_id or not candidate.active:
            await callback.answer("Кандидат недоступен.", show_alert=True)
            return
        if candidate.id == voter.id:
            await callback.answer(
                "Нельзя голосовать за себя. Выберите другого участника команды.",
                show_alert=True,
            )
            return
        vote = db.scalar(select(CaptainVote).where(
            CaptainVote.election_id == election.id, CaptainVote.voter_player_id == voter.id
        ))
        if vote:
            await callback.answer(
                "Вы уже проголосовали. Изменить голос нельзя.",
                show_alert=True,
            )
            return
        candidate_name = candidate.full_name
        db.add(CaptainVote(
            election_id=election.id, voter_player_id=voter.id, candidate_player_id=candidate.id
        ))
        db.flush()
        eligible_count = db.scalar(select(func.count(Player.id)).where(
            Player.team_id == election.team_id, Player.active.is_(True), Player.telegram_user_id.is_not(None)
        )) or 0
        vote_count = db.scalar(select(func.count(CaptainVote.id)).where(
            CaptainVote.election_id == election.id
        )) or 0
        finish_now = vote_count >= eligible_count
        db.commit()
    if finish_now:
        winner_name = await finalize_captain_election(int(election_id), callback.bot, "all_votes_received")
    await callback.answer(f"Ваш голос принят: {candidate_name}", show_alert=True)
    if callback.message:
        try:
            if winner_name:
                await callback.message.edit_text(
                    f"✅ Голосование завершено.\nКапитан команды: {winner_name}",
                    reply_markup=None,
                )
            else:
                suffix = (
                    "\n\nПолучилась ничья. Организатор выберет капитана вручную."
                    if vote_count >= eligible_count else ""
                )
                await callback.message.edit_text(
                    "Выберите капитана команды. Голосовать за себя нельзя — "
                    "выберите другого участника.\n\n"
                    f"Проголосовало: {vote_count} из {eligible_count}.{suffix}",
                    reply_markup=callback.message.reply_markup,
                )
        except Exception:
            # Голос уже сохранён; ошибка обновления сообщения не должна его отменять.
            pass


@router.message(F.text.in_({"🕵️ Моя улика", "🕵️ Менің айғағым"}))
async def detective_clue(message: Message):
    info = await require_player(message)
    if not info:
        return
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(message.from_user.id))
        clue = detective_clue_for_player(db, player)
        case = clue.case if clue else None
        if clue:
            text = clue.text_kk if info["language"] == "KK" and clue.text_kk else clue.text_ru
            story = case.story_kk if info["language"] == "KK" and case.story_kk else case.story_ru
    if not clue:
        await message.answer("Сейчас для вас нет активной детективной игры.")
        return
    await message.answer(f"🕵️ {case.title_ru}\n\n{story}\n\nВаша личная улика:\n{text}")


@router.message(F.text.in_({"🔐 Ответ детектива", "🔐 Детектив жауабы"}))
async def detective_options(message: Message):
    info = await require_player(message)
    if not info:
        return
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(message.from_user.id))
        case = active_detective_case(db, player)
        if not case:
            await message.answer("Сейчас детективная игра не запущена.")
            return
        if case.submission:
            await message.answer("Ответ вашей команды уже зафиксирован и не может быть изменён.")
            return
        options = __import__("json").loads(case.options_json)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=option, callback_data=f"detective:pick:{case.id}:{index}")]
        for index, option in enumerate(options)
    ])
    await message.answer("Выберите окончательный ответ команды. После выбора потребуется подтверждение.", reply_markup=keyboard)


@router.callback_query(F.data.startswith("detective:pick:"))
async def detective_pick(callback: CallbackQuery):
    _, _, case_id, option_index = callback.data.split(":")
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(callback.from_user.id))
        case = active_detective_case(db, player) if player else None
        if not case or case.id != int(case_id):
            await callback.answer("Игра уже недоступна.", show_alert=True)
            return
        options = __import__("json").loads(case.options_json)
        option = options[int(option_index)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, отправить навсегда", callback_data=f"detective:confirm:{case_id}:{option_index}")],
        [InlineKeyboardButton(text="Нет, вернуться", callback_data=f"detective:back:{case_id}")],
    ])
    await callback.message.edit_text(
        f"Вы выбрали: «{option}».\n\nОтвет можно отправить только один раз и изменить его будет нельзя. Вы уверены?",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("detective:back:"))
async def detective_back(callback: CallbackQuery):
    case_id = int(callback.data.split(":")[-1])
    with SessionLocal() as db:
        player = get_player_by_telegram(db, str(callback.from_user.id))
        case = active_detective_case(db, player) if player else None
        if not case or case.id != case_id:
            await callback.answer("Игра уже недоступна.", show_alert=True)
            return
        options = __import__("json").loads(case.options_json)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=option, callback_data=f"detective:pick:{case.id}:{index}")]
        for index, option in enumerate(options)
    ])
    await callback.message.edit_text(
        "Выберите окончательный ответ команды. После выбора потребуется подтверждение.",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("detective:confirm:"))
async def detective_confirm(callback: CallbackQuery):
    _, _, case_id, option_index = callback.data.split(":")
    try:
        with SessionLocal() as db:
            player = get_player_by_telegram(db, str(callback.from_user.id))
            case = active_detective_case(db, player) if player else None
            if not case or case.id != int(case_id):
                raise GameError("Детективная игра уже недоступна.")
            option = __import__("json").loads(case.options_json)[int(option_index)]
            submission = submit_detective_answer(db, player, option)
        await callback.message.edit_text(
            f"Ответ «{option}» зафиксирован.\nИзменить или отправить другой ответ больше нельзя."
        )
        await callback.answer("Ответ принят")
    except GameError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.message(F.text == "🏆 Рейтинг")
async def leaderboard_message(message: Message):
    info = await require_player(message)
    if not info:
        return
    with SessionLocal() as db:
        board = leaderboard(db, info["event_id"])
    lines = ["Командный рейтинг:"]
    lines.extend(f"{i}. {team['name']} — {team['points']:g}" for i, team in enumerate(board["teams"], 1))
    await message.answer("\n".join(lines))


@router.message(StateFilter(None))
async def first_contact_or_unknown_message(message: Message, state: FSMContext):
    """Never leave a first-time user without an onboarding response."""
    info = await player_info(message)
    if not info:
        pending = await pending_info(message)
        if pending:
            await show_pending_status(message, pending)
            return
        await begin_registration(message, state)
        return
    await message.answer(
        "Выберите действие в меню." if info["language"] != "KK" else "Мәзірден әрекетті таңдаңыз.",
        reply_markup=main_keyboard(info["role"], info["language"]),
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def run_bot(token: str) -> None:
    BOT_RUNTIME.update(configured=bool(token), connected=False, username=None, error=None)
    bot = Bot(token)
    try:
        me = await bot.get_me()
        BOT_RUNTIME.update(connected=True, username=me.username, error=None)
        watchdog = asyncio.create_task(captain_election_watchdog(bot))
        try:
            await build_dispatcher().start_polling(bot)
        finally:
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog
    except Exception as exc:
        BOT_RUNTIME.update(connected=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await bot.session.close()
