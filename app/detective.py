from __future__ import annotations

import hashlib
import itertools
import json
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DetectiveCase, DetectiveClue, Player, Stage, Team

SUSPECTS = ["Алекс", "Борис", "Виктор", "Галина"]
SUSPECTS_KK = ["Алекс", "Борис", "Виктор", "Галина"]
CATEGORIES = [
    ("головной убор", ["кепка", "шляпа", "капюшон", "берет"],
     ["кепка", "қалпақ", "капюшон", "берет"]),
    ("верхняя одежда", ["пиджак", "пальто", "куртка", "плащ"],
     ["пиджак", "пальто", "күртеше", "плащ"]),
    ("обувь", ["кроссовки", "ботинки", "туфли", "сапоги"],
     ["кроссовки", "бәтеңке", "туфли", "етік"]),
]


@dataclass
class GeneratedCase:
    solution: list[list[int]]
    culprit: int
    cards: list[list[dict]]
    public_predicates: list[dict]


def _matches(solution: tuple[tuple[int, ...], ...], culprit: int, predicate: dict) -> bool:
    if predicate["type"] == "eq":
        return solution[predicate["category"]][predicate["suspect"]] == predicate["value"]
    if predicate["type"] == "neq":
        return solution[predicate["category"]][predicate["suspect"]] != predicate["value"]
    if predicate["type"] == "not_in":
        return solution[predicate["category"]][predicate["suspect"]] not in predicate["values"]
    return solution[predicate["category"]][culprit] == predicate["value"]


def _flatten(cards: list[dict] | list[list[dict]]) -> list[dict]:
    return [predicate for card in cards for predicate in (card if isinstance(card, list) else [card])]


def all_solutions(cards: list[dict] | list[list[dict]], public_predicates: list[dict] | None = None) -> list[tuple[tuple[tuple[int, ...], ...], int]]:
    predicates = _flatten(cards) + (public_predicates or [])
    result = []
    for assignments in itertools.product(itertools.permutations(range(4)), repeat=3):
        for culprit in range(4):
            if all(_matches(assignments, culprit, p) for p in predicates):
                result.append((assignments, culprit))
                if len(result) > 2:
                    return result
    return result


def validate_predicates(cards: list[dict] | list[list[dict]], public_predicates: list[dict] | None = None) -> dict:
    normalized = [card if isinstance(card, list) else [card] for card in cards]
    solutions = all_solutions(normalized, public_predicates)
    essential = []
    for index in range(len(normalized)):
        essential.append(len(all_solutions(normalized[:index] + normalized[index + 1:], public_predicates)) > 1)
    return {
        "unique_solution": len(solutions) == 1,
        "clue_count": len(normalized),
        "all_clues_essential": all(essential),
        "essential": essential,
    }


def generate_logic(seed: str, card_count: int = 10) -> GeneratedCase:
    if not 4 <= card_count <= 10:
        raise ValueError("Детективная игра поддерживает команды от 4 до 10 активных игроков.")
    rng = random.Random(seed)
    solution = [rng.sample(range(4), 4) for _ in range(3)]
    culprit = rng.randrange(4)
    predicates = []
    # Three facts per category are sufficient and individually necessary;
    # bijectivity determines the fourth value.
    for category, permutation in enumerate(solution):
        omitted = rng.randrange(4)
        for suspect in range(4):
            if suspect != omitted:
                if rng.random() < 0.45:
                    predicates.append({
                        "type": "not_in", "category": category, "suspect": suspect,
                        "values": [value for value in range(4) if value != permutation[suspect]],
                    })
                else:
                    predicates.append({
                        "type": "eq", "category": category,
                        "suspect": suspect, "value": permutation[suspect],
                    })
    public_predicates = [
        {"type": "culprit_property", "category": category, "value": solution[category][culprit]}
        for category in range(3)
    ]
    # One forensic predicate ties the reconstructed table to the culprit.
    # The full three-property profile is shown publicly in the briefing.
    culprit_category = rng.randrange(3)
    predicates.append(public_predicates[culprit_category])
    rng.shuffle(predicates)
    cards = [[] for _ in range(card_count)]
    for index, predicate in enumerate(predicates):
        cards[index % card_count].append(predicate)
    rng.shuffle(cards)
    return GeneratedCase(solution, culprit, cards, public_predicates)


