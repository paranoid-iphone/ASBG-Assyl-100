from datetime import datetime

from app.database import Base, SessionLocal, engine
from app.detective import generate_cases_for_stage, validate_predicates
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


def test_generator_creates_unique_essential_clue_per_player():
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
        all_clues = [clue for case in cases for clue in case.clues]
        assert len(all_clues) == 27
        assert sorted(len(case.clues) for case in cases) == [8, 9, 10]
        assert len({clue.text_ru for clue in all_clues}) == 27
        for case in cases:
            report = validate_predicates([
                __import__("json").loads(clue.predicate_json) for clue in case.clues
            ])
            assert report["unique_solution"] is True
            assert report["all_clues_essential"] is True


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
