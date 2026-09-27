import os
import json
import hmac
import hashlib
import secrets
import asyncio
import random
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

import uvicorn


# =========================================================
# إعدادات
# =========================================================

BOT_TOKEN = "8699183910:AAEEhS-AUesJxYrIBVi2vlicVUi3uNSYPKI"

# بعد الاستضافة سنضع الرابط هنا
WEBAPP_URL = "https://ضع-رابط-الاستضافة-هنا.onrender.com"

DATA_FILE = "mafia_data.json"

MIN_PLAYERS = 6
MAX_PLAYERS = 12


# =========================================================
# قاعدة البيانات
# =========================================================

db_lock = asyncio.Lock()

default_db = {
    "users": {},
    "rooms": {},
    "games": {}
}


def load_db():
    if not os.path.exists(DATA_FILE):
        return default_db.copy()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_db.copy()


DB = load_db()


def save_db():
    temp = DATA_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

    os.replace(temp, DATA_FILE)


# =========================================================
# أدوات المستخدمين
# =========================================================

def get_user(user_id, name=None, username=None):
    uid = str(user_id)

    if uid not in DB["users"]:
        DB["users"][uid] = {
            "id": user_id,
            "name": name or "لاعب",
            "username": username or "",
            "level": 1,
            "xp": 0,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "mafia_wins": 0,
            "citizen_wins": 0,
            "jester_wins": 0,
            "achievements": [],
            "created_at": int(time.time()),
        }
    else:
        if name:
            DB["users"][uid]["name"] = name

        if username is not None:
            DB["users"][uid]["username"] = username

    return DB["users"][uid]


def add_xp(user, amount):
    user["xp"] += amount

    needed = user["level"] * 100

    while user["xp"] >= needed:
        user["xp"] -= needed
        user["level"] += 1
        needed = user["level"] * 100


# =========================================================
# Telegram initData
# =========================================================