EVIDENCE_SOURCES_RU = [
    "Запись камеры в холле", "Показание гардеробщика", "Отчёт охраны",
    "Фотография посетителей", "Запись камеры у выхода", "Показание администратора",
    "Чек из гардероба", "Снимок с выставки", "Журнал службы безопасности",
]
EVIDENCE_SOURCES_KK = [
    "Холлдағы камера жазбасы", "Киім ілушінің айғағы", "Күзет есебі",
    "Келушілердің фотосуреті", "Шығаберістегі камера жазбасы", "Әкімшінің айғағы",
    "Киім ілетін орынның түбіртегі", "Көрмеден түсірілген сурет", "Қауіпсіздік журналы",
]


def _atomic_clue_text(predicate: dict, source_index: int) -> tuple[str, str]:
    if predicate["type"] == "culprit_property":
        category = predicate["category"]
        value = predicate["value"]
        category_label_ru = ["Головной убор", "Верхняя одежда", "Обувь"][category]
        return (
            f"КЛЮЧЕВАЯ УЛИКА — криминалистическая экспертиза: на витрине обнаружены следы, "
            f"по которым установлено: {category_label_ru.lower()} виновного — "
            f"{CATEGORIES[category][1][value]}.",
            f"НЕГІЗГІ АЙҒАҚ — криминалистикалық сараптама: витринадағы іздер бойынша "
            f"кінәлінің «{CATEGORIES[category][0]}» санатындағы заты "
            f"{CATEGORIES[category][2][value]} болғаны анықталды.",
        )
    category = predicate["category"]
    suspect = predicate["suspect"]
    source_ru = EVIDENCE_SOURCES_RU[source_index % len(EVIDENCE_SOURCES_RU)]
    source_kk = EVIDENCE_SOURCES_KK[source_index % len(EVIDENCE_SOURCES_KK)]
    category_label_ru = ["Головной убор", "Верхняя одежда", "Обувь"][category]
    if predicate["type"] == "neq":
        value = predicate["value"]
        return (
            f"{source_ru}: известно, что {category_label_ru.lower()} персонажа {SUSPECTS[suspect]} — "
            f"не {CATEGORIES[category][1][value]}.",
            f"{source_kk}: {SUSPECTS_KK[suspect]} кейіпкерінің «{CATEGORIES[category][0]}» "
            f"санатындағы заты {CATEGORIES[category][2][value]} емес.",
        )
    if predicate["type"] == "not_in":
        values = predicate["values"]
        variants_ru = " и не ".join(CATEGORIES[category][1][value] for value in values)
        variants_kk = ", ".join(CATEGORIES[category][2][value] for value in values)
        return (
            f"{source_ru}: {category_label_ru.lower()} персонажа {SUSPECTS[suspect]} — "
            f"не {variants_ru}.",
            f"{source_kk}: {SUSPECTS_KK[suspect]} кейіпкерінің заты мына нұсқалардың ешқайсысы емес: {variants_kk}.",
        )
    value = predicate["value"]
    return (
        f"{source_ru}: {category_label_ru} персонажа {SUSPECTS[suspect]} — "
        f"{CATEGORIES[category][1][value]}.",
        f"{source_kk}: {SUSPECTS_KK[suspect]} кейіпкерінің «{CATEGORIES[category][0]}» "
        f"санатындағы заты {CATEGORIES[category][2][value]} болды.",
    )


def clue_text(predicates: list[dict], serial: str) -> tuple[str, str]:
    source_seed = int(hashlib.sha256(serial.encode()).hexdigest()[:6], 16)
    pieces = [_atomic_clue_text(predicate, source_seed + index) for index, predicate in enumerate(predicates)]
    ru = (
        "ВАША КАРТОЧКА УЛИКИ\n"
        "Сообщите эти сведения команде. Каждый предмет в каждой категории принадлежит "
        "только одному человеку и не повторяется. Сопоставьте сведения об одежде с ключевой уликой о виновном.\n\n" +
        "\n".join(f"{index}. {piece[0]}" for index, piece in enumerate(pieces, 1))
    )
    kk = (
        "СІЗДІҢ АЙҒАҚ КАРТОЧКАҢЫЗ\n"
        "Бұл мәліметтерді командаға айтыңыз. Әр санаттағы әр зат тек бір адамға тиесілі "
        "және қайталанбайды. Киім туралы мәліметтерді кінәлі туралы негізгі айғақпен салыстырыңыз.\n\n" +
        "\n".join(f"{index}. {piece[1]}" for index, piece in enumerate(pieces, 1))
    )
    return ru, kk


