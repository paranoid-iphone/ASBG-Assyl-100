import asyncio
from datetime import datetime
import json

from app.admin import finish_live_detective, launch_single_stage, live_control_state
from app.database import Base, SessionLocal, engine
from app.detective import generate_cases_for_stage
from app.detective_runtime import detective_answer_markup, send_detective_answer_panel, send_detective_clue
from app.models import (
    DetectiveStatus, Event, Player, PlayerRole, Stage, StageType, Team,
)
from app.services import leaderboard, submit_detective_answer


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def create_team_with_players(db, event, name, code, player_count=10):
    team = Team(event_id=event.id, name=name, code=code)
    db.add(team)
    db.flush()
    for index in range(player_count):
        db.add(Player(
            team_id=team.id,
            full_name=f"{name} {index + 1}",
            registration_code=f"{code}-{index + 1}",
            role=PlayerRole.CAPTAIN if index == 0 else PlayerRole.PLAYER,
        ))
    db.flush()
    return team


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


def test_generator_creates_shared_case_and_one_clue_per_player():
    with SessionLocal() as db:
        event = Event(name="Detective")
        db.add(event)
        db.flush()
        first = create_team_with_players(db, event, "One", "ONE", 8)
        second = create_team_with_players(db, event, "Two", "TWO", 9)
        third = create_team_with_players(db, event, "Three", "THREE", 10)
        stage = Stage(
            event_id=event.id, title="Cases", stage_type=StageType.DETECTIVE,
            detective_status=DetectiveStatus.DRAFT,
        )
        db.add(stage)
        db.flush()
        cases = generate_cases_for_stage(db, stage)
        assert len(cases) == 3
        assert {case.team_id for case in cases} == {first.id, second.id, third.id}
        assert len({case.fingerprint for case in cases}) == 3
        assert {case.title_ru for case in cases} == {"Дело о пропавшем прототипе"}
        assert {case.story_ru for case in cases}.__len__() == 1
        assert {case.correct_option for case in cases} == {"Виктор"}
        all_clues = [clue for case in cases for clue in case.clues]
        assert len(all_clues) == 27
        assert sorted(len(case.clues) for case in cases) == [8, 9, 10]
        for case in cases:
            packets = [json.loads(clue.predicate_json)["clues"] for clue in case.clues]
            assert all(len(packet) >= 1 for packet in packets)
            assert sorted(number for packet in packets for number in packet) == list(range(1, 11))
            assert sorted(len(packet) for packet in packets) == ([1] * (2 * len(case.clues) - 10) + [2] * (10 - len(case.clues)))


def test_detective_can_be_rehearsed_with_one_or_two_players():
    with SessionLocal() as db:
        event = Event(name="Small rehearsal")
        db.add(event); db.flush()
        create_team_with_players(db, event, "Solo", "SOLO", 1)
        create_team_with_players(db, event, "Pair", "PAIR", 2)
        stage = Stage(event_id=event.id, title="Cases", stage_type=StageType.DETECTIVE)
        db.add(stage); db.flush()

        cases = generate_cases_for_stage(db, stage)
        packet_sizes = {
            case.team.name: sorted(len(json.loads(clue.predicate_json)["clues"]) for clue in case.clues)
            for case in cases
        }
        assert packet_sizes["Solo"] == [10]
        assert packet_sizes["Pair"] == [5, 5]
        for case in cases:
            numbers = [
                number
                for clue in case.clues
                for number in json.loads(clue.predicate_json)["clues"]
            ]
            assert sorted(numbers) == list(range(1, 11))


def test_detective_answer_is_single_use_and_ranked():
    with SessionLocal() as db:
        event = Event(name="Detective")
        db.add(event)
        db.flush()
        team = create_team_with_players(db, event, "One", "ONE")
        stage = Stage(
            event_id=event.id, title="Cases", stage_type=StageType.DETECTIVE,
            detective_status=DetectiveStatus.RUNNING, detective_started_at=datetime.utcnow(),
        )
        db.add(stage)
        db.flush()
        case = generate_cases_for_stage(db, stage)[0]
        db.commit()
        captain = next(player for player in team.players if player.role == PlayerRole.CAPTAIN)
        submission = submit_detective_answer(db, captain, case.correct_option)
        assert submission.is_correct is True
        assert submission.rank == 1
        assert submission.points_awarded == 30
        assert leaderboard(db, event.id)["teams"][0]["points"] == 30


def test_single_detective_launch_skips_full_game_prologue():
    with SessionLocal() as db:
        event = Event(name="Detective")
        db.add(event); db.flush()
        create_team_with_players(db, event, "One", "ONE", 2)
        stage = Stage(
            event_id=event.id, title="Cases", stage_type=StageType.DETECTIVE,
            detective_status=DetectiveStatus.DRAFT,
        )
        db.add(stage); db.flush()
        generate_cases_for_stage(db, stage)
        stage.detective_status = DetectiveStatus.READY
        db.commit()

        asyncio.run(launch_single_stage(event.id, stage.id, db, "test"))
        db.refresh(event)
        assert event.display_mode == "STAGE_INTRO"
        assert event.current_detective_stage_id == stage.id
        assert event.current_slide_id is None


def test_captain_gets_options_and_admin_can_finish_detective_early():
    with SessionLocal() as db:
        event = Event(name="Detective", display_mode="DETECTIVE")
        db.add(event); db.flush()
        create_team_with_players(db, event, "One", "ONE", 2)
        stage = Stage(
            event_id=event.id, title="Cases", stage_type=StageType.DETECTIVE,
            detective_status=DetectiveStatus.RUNNING, detective_started_at=datetime.utcnow(),
        )
        db.add(stage); db.flush()
        case = generate_cases_for_stage(db, stage)[0]
        captain = next(player for player in case.team.players if player.role == PlayerRole.CAPTAIN)
        captain.telegram_user_id = "10001"
        event.current_detective_stage_id = stage.id
        db.commit()

        markup = detective_answer_markup(case)
        assert [button.text for row in markup.inline_keyboard for button in row] == json.loads(case.options_json)
        assert all(
            button.callback_data.startswith(f"detective:pick:{case.id}:")
            for row in markup.inline_keyboard for button in row
        )
        fake_bot = FakeBot()
        case.clues[0].player.telegram_user_id = None
        assert asyncio.run(send_detective_clue(fake_bot, stage.id, case.clues[0])) is False
        case.clues[0].player.telegram_user_id = "10002"
        assert asyncio.run(send_detective_clue(fake_bot, stage.id, case.clues[0])) is True
        assert asyncio.run(send_detective_answer_panel(fake_bot, case, captain)) is True
        assert fake_bot.messages[-1][2] is not None

        state = live_control_state(event.id, db, "test")
        assert state["detective"]["materials"]["correct_answer"] == case.correct_option
        assert state["detective"]["materials"]["clues"]

        finish_live_detective(event.id, db, "test")
        db.refresh(event); db.refresh(stage)
        assert stage.detective_status == DetectiveStatus.FINISHED
        assert event.display_mode == "STAGE_COMPLETE"
        assert event.current_detective_stage_id == stage.id