def validate_init_data(init_data: str):
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram initData مفقود"
        )

    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = data.pop("hash", None)

        if not received_hash:
            raise ValueError("hash missing")

        auth_date = int(data.get("auth_date", "0"))

        # صلاحية بيانات الجلسة 24 ساعة
        if time.time() - auth_date > 86400:
            raise ValueError("expired")

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(data.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            raise ValueError("invalid hash")

        user_data = json.loads(data["user"])

        return {
            "user": user_data,
            "chat_type": data.get("chat_type"),
            "chat_instance": data.get("chat_instance"),
            "start_param": data.get("start_param"),
        }

    except Exception:
        raise HTTPException(
            status_code=401,
            detail="بيانات Telegram غير صالحة"
        )


async def current_user(x_telegram_init_data: str):
    result = validate_init_data(x_telegram_init_data)

    tg_user = result["user"]

    async with db_lock:
        user = get_user(
            tg_user["id"],
            tg_user.get("first_name", "لاعب"),
            tg_user.get("username", "")
        )
        save_db()

    return user, result


# =========================================================
# الأدوار
# =========================================================

ROLES = {
    "mafia": {
        "name": "المافيا",
        "emoji": "🔴",
        "team": "mafia",
        "description": "تحاول المافيا إقصاء بقية اللاعبين والوصول إلى التفوق العددي."
    },

    "doctor": {
        "name": "الطبيب",
        "emoji": "💚",
        "team": "citizen",
        "description": "يحاول حماية لاعب واحد في كل ليلة."
    },

    "detective": {
        "name": "المحقق",
        "emoji": "🔎",
        "team": "citizen",
        "description": "يستطيع فحص لاعب لمعرفة فريقه."
    },

    "jester": {
        "name": "المهرج",
        "emoji": "🃏",
        "team": "neutral",
        "description": "هدفه أن يتم إخراجه بالتصويت."
    },

    "citizen": {
        "name": "المواطن",
        "emoji": "👤",
        "team": "citizen",
        "description": "يحاول اكتشاف المافيا ومساعدة المدينة."
    }
}


# =========================================================
# تكوين الأدوار
# =========================================================

def build_roles(count):
    if count <= 6:
        roles = [
            "mafia",
            "doctor",
            "detective",
            "jester",
            "citizen",
            "citizen",
        ]

    elif count <= 8:
        roles = [
            "mafia",
            "mafia",
            "doctor",
            "detective",
            "jester",
            "citizen",
            "citizen",
            "citizen",
        ]

    elif count <= 10:
        roles = [
            "mafia",
            "mafia",
            "doctor",
            "detective",
            "jester",
            "citizen",
            "citizen",
            "citizen",
            "citizen",
            "citizen",
        ]

    else:
        roles = [
            "mafia",
            "mafia",
            "mafia",
            "doctor",
            "detective",
            "jester",
            "citizen",
            "citizen",
            "citizen",
            "citizen",
            "citizen",
            "citizen",
        ]

    random.shuffle(roles)
    return roles[:count]


# =========================================================
# إنشاء لاعب AI
# =========================================================

def create_ai(index):
    names = [
        "Shadow",
        "Hunter",
        "Nova",
        "Ghost",
        "Raven",
        "Zero",
        "Luna",
        "Storm",
        "Fox",
        "Ace",
        "Knight",
        "Echo",
    ]

    return {
        "id": f"ai_{secrets.token_hex(5)}",
        "name": names[index % len(names)],
        "username": "",
        "human": False,
        "alive": True,
        "role": None,
        "votes": 0,
    }


# =========================================================
# إنشاء مباراة
# =========================================================

def create_game(human_user):
    players = [
        {
            "id": str(human_user["id"]),
            "name": human_user["name"],
            "username": human_user.get("username", ""),
            "human": True,
            "alive": True,
            "role": None,
            "votes": 0,
        }
    ]

    while len(players) < MIN_PLAYERS:
        players.append(create_ai(len(players)))

    roles = build_roles(len(players))

    for player, role in zip(players, roles):
        player["role"] = role

    game_id = secrets.token_urlsafe(8)

    game = {
        "id": game_id,
        "players": players,
        "phase": "night",
        "day": 1,
        "started": True,
        "finished": False,
        "winner": None,

        "night_target": None,
        "doctor_target": None,
        "detective_target": None,

        "last_event": "بدأت المباراة.",
        "created_at": int(time.time()),

        "votes": {},
    }

    DB["games"][game_id] = game

    return game


# =========================================================
# أدوات اللعبة
# =========================================================

def find_player(game, player_id):
    for p in game["players"]:
        if str(p["id"]) == str(player_id):
            return p
    return None


def alive_players(game):
    return [
        p for p in game["players"]
        if p["alive"]
    ]


def role_players(game, role):
    return [
        p for p in game["players"]
        if p["alive"] and p["role"] == role
    ]


def game_for_user(user_id):
    uid = str(user_id)

    for game in DB["games"].values():
        if game["finished"]:
            continue

        for player in game["players"]:
            if str(player["id"]) == uid:
                return game

    return None


def winner_check(game):
    alive = alive_players(game)

    mafia = [
        p for p in alive
        if p["role"] == "mafia"
    ]

    non_mafia = [
        p for p in alive
        if p["role"] != "mafia"
    ]

    if not mafia:
        return "citizen"

    if len(mafia) >= len(non_mafia):
        return "mafia"

    return None


# =========================================================
# AI
# =========================================================

def ai_choose_target(game, player, candidates):
    candidates = [
        p for p in candidates
        if p["id"] != player["id"] and p["alive"]
    ]

    if not candidates:
        return None

    # اختيار بسيط مع بعض العشوائية
    return random.choice(candidates)


def perform_ai_night(game):
    alive = alive_players(game)

    mafia = role_players(game, "mafia")

    if mafia:
        mafia_actor = random.choice(mafia)

        targets = [
            p for p in alive
            if p["role"] != "mafia"
        ]

        target = ai_choose_target(
            game,
            mafia_actor,
            targets
        )

        if target:
            game["night_target"] = target["id"]

    doctors = role_players(game, "doctor")

    if doctors:
        doctor = random.choice(doctors)

        target = ai_choose_target(
            game,
            doctor,
            alive
        )

        if target:
            game["doctor_target"] = target["id"]

    detectives = role_players(game, "detective")

    if detectives:
        detective = random.choice(detectives)

        targets = [
            p for p in alive
            if p["id"] != detective["id"]
        ]

        target = ai_choose_target(
            game,
            detective,
            targets
        )

        if target:
            game["detective_target"] = target["id"]


def resolve_night(game):
    target_id = game.get("night_target")
    doctor_id = game.get("doctor_target")

    if target_id and target_id != doctor_id:
        target = find_player(game, target_id)

        if target and target["alive"]:
            target["alive"] = False
            game["last_event"] = (
                f"انتهى الليل وتم إخراج {target['name']} من المباراة."
            )
        else:
            game["last_event"] = "انتهى الليل دون تغيير."
    else:
        game["last_event"] = "انتهى الليل دون إخراج لاعب."

    game["night_target"] = None
    game["doctor_target"] = None
    game["detective_target"] = None

    result = winner_check(game)

    if result:
        finish_game(game, result)
        return

    game["phase"] = "day"


# =========================================================
# التصويت
# =========================================================

def perform_ai_votes(game):
    alive = alive_players(game)

    for voter in alive:
        if voter["human"]:
            continue

        candidates = [
            p for p in alive
            if p["id"] != voter["id"]
        ]

        if not candidates:
            continue

        target = random.choice(candidates)

        game["votes"][voter["id"]] = target["id"]


def resolve_votes(game):
    counts = {}

    for target_id in game["votes"].values():
        counts[target_id] = counts.get(target_id, 0) + 1

    if not counts:
        game["last_event"] = "لم يحدث تصويت."
        game["phase"] = "night"
        game["day"] += 1
        return

    highest = max(counts.values())

    winners = [
        player_id
        for player_id, count in counts.items()
        if count == highest
    ]

    if len(winners) != 1:
        game["last_event"] = "حدث تعادل في التصويت."
    else:
        target = find_player(game, winners[0])

        if target:
            target["alive"] = False

            if target["role"] == "jester":
                finish_game(game, "jester")
                return

            game["last_event"] = (
                f"تم إخراج {target['name']} بالتصويت."
            )

    game["votes"] = {}

    result = winner_check(game)

    if result:
        finish_game(game, result)
        return

    game["phase"] = "night"
    game["day"] += 1


# =========================================================
# إنهاء المباراة
# =========================================================

def finish_game(game, winner):
    game["finished"] = True
    game["winner"] = winner
    game["phase"] = "finished"

    human = None

    for p in game["players"]:
        if p["human"]:
            human = p
            break

    if not human:
        return

    user = DB["users"].get(str(human["id"]))

    if not user:
        return

    user["games"] += 1

    if winner == "jester":
        won = human["role"] == "jester"

    elif winner == "mafia":
        won = human["role"] == "mafia"

    else:
        won = human["role"] in [
            "citizen",
            "doctor",
            "detective"
        ]

    if won:
        user["wins"] += 1
        add_xp(user, 75)

        if human["role"] == "mafia":
            user["mafia_wins"] += 1

        elif human["role"] == "jester":
            user["jester_wins"] += 1

        else:
            user["citizen_wins"] += 1

    else:
        user["losses"] += 1
        add_xp(user, 20)

    save_db()


# =========================================================
# API Models
# =========================================================

class ActionRequest(BaseModel):
    action: str
    target_id: str | None = None


# =========================================================
# FastAPI
# =========================================================

web = FastAPI(title="لعبة المافيا")


@web.get("/", response_class=HTMLResponse)
async def homepage():
    return HTML_PAGE


@web.get("/health")
async def health():
    return {
        "ok": True,
        "game": "لعبة المافيا"
    }


@web.get("/api/me")
async def api_me(
    x_telegram_init_data: str = Header(default="")
):
    user, tg = await current_user(
        x_telegram_init_data
    )

    game = game_for_user(user["id"])

    return {
        "user": user,
        "game": public_game(game, user["id"]) if game else None
    }


@web.post("/api/start")
async def api_start(
    x_telegram_init_data: str = Header(default="")
):
    user, tg = await current_user(
        x_telegram_init_data
    )

    async with db_lock:
        existing = game_for_user(user["id"])

        if existing:
            return {
                "ok": True,
                "game": public_game(
                    existing,
                    user["id"]
                )
            }

        game = create_game(user)
        save_db()

    return {
        "ok": True,
        "game": public_game(
            game,
            user["id"]
        )
    }


@web.get("/api/game")
async def api_game(
    x_telegram_init_data: str = Header(default="")
):
    user, tg = await current_user(
        x_telegram_init_data
    )

    game = game_for_user(user["id"])

    return {
        "game": public_game(
            game,
            user["id"]
        ) if game else None
    }


@web.post("/api/action")
async def api_action(
    request: ActionRequest,
    x_telegram_init_data: str = Header(default="")
):
    user, tg = await current_user(
        x_telegram_init_data
    )

    async with db_lock:
        game = game_for_user(user["id"])

        if not game:
            raise HTTPException(
                status_code=404,
                detail="لا توجد مباراة"
            )

        if game["finished"]:
            return {
                "ok": True,
                "game": public_game(
                    game,
                    user["id"]
                )
            }

        player = find_player(
            game,
            str(user["id"])
        )

        if not player or not player["alive"]:
            raise HTTPException(
                status_code=400,
                detail="أنت خارج المباراة"
            )

        # -----------------------------
        # الليل
        # -----------------------------

        if game["phase"] == "night":

            if request.action == "mafia_target":

                if player["role"] != "mafia":
                    raise HTTPException(
                        status_code=403,
                        detail="هذا الإجراء ليس لدورك"
                    )

                target = find_player(
                    game,
                    request.target_id
                )

                if not target or not target["alive"]:
                    raise HTTPException(
                        status_code=400,
                        detail="اللاعب غير صالح"
                    )

                if target["role"] == "mafia":
                    raise HTTPException(
                        status_code=400,
                        detail="لا يمكنك اختيار لاعب من المافيا"
                    )

                game["night_target"] = target["id"]
                game["last_event"] = (
                    f"تم اختيار {target['name']}."
                )

            elif request.action == "doctor_target":

                if player["role"] != "doctor":
                    raise HTTPException(
                        status_code=403,
                        detail="هذا الإجراء ليس لدورك"
                    )

                target = find_player(
                    game,
                    request.target_id
                )

                if not target or not target["alive"]:
                    raise HTTPException(
                        status_code=400,
                        detail="اللاعب غير صالح"
                    )

                game["doctor_target"] = target["id"]

                game["last_event"] = (
                    f"تم اختيار لاعب للحماية."
                )

            elif request.action == "detective_check":

                if player["role"] != "detective":
                    raise HTTPException(
                        status_code=403,
                        detail="هذا الإجراء ليس لدورك"
                    )

                target = find_player(
                    game,
                    request.target_id
                )

                if not target or not target["alive"]:
                    raise HTTPException(
                        status_code=400,
                        detail="اللاعب غير صالح"
                    )

                result = (
                    "مافيا"
                    if target["role"] == "mafia"
                    else "ليس من المافيا"
                )

                game["last_event"] = (
                    f"نتيجة التحقيق: {target['name']} {result}."
                )

        # -----------------------------
        # النهار / التصويت
        # -----------------------------

        elif game["phase"] == "day":

            if request.action == "vote":

                target = find_player(
                    game,
                    request.target_id
                )

                if not target or not target["alive"]:
                    raise HTTPException(
                        status_code=400,
                        detail="اللاعب غير صالح"
                    )

                if target["id"] == player["id"]:
                    raise HTTPException(
                        status_code=400,
                        detail="لا يمكنك التصويت لنفسك"
                    )

                game["votes"][player["id"]] = target["id"]

                # إذا صوّت كل اللاعبين الأحياء
                alive = alive_players(game)

                if all(
                    p["id"] in game["votes"]
                    for p in alive
                ):
                    resolve_votes(game)

        save_db()

    return {
        "ok": True,
        "game": public_game(
            game,
            user["id"]
        )
    }


def public_game(game, user_id):
    if not game:
        return None

    me = find_player(
        game,
        str(user_id)
    )

    players = []

    for p in game["players"]:
        item = {
            "id": p["id"],
            "name": p["name"],
            "alive": p["alive"],
            "human": p["human"],
        }

        # اللاعب يرى دوره فقط
        if str(p["id"]) == str(user_id):
            item["role"] = p["role"]
            item["role_info"] = ROLES[p["role"]]

        players.append(item)

    return {
        "id": game["id"],
        "phase": game["phase"],
        "day": game["day"],
        "finished": game["finished"],
        "winner": game["winner"],
        "last_event": game["last_event"],
        "players": players,
        "my_role": me["role"] if me else None,
        "my_alive": me["alive"] if me else False,
    }


# =========================================================
# Telegram Bot
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    async with db_lock:
        get_user(
            user.id,
            user.first_name or "لاعب",
            user.username or ""
        )
        save_db()

    keyboard = [
        [
            InlineKeyboardButton(
                "🎮 فتح لعبة المافيا",
                web_app=WebAppInfo(
                    url=WEBAPP_URL
                )
            )
        ]
    ]

    text = (
        "🕵️‍♂️ *لعبة المافيا*\n\n"
        "أهلًا بك في عالم المافيا.\n"
        "افتح اللعبة وشوف حسابك، إحصائياتك "
        "وابدأ مباراة جديدة.\n\n"
        "🎭 أدوار مختلفة\n"
        "🤖 لعب ضد الذكاء الاصطناعي\n"
        "🏆 مستويات وإحصائيات"
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def game_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    # في المجموعة نرسل رابط Main Mini App
    bot_username = context.bot.username

    if bot_username:
        url = (
            f"https://t.me/"
            f"{bot_username}"
            f"?startapp=group"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🎮 فتح لعبة المافيا",
                    url=url
                )
            ]
        ]

        await update.message.reply_text(
            "🎮 افتح لعبة المافيا من هنا:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )


# =========================================================
# HTML / CSS / JS
# =========================================================

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1.0,
             maximum-scale=1.0,
             user-scalable=no"
>

<title>لعبة المافيا</title>

<script src="https://telegram.org/js/telegram-web-app.js?63"></script>

<style>

* {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}

body {
    margin: 0;
    min-height: 100vh;

    background:
        radial-gradient(
            circle at top,
            #25252e 0%,
            #111116 45%,
            #09090d 100%
        );

    color: #fff;

    font-family:
        Arial,
        "Noto Sans Arabic",
        sans-serif;
}

button {
    font-family: inherit;
}

.app {
    width: 100%;
    max-width: 520px;
    min-height: 100vh;
    margin: auto;

    padding: 18px 15px 30px;
}

.top {
    display: flex;
    align-items: center;
    justify-content: space-between;

    margin-bottom: 22px;
}

.logo {
    font-size: 24px;
    font-weight: 900;
}

.logo span {
    display: block;
    margin-top: 3px;

    color: #a5a5b2;
    font-size: 12px;
    font-weight: 500;
}

.avatar {
    width: 46px;
    height: 46px;

    border-radius: 15px;

    display: flex;
    align-items: center;
    justify-content: center;

    background: linear-gradient(
        135deg,
        #7227ff,
        #d43cff
    );

    font-size: 21px;
    font-weight: 900;
}

.profile {
    background: rgba(255,255,255,.055);
    border: 1px solid rgba(255,255,255,.08);

    border-radius: 24px;

    padding: 18px;

    margin-bottom: 15px;

    box-shadow:
        0 15px 40px rgba(0,0,0,.22);
}

.profile-row {
    display: flex;
    align-items: center;
    gap: 13px;
}

.profile-avatar {
    width: 58px;
    height: 58px;

    border-radius: 19px;

    display: flex;
    align-items: center;
    justify-content: center;

    background: linear-gradient(
        135deg,
        #4c22a8,
        #a52cd4
    );

    font-size: 25px;
}

.name {
    font-size: 18px;
    font-weight: 800;
}

.level {
    margin-top: 5px;

    color: #aaa9b7;
    font-size: 13px;
}

.xp {
    margin-top: 15px;
}

.xp-label {
    display: flex;
    justify-content: space-between;

    color: #a7a6b0;
    font-size: 11px;

    margin-bottom: 6px;
}

.xp-bar {
    height: 7px;

    background: #272731;

    border-radius: 20px;
    overflow: hidden;
}

.xp-fill {
    height: 100%;

    width: 35%;

    background:
        linear-gradient(
            90deg,
            #7d32ff,
            #d13eff
        );
}

.stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 9px;

    margin-top: 15px;
}

