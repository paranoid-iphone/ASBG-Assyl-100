from datetime import datetime
from app.database import Base, SessionLocal, engine
from app.models import AnswerScope, Event, Player, PlayerRole, Question, QuestionStatus, Stage, Team
from app.services import grade_all_answers, leaderboard, register_player, self_register_player, submit_answer


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_registration_answers_grading_and_leaderboard():
    with SessionLocal() as db:
        game = Event(name="Test")
        db.add(game); db.flush()
        team = Team(event_id=game.id, name="One", code="ONE")
        db.add(team); db.flush()
        captain = Player(team_id=team.id, full_name="Captain", registration_code="CAP", role=PlayerRole.CAPTAIN)
        player = Player(team_id=team.id, full_name="Player", registration_code="P1")
        stage = Stage(event_id=game.id, title="Stage", position=1)
        db.add_all([captain, player, stage]); db.flush()
        question = Question(stage_id=stage.id, title="Question", text="Text", correct_answer="Ёлка", status=QuestionStatus.OPEN)
        db.add(question); db.flush()
        game.current_question_id = question.id
        game.display_mode = "SUBMISSION"
        game.timer_started_at = datetime.utcnow()
        game.timer_duration_seconds = 20
        db.commit()

        registered = register_player(db, "p1", "100", "user")
        personal = submit_answer(db, registered, " елка! ", AnswerScope.PERSONAL)
        official = submit_answer(db, db.get(Player, captain.id), "Ёлка", AnswerScope.TEAM)
        grade_all_answers(db, question)

        assert personal.is_correct is True
        assert official.is_correct is True
        board = leaderboard(db, game.id)
        assert board["teams"][0]["points"] == 5
        assert board["players"][0]["points"] == 1


def test_self_registration_by_event_and_team():
    with SessionLocal() as db:
        game = Event(name="Open event", registration_code="OPEN26")
        db.add(game); db.flush()
        team = Team(event_id=game.id, name="Blue", code="BLUE")
        db.add(team); db.commit()
        player = self_register_player(db, game.id, "blue", "Иван Иванов", "777", "ivan")
        assert player.team_id == team.id
        assert player.role == PlayerRole.PLAYER
        assert player.telegram_user_id == "777"
