import asyncio
import secrets
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select

from .database import SessionLocal
from .models import CaptainElection, CaptainVote, Player, PlayerRole
from .services import audit


ELECTION_DURATION_SECONDS = 60


def election_deadline(election: CaptainElection) -> datetime:
    return election.expires_at or (election.created_at + timedelta(seconds=ELECTION_DURATION_SECONDS))


async def finalize_captain_election(election_id: int, bot: Bot, actor: str = "timer") -> str | None:
    """Finish one election deterministically except for a fair random tie break."""
    recipients: list[tuple[str, str]] = []
    winner_name: str | None = None
    team_name = ""
    with SessionLocal() as db:
        election = db.get(CaptainElection, election_id)
        if not election or not election.active:
            return None
        team = election.team
        team_name = team.name
        candidates = [player for player in team.players if player.active and player.telegram_user_id]
        if not candidates:
            election.active = False
            election.finished_at = datetime.utcnow()
            audit(db, actor, "captain_election.finish_empty", team, f"election={election.id}")
            db.commit()
            return None

        counts = db.execute(
            select(CaptainVote.candidate_player_id, func.count(CaptainVote.id).label("votes"))
            .where(CaptainVote.election_id == election.id)
            .group_by(CaptainVote.candidate_player_id)
        ).all()
        if counts:
            maximum = max(row.votes for row in counts)
            leaders = [row.candidate_player_id for row in counts if row.votes == maximum]
            winner = db.get(Player, secrets.choice(leaders))
            tie = len(leaders) > 1
        else:
            maximum = 0
            winner = secrets.choice(candidates)
            tie = len(candidates) > 1

        for player in team.players:
            if player.role != PlayerRole.ADMIN:
                player.role = PlayerRole.CAPTAIN if player.id == winner.id else PlayerRole.PLAYER
        election.active = False
        election.finished_at = datetime.utcnow()
        winner_name = winner.full_name
        recipients = [
            (player.telegram_user_id, player.preferred_language)
            for player in team.players if player.active and player.telegram_user_id
        ]
        audit(
            db, actor, "captain_election.finish", team,
            f"winner={winner.id}; votes={maximum}; tie_break={tie}; election={election.id}",
        )
        db.commit()

    for telegram_user_id, language in recipients:
        text = (
            f"✅ Дауыс беру аяқталды.\n«{team_name}» командасының капитаны: {winner_name}"
            if language == "KK" else
            f"✅ Голосование завершено.\nКапитан команды «{team_name}»: {winner_name}"
        )
        try:
            await bot.send_message(telegram_user_id, text)
        except Exception:
            pass
    return winner_name


async def captain_election_watchdog(bot: Bot) -> None:
    """Survives restarts because deadlines and votes are stored in the database."""
    while True:
        try:
            now = datetime.utcnow()
            with SessionLocal() as db:
                active = db.scalars(select(CaptainElection).where(CaptainElection.active.is_(True))).all()
                expired_ids = [election.id for election in active if election_deadline(election) <= now]
            for election_id in expired_ids:
                await finalize_captain_election(election_id, bot)
        except Exception:
            # A temporary database/Telegram problem must not permanently stop future elections.
            pass
        await asyncio.sleep(2)
