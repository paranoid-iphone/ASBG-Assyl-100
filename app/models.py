from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import secrets

from sqlalchemy import Boolean, DateTime, Enum as SQLEnum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class PlayerRole(str, Enum):
    PLAYER = "PLAYER"
    CAPTAIN = "CAPTAIN"
    ADMIN = "ADMIN"


class QuestionStatus(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    PUBLISHED = "PUBLISHED"


class AnswerScope(str, Enum):
    PERSONAL = "PERSONAL"
    TEAM = "TEAM"

class QuestionType(str, Enum):
    TEXT = "TEXT"
    CHOICE = "CHOICE"
    CHOICE_EXPLANATION = "CHOICE_EXPLANATION"

class StageType(str, Enum):
    QUIZ = "QUIZ"
    DETECTIVE = "DETECTIVE"

class DetectiveStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class Event(Base):
    """A game. The historical class name is kept to avoid needless API churn."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    event_date: Mapped[date | None] = mapped_column(nullable=True)
    registration_code: Mapped[str] = mapped_column(
        String(40), unique=True, index=True, default=lambda: f"EVENT-{secrets.token_hex(4).upper()}"
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    display_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=lambda: secrets.token_urlsafe(10))
    display_mode: Mapped[str] = mapped_column(String(30), default="WELCOME")
    current_question_id: Mapped[int | None] = mapped_column(nullable=True)
    current_detective_stage_id: Mapped[int | None] = mapped_column(nullable=True)
    current_slide_id: Mapped[int | None] = mapped_column(nullable=True)
    timer_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timer_duration_seconds: Mapped[int] = mapped_column(Integer, default=60)
    default_question_duration: Mapped[int] = mapped_column(Integer, default=60)
    default_personal_points: Mapped[float] = mapped_column(Float, default=1)
    default_team_points: Mapped[float] = mapped_column(Float, default=5)
    timer_sound_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_language: Mapped[str] = mapped_column(String(10), default="BOTH")
    screen_history_json: Mapped[str] = mapped_column(Text, default="[]")
    screen_future_json: Mapped[str] = mapped_column(Text, default="[]")
    pause_snapshot_json: Mapped[str] = mapped_column(Text, default="")
    slides_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    teams: Mapped[list[Team]] = relationship(back_populates="event", cascade="all, delete-orphan")
    stages: Mapped[list[Stage]] = relationship(back_populates="event", cascade="all, delete-orphan")
    programs: Mapped[list[GameProgram]] = relationship(back_populates="event", cascade="all, delete-orphan")
    slides: Mapped[list[EventSlide]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="EventSlide.position"
    )


class EventSlide(Base):
    __tablename__ = "event_slides"
    __table_args__ = (UniqueConstraint("event_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text, default="")
    title_kk: Mapped[str] = mapped_column(String(200), default="")
    text_kk: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=1)

    event: Mapped[Event] = relationship(back_populates="slides")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("event_id", "name"),
        UniqueConstraint("event_id", "code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(40), index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    telegram_invite_url: Mapped[str] = mapped_column(String(500), default="")
    gathering_riddle_ru: Mapped[str] = mapped_column(Text, default="")
    gathering_riddle_kk: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    capacity: Mapped[int] = mapped_column(Integer, default=10)

    event: Mapped[Event] = relationship(back_populates="teams")
    players: Mapped[list[Player]] = relationship(back_populates="team", cascade="all, delete-orphan")
    captain_elections: Mapped[list[CaptainElection]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    full_name: Mapped[str] = mapped_column(String(160))
    registration_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    role: Mapped[PlayerRole] = mapped_column(SQLEnum(PlayerRole), default=PlayerRole.PLAYER)
    telegram_user_id: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(80), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(5), default="RU")

    team: Mapped[Team | None] = relationship(back_populates="players")


class PendingRegistration(Base):
    __tablename__ = "pending_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    telegram_user_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(80), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(5), default="RU")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    event: Mapped[Event] = relationship()


class Stage(Base):
    __tablename__ = "stages"
    __table_args__ = (UniqueConstraint("event_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    system_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    title_kk: Mapped[str] = mapped_column(String(200), default="")
    description_kk: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=1)
    stage_type: Mapped[StageType] = mapped_column(SQLEnum(StageType), default=StageType.QUIZ)
    detective_status: Mapped[DetectiveStatus] = mapped_column(
        SQLEnum(DetectiveStatus), default=DetectiveStatus.DRAFT
    )
    detective_duration_seconds: Mapped[int] = mapped_column(Integer, default=1200)
    detective_points: Mapped[str] = mapped_column(String(200), default="30,25,20,17,14,10")
    detective_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    default_duration_seconds: Mapped[int] = mapped_column(Integer, default=60)
    default_submission_seconds: Mapped[int] = mapped_column(Integer, default=20)
    default_team_points: Mapped[float] = mapped_column(Float, default=5)

    event: Mapped[Event] = relationship(back_populates="stages")
    questions: Mapped[list[Question]] = relationship(
        back_populates="stage", cascade="all, delete-orphan", order_by="Question.position"
    )
    detective_cases: Mapped[list[DetectiveCase]] = relationship(
        back_populates="stage", cascade="all, delete-orphan"
    )


class GameProgram(Base):
    __tablename__ = "game_programs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    event: Mapped[Event] = relationship(back_populates="programs")
    stage_links: Mapped[list[GameProgramStage]] = relationship(
        back_populates="program", cascade="all, delete-orphan", order_by="GameProgramStage.position"
    )


class GameProgramStage(Base):
    __tablename__ = "game_program_stages"
    __table_args__ = (
        UniqueConstraint("program_id", "stage_id"),
        UniqueConstraint("program_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("game_programs.id"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    position: Mapped[int] = mapped_column(Integer)

    program: Mapped[GameProgram] = relationship(back_populates="stage_links")
    stage: Mapped[Stage] = relationship()


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("stage_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    correct_answer: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text, default="")
    title_kk: Mapped[str] = mapped_column(String(200), default="")
    text_kk: Mapped[str] = mapped_column(Text, default="")
    correct_answer_kk: Mapped[str] = mapped_column(Text, default="")
    explanation_kk: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[QuestionStatus] = mapped_column(SQLEnum(QuestionStatus), default=QuestionStatus.DRAFT)
    personal_answers_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    team_answers_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    personal_points: Mapped[float] = mapped_column(Float, default=1)
    team_points: Mapped[float] = mapped_column(Float, default=5)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=60)
    submission_seconds: Mapped[int] = mapped_column(Integer, default=20)
    question_type: Mapped[QuestionType] = mapped_column(SQLEnum(QuestionType), default=QuestionType.TEXT)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    show_anonymous_answers: Mapped[bool] = mapped_column(Boolean, default=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    stage: Mapped[Stage] = relationship(back_populates="questions")


# Compatibility alias used by a few integrations.
GameRound = Question
RoundStatus = QuestionStatus


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint("question_id", "scope", "player_id"),
        UniqueConstraint("question_id", "scope", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    scope: Mapped[AnswerScope] = mapped_column(SQLEnum(AnswerScope))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text, default="")
    respondent_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    points_awarded: Mapped[float] = mapped_column(Float, default=0)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    question: Mapped[Question] = relationship()
    team: Mapped[Team | None] = relationship(foreign_keys=[team_id])
    respondent: Mapped[Player | None] = relationship(foreign_keys=[respondent_player_id])


class TeamQuestionPrompt(Base):
    __tablename__ = "team_question_prompts"
    __table_args__ = (UniqueConstraint("question_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    telegram_chat_id: Mapped[str] = mapped_column(String(40))
    telegram_message_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CaptainElection(Base):
    __tablename__ = "captain_elections"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    telegram_message_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    team: Mapped[Team] = relationship(back_populates="captain_elections")
    votes: Mapped[list[CaptainVote]] = relationship(back_populates="election", cascade="all, delete-orphan")


class CaptainVote(Base):
    __tablename__ = "captain_votes"
    __table_args__ = (UniqueConstraint("election_id", "voter_player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("captain_elections.id"))
    voter_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    candidate_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    election: Mapped[CaptainElection] = relationship(back_populates="votes")
    voter: Mapped[Player] = relationship(foreign_keys=[voter_player_id])
    candidate: Mapped[Player] = relationship(foreign_keys=[candidate_player_id])


class DetectiveCase(Base):
    __tablename__ = "detective_cases"
    __table_args__ = (UniqueConstraint("stage_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    title_ru: Mapped[str] = mapped_column(String(240))
    title_kk: Mapped[str] = mapped_column(String(240), default="")
    story_ru: Mapped[str] = mapped_column(Text)
    story_kk: Mapped[str] = mapped_column(Text, default="")
    options_json: Mapped[str] = mapped_column(Text)
    correct_option: Mapped[str] = mapped_column(String(120))
    solution_json: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    stage: Mapped[Stage] = relationship(back_populates="detective_cases")
    team: Mapped[Team] = relationship()
    clues: Mapped[list[DetectiveClue]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="DetectiveClue.position"
    )
    submission: Mapped[DetectiveSubmission | None] = relationship(
        back_populates="case", cascade="all, delete-orphan", uselist=False
    )


class DetectiveClue(Base):
    __tablename__ = "detective_clues"
    __table_args__ = (
        UniqueConstraint("case_id", "position"),
        UniqueConstraint("case_id", "player_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("detective_cases.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    position: Mapped[int] = mapped_column(Integer)
    text_ru: Mapped[str] = mapped_column(Text)
    text_kk: Mapped[str] = mapped_column(Text, default="")
    predicate_json: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_essential: Mapped[bool] = mapped_column(Boolean, default=False)

    case: Mapped[DetectiveCase] = relationship(back_populates="clues")
    player: Mapped[Player] = relationship()


class DetectiveSubmission(Base):
    __tablename__ = "detective_submissions"
    __table_args__ = (UniqueConstraint("case_id"), UniqueConstraint("stage_id", "team_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("detective_cases.id"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    captain_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    selected_option: Mapped[str] = mapped_column(String(120))
    is_correct: Mapped[bool] = mapped_column(Boolean)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    elapsed_seconds: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_awarded: Mapped[float] = mapped_column(Float, default=0)

    case: Mapped[DetectiveCase] = relationship(back_populates="submission")
    team: Mapped[Team] = relationship()
    captain: Mapped[Player] = relationship()


class ScoreAdjustment(Base):
    __tablename__ = "score_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    points: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