.stat {
    background: rgba(255,255,255,.04);
    border-radius: 15px;

    padding: 12px 5px;

    text-align: center;
}

.stat strong {
    display: block;

    font-size: 19px;
}

.stat span {
    display: block;

    margin-top: 4px;

    color: #9796a3;
    font-size: 10px;
}

.section-title {
    margin: 21px 3px 10px;

    color: #aaa9b4;

    font-size: 12px;
    font-weight: 700;
}

.main-button {
    width: 100%;

    border: 0;
    border-radius: 21px;

    padding: 18px;

    color: #fff;

    background:
        linear-gradient(
            135deg,
            #7027ff,
            #bc35dd
        );

    font-size: 17px;
    font-weight: 900;

    box-shadow:
        0 12px 30px rgba(125,45,255,.25);
}

.main-button:active {
    transform: scale(.98);
}

.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;

    margin-top: 10px;
}

.card-button {
    border: 1px solid rgba(255,255,255,.07);

    border-radius: 18px;

    padding: 17px 12px;

    background: rgba(255,255,255,.045);

    color: white;

    text-align: right;
}

.card-button .icon {
    font-size: 24px;
}

.card-button strong {
    display: block;
    margin-top: 8px;
}

.card-button small {
    display: block;
    margin-top: 4px;

    color: #898894;
}