def generate_cases_for_stage(db: Session, stage: Stage) -> list[DetectiveCase]:
    teams = db.scalars(select(Team).where(Team.event_id == stage.event_id, Team.active.is_(True)).order_by(Team.id)).all()
    if not teams:
        raise ValueError("Сначала создайте хотя бы одну активную команду.")
    existing_case_fingerprints = set(db.scalars(select(DetectiveCase.fingerprint)).all())
    existing_clue_fingerprints = set(db.scalars(select(DetectiveClue.fingerprint)).all())
    created = []
    for team in teams:
        players = db.scalars(
            select(Player).where(Player.team_id == team.id, Player.active.is_(True)).order_by(Player.id)
        ).all()
        if not 4 <= len(players) <= 10:
            raise ValueError(
                f"В команде «{team.name}» должно быть от 4 до 10 активных игроков; сейчас {len(players)}."
            )
        old = db.scalar(select(DetectiveCase).where(DetectiveCase.stage_id == stage.id, DetectiveCase.team_id == team.id))
        if old:
            db.delete(old)
            db.flush()
        attempt = 0
        while True:
            seed = f"{stage.event_id}:{stage.id}:{team.id}:{attempt}"
            generated = generate_logic(seed, len(players))
            solution_key = json.dumps(
                {"solution": generated.solution, "culprit": generated.culprit},
                sort_keys=True, ensure_ascii=False,
            )
            fingerprint = hashlib.sha256(solution_key.encode()).hexdigest()
            card_keys = [
                hashlib.sha256(
                    f"{fingerprint}:{json.dumps(card, sort_keys=True)}".encode()
                ).hexdigest()
                for card in generated.cards
            ]
            if fingerprint not in existing_case_fingerprints and not (set(card_keys) & existing_clue_fingerprints):
                break
            attempt += 1
        validation = validate_predicates(generated.cards)
        if not validation["unique_solution"] or not validation["all_clues_essential"]:
            raise RuntimeError("Генератор создал непроверяемый кейс.")
        culprit_profile_ru = ", ".join(
            CATEGORIES[category][1][generated.solution[category][generated.culprit]]
            for category in range(3)
        )
        culprit_profile_kk = ", ".join(
            CATEGORIES[category][2][generated.solution[category][generated.culprit]]
            for category in range(3)
        )
        case = DetectiveCase(
            stage_id=stage.id,
            team_id=team.id,
            title_ru=f"Дело команды «{team.name}»",
            title_kk=f"«{team.name}» командасының ісі",
            story_ru=(
                "На выставке пропал экспонат. Рядом находились Алекс, Борис, Виктор и Галина. "
                "У каждого был свой головной убор, своя верхняя одежда и своя обувь; предметы "
                "внутри каждой категории не повторяются. Участники команды получили разные улики. "
                "Обменяйтесь ими и восстановите внешний вид посетителей. Криминалисты установили "
                f"три общих признака виновного: {culprit_profile_ru}. Найдите единственного человека, "
                "которому одновременно принадлежат все три признака."
            ),
            story_kk=(
                "Көрмеде жәдігер жоғалды. Оның жанында Алекс, Борис, Виктор және Галина болды. "
                "Әрқайсысының бас киімі, сырт киімі және аяқ киімі бөлек; әр санаттағы заттар "
                "қайталанбайды. Команда мүшелеріне әртүрлі айғақтар берілді. Оларды бөлісіп, "
                "келушілердің сыртқы бейнесін қалпына келтіріңіз. Криминалистер кінәлінің үш белгісін "
                f"анықтады: {culprit_profile_kk}. Үш белгісі де сәйкес келетін жалғыз адамды табыңыз."
            ),
            options_json=json.dumps(SUSPECTS, ensure_ascii=False),
            correct_option=SUSPECTS[generated.culprit],
            solution_json=solution_key,
            fingerprint=fingerprint,
            validation_json=json.dumps({**validation, "culprit_profile": generated.public_predicates}),
            approved=True,
        )
        db.add(case)
        db.flush()
        for index, (player, card, clue_key) in enumerate(zip(players, generated.cards, card_keys), 1):
            ru, kk = clue_text(card, f"{fingerprint[:5].upper()}-{index}")
            db.add(DetectiveClue(
                case_id=case.id, player_id=player.id, position=index,
                text_ru=ru, text_kk=kk,
                predicate_json=json.dumps(card), fingerprint=clue_key, is_essential=True,
            ))
        existing_case_fingerprints.add(fingerprint)
        existing_clue_fingerprints.update(card_keys)
        created.append(case)
    db.flush()
    return created
