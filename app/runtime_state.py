from datetime import datetime
import json


# Lightweight live telemetry. The application runs as one web process in the MVP.
SCREEN_HEARTBEATS: dict[int, datetime] = {}
TEAM_DELIVERY: dict[tuple[int, int], dict] = {}
CLUE_DELIVERY: dict[tuple[int, int], dict] = {}
TEMPORARY_SENDERS: dict[tuple[int, int], int] = {}
SCREEN_HISTORY: dict[int, list[dict]] = {}
CUSTOM_SLIDES: dict[int, dict] = {}


def screen_snapshot(event) -> dict:
    elapsed = (datetime.utcnow() - event.timer_started_at).total_seconds() if event.timer_started_at else None
    remaining = max(0, event.timer_duration_seconds - int(elapsed)) if elapsed is not None else None
    return {
        "display_mode": event.display_mode,
        "current_question_id": event.current_question_id,
        "current_detective_stage_id": event.current_detective_stage_id,
        "timer_duration_seconds": event.timer_duration_seconds,
        "timer_remaining": remaining,
        "slide": dict(CUSTOM_SLIDES.get(event.id) or {}),
    }


def navigation_stack(event, field: str) -> list[dict]:
    try:
        value = json.loads(getattr(event, field, "[]") or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def save_navigation_stack(event, field: str, stack: list[dict]) -> None:
    setattr(event, field, json.dumps(stack[-100:], ensure_ascii=False))


def push_persistent_history(event, *, clear_future: bool = True) -> None:
    history = navigation_stack(event, "screen_history_json")
    history.append(screen_snapshot(event))
    save_navigation_stack(event, "screen_history_json", history)
    if clear_future:
        event.screen_future_json = "[]"


def clear_persistent_navigation(event) -> None:
    event.screen_history_json = "[]"
    event.screen_future_json = "[]"


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