.screen {
    display: none;
}

.screen.active {
    display: block;
}

.back {
    border: 0;
    background: none;

    color: #aaa;

    font-size: 14px;

    padding: 8px 0;

    margin-bottom: 8px;
}

.role {
    border-radius: 25px;

    padding: 25px 18px;

    text-align: center;

    background:
        linear-gradient(
            145deg,
            rgba(116,39,255,.2),
            rgba(214,50,223,.09)
        );

    border: 1px solid rgba(255,255,255,.08);
}

.role-emoji {
    font-size: 55px;
}

.role-name {
    margin-top: 10px;

    font-size: 26px;
    font-weight: 900;
}

.role-description {
    margin-top: 8px;

    color: #aaa9b5;

    line-height: 1.7;
    font-size: 13px;
}

.phase {
    text-align: center;

    margin: 17px 0;

    font-size: 22px;
    font-weight: 900;
}

.players {
    display: flex;
    flex-direction: column;

    gap: 8px;
}

.player {
    display: flex;
    align-items: center;
    justify-content: space-between;

    padding: 13px;

    background: rgba(255,255,255,.045);

    border: 1px solid rgba(255,255,255,.06);

    border-radius: 17px;
}

.player-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.player-avatar {
    width: 39px;
    height: 39px;

    display: flex;
    align-items: center;
    justify-content: center;

    border-radius: 13px;

    background: #292933;
}

