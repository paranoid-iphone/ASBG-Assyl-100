import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.captain_elections import finalize_captain_election
from app.database import Base, SessionLocal, engine
from app.models import CaptainElection, CaptainVote, Event, Player, PlayerRole, Team


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, telegram_user_id, text):
        self.messages.append((str(telegram_user_id), text))


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_tied_vote_is_finished_and_every_player_is_notified():
    with SessionLocal() as db:
        event = Event(name="Election")
        db.add(event); db.flush()
        team = Team(event_id=event.id, name="Alpha", code="alpha")
        db.add(team); db.flush()
        first = Player(
            team_id=team.id, full_name="First", registration_code="first",
            telegram_user_id="101", role=PlayerRole.PLAYER,
        )
        second = Player(
            team_id=team.id, full_name="Second", registration_code="second",
            telegram_user_id="102", role=PlayerRole.PLAYER,
        )
        db.add_all([first, second]); db.flush()
        election = CaptainElection(
            team_id=team.id, expires_at=datetime.utcnow() - timedelta(seconds=1)
        )
        db.add(election); db.flush()
        db.add_all([
            CaptainVote(election_id=election.id, voter_player_id=first.id, candidate_player_id=second.id),
            CaptainVote(election_id=election.id, voter_player_id=second.id, candidate_player_id=first.id),
        ])
        db.commit()
        election_id = election.id

    bot = FakeBot()
    winner = asyncio.run(finalize_captain_election(election_id, bot))

    assert winner in {"First", "Second"}
    assert len(bot.messages) == 2
    assert all(winner in text for _, text in bot.messages)
    with SessionLocal() as db:
        election = db.get(CaptainElection, election_id)
        players = list(db.scalars(select(Player).where(Player.team_id == election.team_id)))
        assert election.active is False
        assert len([player for player in players if player.role == PlayerRole.CAPTAIN]) == 1
