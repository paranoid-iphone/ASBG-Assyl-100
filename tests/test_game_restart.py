from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    Answer, AnswerScope, Event, GameProgram, GameProgramStage, Player, PlayerRole,
    Question, QuestionStatus, ResponseArchive, ScoreAdjustment, Stage, Team, TeamQuestionPrompt,
)


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_restart_clears_game_answers_but_preserves_roster_captain_and_manual_points():
    with SessionLocal() as db:
        event = Event(name="Restart", display_mode="ANSWER")
        db.add(event); db.flush()
        team = Team(event_id=event.id, name="Alpha", code="ALPHA")
        stage = Stage(event_id=event.id, title="Round", position=1)
        program = GameProgram(event_id=event.id, title="Main", status="RUNNING", started_at=datetime.utcnow())
        db.add_all([team, stage, program]); db.flush()
        captain = Player(
            team_id=team.id, full_name="Captain", registration_code="CAPTAIN",
            role=PlayerRole.CAPTAIN, active=True,
        )
        question = Question(
            stage_id=stage.id, title="Q", text="Text", correct_answer="A",
            status=QuestionStatus.LOCKED,
        )
        db.add_all([captain, question]); db.flush()
        db.add(GameProgramStage(program_id=program.id, stage_id=stage.id, position=1))
        db.add(Answer(
            question_id=question.id, scope=AnswerScope.TEAM, team_id=team.id,
            respondent_player_id=captain.id, text="A", is_correct=True, points_awarded=5,
        ))
        db.add(TeamQuestionPrompt(
            question_id=question.id, team_id=team.id,
            telegram_chat_id="1", telegram_message_id="2",
        ))
        db.add(ScoreAdjustment(event_id=event.id, team_id=team.id, points=7, reason="Спорт"))
        event.current_question_id = question.id
        db.commit()
        event_id, team_id, captain_id, question_id = event.id, team.id, captain.id, question.id

    with TestClient(app) as client:
        response = client.post(
            f"/admin/events/{event_id}/live/restart", auth=("admin", "change-me")
        )
        assert response.status_code == 200

    with SessionLocal() as db:
        event = db.get(Event, event_id)
        question = db.get(Question, question_id)
        captain = db.get(Player, captain_id)
        assert event.display_mode == "INTRO"
        assert event.current_question_id is None
        assert question.status == QuestionStatus.DRAFT
        assert db.scalar(select(func.count(Answer.id))) == 0
        archived = db.scalar(select(ResponseArchive))
        assert archived.answer_text == "A"
        assert archived.respondent_name == "Captain"
        assert archived.is_correct is True
        assert archived.points_awarded == 5
        assert db.scalar(select(func.count(TeamQuestionPrompt.id))) == 0
        assert db.scalar(select(func.count(ScoreAdjustment.id))) == 1
        assert captain.team_id == team_id
        assert captain.role == PlayerRole.CAPTAIN
