from datetime import datetime


# Lightweight live telemetry. The application runs as one web process in the MVP.
SCREEN_HEARTBEATS: dict[int, datetime] = {}
TEAM_DELIVERY: dict[tuple[int, int], dict] = {}
CLUE_DELIVERY: dict[tuple[int, int], dict] = {}
TEMPORARY_SENDERS: dict[tuple[int, int], int] = {}
SCREEN_HISTORY: dict[int, list[dict]] = {}
CUSTOM_SLIDES: dict[int, dict] = {}


def mark_team_delivery(question_id: int, team_id: int, status: str, error: str = "") -> None:
    TEAM_DELIVERY[(question_id, team_id)] = {
        "status": status,
        "error": error[:300],
        "updated_at": datetime.utcnow(),
    }


def mark_clue_delivery(stage_id: int, player_id: int, status: str, error: str = "") -> None:
    CLUE_DELIVERY[(stage_id, player_id)] = {
        "status": status,
        "error": error[:300],
        "updated_at": datetime.utcnow(),
    }