.dead {
    opacity: .38;
}

.alive-dot {
    width: 8px;
    height: 8px;

    border-radius: 50%;

    background: #4ed68b;
}

.dead .alive-dot {
    background: #777;
}

.action-area {
    margin-top: 15px;
}

.target-button {
    width: 100%;

    margin-top: 7px;

    border: 1px solid rgba(255,255,255,.07);

    border-radius: 15px;

    padding: 13px;

    color: white;

    background: rgba(255,255,255,.05);
}

.target-button:active {
    transform: scale(.98);
}

.notice {
    margin-top: 15px;

    padding: 13px;

    border-radius: 15px;

    background: rgba(255,255,255,.045);

    color: #aaa9b5;

    line-height: 1.6;

    font-size: 12px;
}

.result {
    text-align: center;

    padding: 35px 15px;
}

.result-icon {
    font-size: 65px;
}

.result h1 {
    margin: 12px 0 5px;

    font-size: 28px;
}

.hidden {
    display: none !important;
}

.loading {
    min-height: 100vh;

    display: flex;
    align-items: center;
    justify-content: center;

    color: #aaa;
}

</style>

</head>

<body>

<div id="loading" class="loading">
    جاري تحميل اللعبة...
</div>

<div id="app" class="app hidden">

    <!-- الرئيسية -->

    <section id="home" class="screen active">

        <div class="top">
            <div class="logo">
                🕵️ لعبة المافيا
                <span>اكتشف... خطط... وانتصر</span>
            </div>

            <div class="avatar">
                🕵️
            </div>
        </div>

        <div class="profile">

            <div class="profile-row">

                <div class="profile-avatar">
                    👤
                </div>

                <div>
                    <div id="userName" class="name">
                        لاعب
                    </div>

                    <div id="userLevel" class="level">
                        المستوى 1
                    </div>
                </div>

            </div>

            <div class="xp">

                <div class="xp-label">
                    <span>XP</span>
                    <span id="xpText">0 / 100</span>
                </div>

                <div class="xp-bar">
                    <div
                        id="xpFill"
                        class="xp-fill"
                    ></div>
                </div>

            </div>

            <div class="stats">

                <div class="stat">
                    <strong id="games">0</strong>
                    <span>المباريات</span>
                </div>

                <div class="stat">
                    <strong id="wins">0</strong>
                    <span>الانتصارات</span>
                </div>

                <div class="stat">
                    <strong id="losses">0</strong>
                    <span>الخسائر</span>
                </div>

            </div>

        </div>

        <button
            class="main-button"
            onclick="startGame()"
        >
            🎮 ابدأ اللعب
        </button>

        <div class="section-title">
            حسابك
        </div>

        <div class="grid">

            <button class="card-button">
                <div class="icon">👤</div>
                <strong>حسابي</strong>
                <small>معلومات اللاعب</small>
            </button>

            <button class="card-button">
                <div class="icon">🏆</div>
                <strong>الإنجازات</strong>
                <small>إنجازاتك</small>
            </button>

            <button class="card-button">
                <div class="icon">📊</div>
                <strong>الإحصائيات</strong>
                <small>سجل المباريات</small>
            </button>

            <button class="card-button">
                <div class="icon">⚙️</div>
                <strong>الإعدادات</strong>
                <small>إعدادات اللعبة</small>
            </button>

        </div>

    </section>


    <!-- اللعبة -->

    <section id="game" class="screen">

        <button
            class="back"
            onclick="goHome()"
        >
            ← الرئيسية
        </button>

        <div id="roleBox" class="role">

            <div
                id="roleEmoji"
                class="role-emoji"
            >
                🎭
            </div>

            <div
                id="roleName"
                class="role-name"
            >
                دورك
            </div>

            <div
                id="roleDescription"
                class="role-description"
            >
            </div>

        </div>

        <div
            id="phase"
            class="phase"
        >
            🌙 الليل
        </div>

        <div id="players" class="players">
        </div>

        <div
            id="notice"
            class="notice"
        >
        </div>

        <div
            id="actions"
            class="action-area"
        >
        </div>

    </section>


    <!-- النتيجة -->

    <section id="result" class="screen">

        <div class="result">

            <div
                id="resultIcon"
                class="result-icon"
            >
                🏆
            </div>

            <h1 id="resultTitle">
                انتهت المباراة
            </h1>

            <div
                id="resultText"
                class="notice"
            >
            </div>

            <button
                class="main-button"
                style="margin-top:15px"
                onclick="goHome()"
            >
                🏠 العودة للرئيسية
            </button>

        </div>

    </section>

