from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import CommunicationVote, Event, EventFeedback, Player, Team
from app.surveys import (
    COMMUNICATION_QUESTION_KK, COMMUNICATION_QUESTION_RU,
    FEEDBACK_QUESTION_KK, FEEDBACK_QUESTION_RU,
    communication_markup, feedback_markup,
)


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_survey_keyboards_and_bilingual_copy():
    voter = Player(id=1, full_name="Voter", registration_code="VOTER")
    candidate = Player(id=2, full_name="Candidate", registration_code="CANDIDATE")
    peer_markup = communication_markup(7, voter, [voter, candidate])
    assert len(peer_markup.inline_keyboard) == 1
    assert peer_markup.inline_keyboard[0][0].callback_data == "commvote:7:2"
    rating_markup = feedback_markup(7)
    assert [button.callback_data for button in rating_markup.inline_keyboard[0]] == [
        f"eventrating:7:{rating}" for rating in range(1, 6)
    ]
    assert COMMUNICATION_QUESTION_RU and COMMUNICATION_QUESTION_KK
    assert FEEDBACK_QUESTION_RU and FEEDBACK_QUESTION_KK


def test_survey_results_are_persisted():
    with SessionLocal() as db:
        event = Event(name="Survey")
        db.add(event); db.flush()
        team = Team(event_id=event.id, name="One", code="ONE")
        db.add(team); db.flush()
        voter = Player(team_id=team.id, full_name="Voter", registration_code="VOTER")
        candidate = Player(team_id=team.id, full_name="Candidate", registration_code="CANDIDATE")
        db.add_all([voter, candidate]); db.flush()
        db.add_all([
            CommunicationVote(
                event_id=event.id, team_id=team.id,
                voter_player_id=voter.id, candidate_player_id=candidate.id,
            ),
            EventFeedback(event_id=event.id, player_id=voter.id, rating=5, review="Отлично"),
        ])
        db.commit()
        assert db.scalar(select(CommunicationVote)).candidate_player_id == candidate.id
        feedback = db.scalar(select(EventFeedback))
        assert feedback.rating == 5
        assert feedback.review == "Отлично"
