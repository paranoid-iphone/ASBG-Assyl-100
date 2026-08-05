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
    # All teams investigate the same story. We still keep one case row per team,
    # because clues and the single final submission are team-scoped in the data model.
    clue_cards = [
        (
            "Журнал доступа: дверь архива открыли в 19:38 красной картой № 17.",
            "Кіру журналы: мұрағат есігі 19:38-де №17 қызыл картамен ашылған.",
        ),
        (
            "Красная карта № 17 закреплена за Борисом. В 19:30 он сообщил охране, что потерял её.",
            "№17 қызыл карта Бориске тиесілі. 19:30-да ол картаны жоғалтқанын күзетке хабарлаған.",
        ),
        (
            "Камера в холле: в 19:31 Виктор поднял с пола красную карту № 17 и положил её в карман.",
            "Холл камерасы: 19:31-де Виктор еденнен №17 қызыл картаны көтеріп, қалтасына салған.",
        ),
        (
            "Камера у архива не показывает лица, но в 19:38 зафиксировала человека в светлой куртке.",
            "Мұрағат камерасы адамның бетін көрсетпейді, бірақ 19:38-де ашық түсті күртеше киген адамды тіркеген.",
        ),
        (
            "На общей фотографии в 19:25 Виктор был в светлой куртке; Айдар, Борис и Галина — в тёмной одежде.",
            "19:25-тегі ортақ суретте Виктор ашық түсті күртешеде, ал Айдар, Борис және Галина қара киімде болған.",
        ),
        (
            "Запись сцены непрерывно показывает Айдара с 19:35 до 19:42.",
            "Сахна жазбасында Айдар 19:35-тен 19:42-ге дейін үздіксіз көрінеді.",
        ),
        (
            "Чек кафе и камера кассы подтверждают: Галина оплачивала заказ в 19:38.",
            "Кафе чегі мен касса камерасы Галинаның 19:38-де тапсырыс төлегенін растайды.",
        ),
        (
            "Камера охраны непрерывно показывает Бориса у стойки с 19:34 до 19:41.",
            "Күзет камерасында Борис 19:34-тен 19:41-ге дейін күзет орнында көрінеді.",
        ),
        (
            "Эксперт подтвердил: часы всех камер и системы доступа синхронизированы; расхождения во времени нет.",
            "Сарапшы барлық камералар мен кіру жүйесінің сағаттары бірдей екенін растады; уақыт айырмасы жоқ.",
        ),
        (
            "После 19:31 Виктор не появлялся ни на одной общей камере до 19:41.",
            "19:31-ден кейін Виктор 19:41-ге дейін бірде-бір жалпы камерада көрінбеген.",
        ),
    ]
    story_ru = (
        "В 19:40 из закрытого архива исчез прототип. Дверь не взломана: её открыли штатной картой. "
        "Рядом находились четыре человека — Айдар, Борис, Виктор и Галина. Установите, кто забрал прототип. "
        "Для доказанного ответа соедините три вещи: кто мог открыть дверь, кто находился у архива и чьи алиби исключают остальных."
    )
    story_kk = (
        "19:40-та жабық мұрағаттан прототип жоғалды. Есік бұзылмаған: ол қызметтік картамен ашылған. "
        "Жақын жерде төрт адам болды — Айдар, Борис, Виктор және Галина. Прототипті кім алғанын анықтаңыз. "
        "Дәлелді жауап үшін үш нәрсені байланыстырыңыз: есікті кім аша алды, мұрағат маңында кім болды және кімдердің алибиі бар."
    )
    options = ["Айдар", "Борис", "Виктор", "Галина"]
    shared_key = hashlib.sha256(f"shared-detective-v2:{stage.event_id}:{stage.id}".encode()).hexdigest()
    created = []
    for team in teams:
        players = db.scalars(
            select(Player).where(Player.team_id == team.id, Player.active.is_(True)).order_by(Player.id)
        ).all()
        if not 8 <= len(players) <= 10:
            raise ValueError(
                f"В команде «{team.name}» должно быть от 8 до 10 активных игроков; сейчас {len(players)}."
            )
        old = db.scalar(select(DetectiveCase).where(DetectiveCase.stage_id == stage.id, DetectiveCase.team_id == team.id))
        if old:
            db.delete(old)
            db.flush()
        fingerprint = hashlib.sha256(f"{shared_key}:team:{team.id}".encode()).hexdigest()
        solution_key = json.dumps({"case": "archive_prototype", "culprit": "Виктор"}, ensure_ascii=False)
        case = DetectiveCase(
            stage_id=stage.id,
            team_id=team.id,
            title_ru="Дело о пропавшем прототипе",
            title_kk="Жоғалған прототип ісі",
            story_ru=story_ru,
            story_kk=story_kk,
            options_json=json.dumps(options, ensure_ascii=False),
            correct_option="Виктор",
            solution_json=solution_key,
            fingerprint=fingerprint,
            validation_json=json.dumps({
                "shared_case": True,
                "version": 2,
                "core_clues": 8,
                "player_count": len(players),
                "reason": "Виктор нашёл карту, совпадает с камерой у архива; у остальных подтверждённые алиби.",
            }, ensure_ascii=False),
            approved=True,
        )
        db.add(case)
        db.flush()
        for index, player in enumerate(players, 1):
            ru_fact, kk_fact = clue_cards[index - 1]
            ru = f"КАРТОЧКА УЛИКИ {index}\n\n{ru_fact}\n\nРасскажите эту улику команде."
            kk = f"АЙҒАҚ КАРТОЧКАСЫ {index}\n\n{kk_fact}\n\nБұл айғақты командаңызға айтыңыз."
            clue_key = hashlib.sha256(f"{fingerprint}:clue:{index}".encode()).hexdigest()
            db.add(DetectiveClue(
                case_id=case.id, player_id=player.id, position=index,
                text_ru=ru, text_kk=kk,
                predicate_json=json.dumps({"shared_case": True, "clue": index}),
                fingerprint=clue_key, is_essential=index <= 8,
            ))
        created.append(case)
    db.flush()
    return created