</div>


<script>

const tg = window.Telegram.WebApp;

tg.ready();
tg.expand();

const API = "";

let currentGame = null;
let polling = null;


function headers() {

    return {
        "X-Telegram-Init-Data":
            tg.initData
    };

}


async function api(
    url,
    options = {}
) {

    options.headers = {
        ...(options.headers || {}),
        ...headers(),
        "Content-Type":
            "application/json"
    };

    const response =
        await fetch(
            API + url,
            options
        );

    const data =
        await response.json();

    if (!response.ok) {
        throw new Error(
            data.detail ||
            "حدث خطأ"
        );
    }

    return data;
}


async function loadMe() {

    try {

        const data =
            await api("/api/me");

        const user =
            data.user;

        document.getElementById(
            "userName"
        ).textContent =
            user.name || "لاعب";

        document.getElementById(
            "userLevel"
        ).textContent =
            "المستوى " +
            user.level;

        document.getElementById(
            "games"
        ).textContent =
            user.games;

        document.getElementById(
            "wins"
        ).textContent =
            user.wins;

        document.getElementById(
            "losses"
        ).textContent =
            user.losses;

        const needed =
            user.level * 100;

        document.getElementById(
            "xpText"
        ).textContent =
            `${user.xp} / ${needed}`;

        document.getElementById(
            "xpFill"
        ).style.width =
            Math.min(
                100,
                (user.xp / needed) * 100
            ) + "%";

        document.getElementById(
            "loading"
        ).classList.add("hidden");

        document.getElementById(
            "app"
        ).classList.remove("hidden");


        if (data.game) {

            currentGame =
                data.game;

            renderGame();

        }

    } catch (error) {

        document.getElementById(
            "loading"
        ).textContent =
            error.message;

    }

}


async function startGame() {

    try {

        const data =
            await api(
                "/api/start",
                {
                    method: "POST"
                }
            );

        currentGame =
            data.game;

        showScreen("game");

        renderGame();

        startPolling();

    } catch (error) {

        alert(error.message);

    }

}


async function sendAction(
    action,
    targetId = null
) {

    try {

        const data =
            await api(
                "/api/action",
                {
                    method: "POST",

                    body: JSON.stringify({
                        action,
                        target_id:
                            targetId
                    })
                }
            );

        currentGame =
            data.game;

        renderGame();

    } catch (error) {

        alert(error.message);

    }

}


function renderGame() {

    if (!currentGame) {
        return;
    }

    if (currentGame.finished) {

        renderResult();

        return;
    }

    showScreen("game");


    // الدور

    const role =
        currentGame.my_role;

    if (role) {

        document.getElementById(
            "roleEmoji"
        ).textContent =
            role.emoji;

        document.getElementById(
            "roleName"
        ).textContent =
            role.name;

        document.getElementById(
            "roleDescription"
        ).textContent =
            role.description;

    }


    // المرحلة

    const phase =
        currentGame.phase;

    document.getElementById(
        "phase"
    ).textContent =
        phase === "night"
        ? "🌙 الليل"
        : "☀️ النهار";


    // اللاعبين

    const container =
        document.getElementById(
            "players"
        );

    container.innerHTML = "";


    for (const player of currentGame.players) {

        const div =
            document.createElement(
                "div"
            );

        div.className =
            "player " +
            (
                player.alive
                ? ""
                : "dead"
            );

        div.innerHTML = `
            <div class="player-left">

                <div class="player-avatar">
                    ${player.human ? "👤" : "🤖"}
                </div>

                <div>
                    <strong>
                        ${escapeHtml(player.name)}
                    </strong>

                    <div style="
                        color:#888;
                        font-size:10px;
                        margin-top:3px;
                    ">
                        ${
                            player.alive
                            ? "على قيد اللعب"
                            : "خرج من المباراة"
                        }
                    </div>
                </div>

            </div>

            <div class="alive-dot"></div>
        `;

        container.appendChild(div);

    }


    document.getElementById(
        "notice"
    ).textContent =
        currentGame.last_event || "";


    renderActions();

}


function renderActions() {

    const box =
        document.getElementById(
            "actions"
        );

    box.innerHTML = "";


    if (!currentGame.my_alive) {

        box.innerHTML =
            `<div class="notice">
                أنت خارج المباراة.
                تابع الأحداث حتى النهاية.
            </div>`;

        return;
    }


    if (currentGame.phase === "night") {

        const role =
            currentGame.my_role;


        if (role === "mafia") {

            box.innerHTML =
                `<div class="notice">
                    🔴 اختر لاعبًا لاستهدافه.
                </div>`;

            addTargetButtons(
                box,
                "mafia_target"
            );

        }

        else if (role === "doctor") {

            box.innerHTML =
                `<div class="notice">
                    💚 اختر لاعبًا للحماية.
                </div>`;

            addTargetButtons(
                box,
                "doctor_target"
            );

        }

        else if (role === "detective") {

            box.innerHTML =
                `<div class="notice">
                    🔎 اختر لاعبًا للتحقق منه.
                </div>`;

            addTargetButtons(
                box,
                "detective_check"
            );

        }

        else {

            box.innerHTML =
                `<div class="notice">
                    🌙 انتظر حتى ينتهي الليل.
                </div>`;

        }

    }

    else if (
        currentGame.phase === "day"
    ) {

        box.innerHTML =
            `<div class="notice">
                🗳️ اختر لاعبًا للتصويت عليه.
            </div>`;

        addTargetButtons(
            box,
            "vote"
        );

    }

}


function addTargetButtons(
    box,
    action
) {

    for (
        const player
        of currentGame.players
    ) {

        if (!player.alive) {
            continue;
        }

        // لا يظهر اللاعب نفسه
        // كهدف للتصويت أو الاختيار
        // إلا إذا كان الدور يسمح بذلك

        if (
            player.human &&
            (
                action === "vote" ||
                action === "detective_check" ||
                action === "mafia_target"
            )
        ) {
            continue;
        }


        const button =
            document.createElement(
                "button"
            );

        button.className =
            "target-button";

        button.textContent =
            (
                player.human
                ? "👤 "
                : "🤖 "
            ) +
            player.name;

        button.onclick =
            () => sendAction(
                action,
                player.id
            );

        box.appendChild(button);

    }

}


function renderResult() {

    showScreen("result");

    const winner =
        currentGame.winner;

    let title = "";
    let text = "";
    let icon = "🏆";

    if (winner === "mafia") {

        title = "المافيا فازت";
        text =
            "انتهت المباراة لصالح المافيا.";
        icon = "🔴";

    }

    else if (
        winner === "citizen"
    ) {

        title = "المدينة فازت";
        text =
            "تمكنت المدينة من إنهاء سيطرة المافيا.";
        icon = "🏆";

    }

    else if (
        winner === "jester"
    ) {

        title = "المهرج فاز";
        text =
            "نجح المهرج في تحقيق هدفه.";
        icon = "🃏";

    }


    document.getElementById(
        "resultIcon"
    ).textContent = icon;

    document.getElementById(
        "resultTitle"
    ).textContent = title;

    document.getElementById(
        "resultText"
    ).textContent = text;

}


function startPolling() {

    if (polling) {
        return;
    }

    polling =
        setInterval(
            async () => {

                try {

                    const data =
                        await api(
                            "/api/game"
                        );

                    currentGame =
                        data.game;

                    if (
                        currentGame
                    ) {

                        renderGame();

                    }

                } catch (e) {}

            },
            2500
        );

}


function showScreen(id) {

    document
        .querySelectorAll(
            ".screen"
        )
        .forEach(
            el =>
                el.classList.remove(
                    "active"
                )
        );

    document
        .getElementById(id)
        .classList.add("active");

}


function goHome() {

    showScreen("home");

    loadMe();

}


function escapeHtml(text) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text;

    return div.innerHTML;

}


loadMe();

</script>

</body>
</html>
"""


# =========================================================
# تشغيل البوت + السيرفر
# =========================================================

async def run():

    if BOT_TOKEN == "ضع_توكن_البوت_هنا":
        print(
            "\n❌ ضع توكن البوت داخل BOT_TOKEN أولاً.\n"
        )
        return

    telegram_app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "game",
            game_command
        )
    )

    print("🤖 تشغيل البوت...")

    await telegram_app.initialize()

    await telegram_app.start()

    await telegram_app.updater.start_polling(
        drop_pending_updates=True
    )

    print("✅ Telegram Bot يعمل")

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    config = uvicorn.Config(
        web,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )

    server = uvicorn.Server(config)

    print(
        f"🌐 Web Server يعمل على المنفذ {port}"
    )

    try:

        await server.serve()

    finally:

        await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()


if __name__ == "__main__":

    asyncio.run(run())