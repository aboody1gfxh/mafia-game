import os
import json
import random
import string
import logging
from typing import Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse


# ============================================================
# CONFIG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mafia-game")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev",
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = WEBAPP_URL + WEBHOOK_PATH

DATA_FILE = "mafia_games.json"

app = FastAPI(title="Mafia Game")

# FastAPI Cloud expects main:web
web = app


# ============================================================
# MEMORY
# ============================================================

games: Dict[str, Dict[str, Any]] = {}


# ============================================================
# STORAGE
# ============================================================

def save_games() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as file:
            json.dump(games, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("Could not save games: %s", exc)


def load_games() -> None:
    global games

    if not os.path.exists(DATA_FILE):
        games = {}
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            games = json.load(file)
    except Exception as exc:
        logger.error("Could not load games: %s", exc)
        games = {}


# ============================================================
# TELEGRAM API
# ============================================================

async def telegram_call(method: str, payload: dict | None = None):
    if not BOT_TOKEN:
        return {
            "ok": False,
            "description": "BOT_TOKEN is not configured",
        }

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json=payload or {},
            )

        try:
            return response.json()
        except Exception:
            return {
                "ok": False,
                "description": response.text,
            }

    except Exception as exc:
        logger.error("Telegram API error: %s", exc)
        return {
            "ok": False,
            "description": str(exc),
        }


async def send_message(chat_id: int | str, text: str, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    if keyboard:
        payload["reply_markup"] = keyboard

    return await telegram_call("sendMessage", payload)


async def answer_callback(callback_id: str, text: str = ""):
    return await telegram_call(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text,
        },
    )


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def main_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🎮 فتح لعبة المافيا",
                    "web_app": {
                        "url": WEBAPP_URL,
                    },
                }
            ],
            [
                {
                    "text": "ℹ️ طريقة اللعب",
                    "callback_data": "help",
                }
            ],
        ]
    }


# ============================================================
# HELPERS
# ============================================================

def make_code(length: int = 5) -> str:
    chars = string.ascii_uppercase + string.digits

    while True:
        code = "".join(random.choice(chars) for _ in range(length))

        if code not in games:
            return code


def user_id_from(data: dict) -> str:
    return str(data.get("user_id", "")).strip()


def user_name_from(data: dict) -> str:
    name = str(data.get("name", "")).strip()

    if not name:
        name = "لاعب"

    return name[:40]


def find_player(game: dict, user_id: str):
    for player in game["players"]:
        if str(player["id"]) == str(user_id):
            return player

    return None


def alive_players(game: dict):
    return [
        player
        for player in game["players"]
        if player.get("alive", True)
    ]


def get_player_role(game: dict, user_id: str):
    player = find_player(game, user_id)

    if not player:
        return None

    return player.get("role")


def role_distribution(count: int):
    if count < 3:
        return None

    mafia = 1
    detective = 0
    doctor = 0

    if count >= 4:
        detective = 1

    if count >= 5:
        doctor = 1

    if count >= 7:
        mafia = 2

    if count >= 10:
        mafia = 3

    citizens = count - mafia - detective - doctor

    if citizens < 1:
        citizens = 1

    return {
        "mafia": mafia,
        "detective": detective,
        "doctor": doctor,
        "citizen": citizens,
    }


def assign_roles(game: dict):
    count = len(game["players"])
    distribution = role_distribution(count)

    if not distribution:
        return False

    roles = []

    roles.extend(["mafia"] * distribution["mafia"])
    roles.extend(["detective"] * distribution["detective"])
    roles.extend(["doctor"] * distribution["doctor"])
    roles.extend(["citizen"] * distribution["citizen"])

    random.shuffle(roles)

    for player, role in zip(game["players"], roles):
        player["role"] = role
        player["alive"] = True
        player["night_action"] = None
        player["vote"] = None
        player["private_message"] = ""

    return True


def role_name(role: str):
    names = {
        "mafia": "🔴 مافيا",
        "detective": "🔎 محقق",
        "doctor": "💉 طبيب",
        "citizen": "👤 مواطن",
    }

    return names.get(role, "👤 لاعب")


def phase_name(phase: str):
    names = {
        "waiting": "انتظار اللاعبين",
        "night": "🌙 الليل",
        "day": "☀️ النهار",
        "finished": "🏁 انتهت اللعبة",
    }

    return names.get(phase, phase)


def winner(game: dict):
    alive = alive_players(game)

    mafia_count = sum(
        1 for p in alive if p.get("role") == "mafia"
    )

    non_mafia_count = len(alive) - mafia_count

    if mafia_count == 0:
        return "citizens"

    if mafia_count >= non_mafia_count:
        return "mafia"

    return None


def winner_text(value: str):
    if value == "mafia":
        return "🔴 المافيا فازت!"

    if value == "citizens":
        return "🟢 المواطنين فازوا!"

    return ""


def game_public(game: dict, user_id: str):
    player = find_player(game, user_id)

    players = []

    for p in game["players"]:
        item = {
            "id": str(p["id"]),
            "name": p["name"],
            "alive": p.get("alive", True),
        }

        if game["phase"] == "finished":
            item["role"] = p.get("role")
        elif player and p["id"] == player["id"]:
            item["role"] = p.get("role")

        players.append(item)

    targets = []

    if player and player.get("alive", True):
        role = player.get("role")

        if game["phase"] == "night":
            for p in alive_players(game):
                if str(p["id"]) == str(user_id):
                    continue

                if role == "mafia":
                    if p.get("role") != "mafia":
                        targets.append({
                            "id": str(p["id"]),
                            "name": p["name"],
                        })

                elif role in ("detective", "doctor"):
                    targets.append({
                        "id": str(p["id"]),
                        "name": p["name"],
                    })

        elif game["phase"] == "day":
            for p in alive_players(game):
                if str(p["id"]) != str(user_id):
                    targets.append({
                        "id": str(p["id"]),
                        "name": p["name"],
                    })

    private_message = ""

    if player:
        private_message = player.get("private_message", "")

    return {
        "code": game["code"],
        "phase": game["phase"],
        "phase_name": phase_name(game["phase"]),
        "host_id": str(game["host_id"]),
        "players": players,
        "targets": targets,
        "my_role": player.get("role") if player else None,
        "my_alive": player.get("alive", True) if player else False,
        "private_message": private_message,
        "message": game.get("message", ""),
        "winner": game.get("winner"),
        "winner_text": winner_text(game.get("winner")),
        "started": game.get("started", False),
    }


# ============================================================
# GAME LOGIC
# ============================================================

def create_game(user_id: str, name: str):
    code = make_code()

    game = {
        "code": code,
        "host_id": user_id,
        "players": [
            {
                "id": user_id,
                "name": name,
                "alive": True,
                "role": None,
                "night_action": None,
                "vote": None,
                "private_message": "",
            }
        ],
        "phase": "waiting",
        "started": False,
        "message": "بانتظار اللاعبين...",
        "winner": None,
    }

    games[code] = game
    save_games()

    return game


def join_game(code: str, user_id: str, name: str):
    code = code.upper().strip()

    game = games.get(code)

    if not game:
        return None, "الغرفة غير موجودة."

    if game["started"]:
        return None, "اللعبة بدأت بالفعل."

    existing = find_player(game, user_id)

    if existing:
        return game, "أنت داخل الغرفة بالفعل."

    if len(game["players"]) >= 20:
        return None, "الغرفة ممتلئة. الحد الأقصى 20 لاعب."

    game["players"].append(
        {
            "id": user_id,
            "name": name,
            "alive": True,
            "role": None,
            "night_action": None,
            "vote": None,
            "private_message": "",
        }
    )

    save_games()

    return game, "تم الانضمام."


def leave_game(code: str, user_id: str):
    game = games.get(code)

    if not game:
        return False, "الغرفة غير موجودة."

    if game["started"]:
        return False, "لا يمكنك مغادرة اللعبة بعد بدايتها."

    game["players"] = [
        p for p in game["players"]
        if str(p["id"]) != str(user_id)
    ]

    if not game["players"]:
        del games[code]
        save_games()
        return True, "تم حذف الغرفة."

    if str(game["host_id"]) == str(user_id):
        game["host_id"] = str(game["players"][0]["id"])

    save_games()

    return True, "تمت المغادرة."


def start_game(code: str, user_id: str):
    game = games.get(code)

    if not game:
        return False, "الغرفة غير موجودة."

    if str(game["host_id"]) != str(user_id):
        return False, "فقط صاحب الغرفة يستطيع بدء اللعبة."

    if game["started"]:
        return False, "اللعبة بدأت بالفعل."

    if len(game["players"]) < 3:
        return False, "تحتاج اللعبة إلى 3 لاعبين على الأقل."

    if len(game["players"]) > 20:
        return False, "الحد الأقصى 20 لاعب."

    if not assign_roles(game):
        return False, "تعذر توزيع الأدوار."

    game["started"] = True
    game["phase"] = "night"
    game["message"] = "🌙 بدأ الليل! أصحاب الأدوار الخاصة اختاروا أهدافكم."

    save_games()

    return True, "بدأت اللعبة."


def night_complete(game: dict):
    alive = alive_players(game)

    for player in alive:
        role = player.get("role")

        if role in ("mafia", "detective", "doctor"):
            if not player.get("night_action"):
                return False

    return True


def resolve_night(game: dict):
    alive = alive_players(game)

    mafia_players = [
        p for p in alive
        if p.get("role") == "mafia"
    ]

    detective_players = [
        p for p in alive
        if p.get("role") == "detective"
    ]

    doctor_players = [
        p for p in alive
        if p.get("role") == "doctor"
    ]

    # ------------------------------------
    # Detective result
    # ------------------------------------

    for detective in detective_players:
        target_id = detective.get("night_action")

        target = find_player(game, str(target_id))

        if target:
            if target.get("role") == "mafia":
                detective["private_message"] = (
                    f"🔎 نتيجة التحقيق: {target['name']} هو مافيا 🔴"
                )
            else:
                detective["private_message"] = (
                    f"🔎 نتيجة التحقيق: {target['name']} ليس مافيا 🟢"
                )

    # ------------------------------------
    # Mafia target
    # ------------------------------------

    kill_targets = []

    for mafia in mafia_players:
        target_id = mafia.get("night_action")

        if target_id:
            kill_targets.append(str(target_id))

    killed_id = None

    if kill_targets:
        killed_id = max(
            set(kill_targets),
            key=kill_targets.count,
        )

    # ------------------------------------
    # Doctor heal
    # ------------------------------------

    healed_ids = set()

    for doctor in doctor_players:
        target_id = doctor.get("night_action")

        if target_id:
            healed_ids.add(str(target_id))

    # ------------------------------------
    # Apply kill
    # ------------------------------------

    night_text = "☀️ انتهى الليل."

    if killed_id and killed_id not in healed_ids:
        target = find_player(game, killed_id)

        if target:
            target["alive"] = False
            night_text = (
                f"☀️ انتهى الليل.\n"
                f"💀 تم إخراج {target['name']} من اللعبة."
            )

    elif killed_id and killed_id in healed_ids:
        night_text = (
            "☀️ انتهى الليل.\n"
            "💉 الطبيب أنقذ اللاعب المستهدف!"
        )

    for player in alive_players(game):
        player["night_action"] = None

    game["phase"] = "day"
    game["message"] = night_text

    result = winner(game)

    if result:
        finish_game(game, result)
        return

    save_games()


def finish_game(game: dict, result: str):
    game["winner"] = result
    game["phase"] = "finished"
    game["started"] = False
    game["message"] = winner_text(result)

    save_games()


def resolve_votes(game: dict):
    alive = alive_players(game)

    counts = {}

    for player in alive:
        vote = player.get("vote")

        if vote:
            vote = str(vote)
            counts[vote] = counts.get(vote, 0) + 1

    for player in game["players"]:
        player["vote"] = None

    if not counts:
        game["message"] = (
            "☀️ لم يتم تسجيل أي أصوات.\n"
            "🌙 يبدأ الليل من جديد."
        )
        game["phase"] = "night"

        save_games()
        return

    highest = max(counts.values())

    winners = [
        player_id
        for player_id, count in counts.items()
        if count == highest
    ]

    if len(winners) != 1:
        game["message"] = (
            "⚖️ حصل تعادل في التصويت.\n"
            "لم يتم إخراج أي لاعب."
        )
        game["phase"] = "night"

        save_games()
        return

    eliminated_id = winners[0]
    eliminated = find_player(game, eliminated_id)

    if eliminated:
        eliminated["alive"] = False

        game["message"] = (
            f"⚖️ نتيجة التصويت:\n"
            f"🚫 تم إخراج {eliminated['name']}."
        )

    result = winner(game)

    if result:
        finish_game(game, result)
        return

    game["phase"] = "night"

    for player in game["players"]:
        if player.get("alive", True):
            player["night_action"] = None

    save_games()


def perform_action(
    code: str,
    user_id: str,
    action: str,
    target_id: str,
):
    game = games.get(code)

    if not game:
        return False, "الغرفة غير موجودة."

    player = find_player(game, user_id)

    if not player:
        return False, "أنت لست داخل اللعبة."

    if not player.get("alive", True):
        return False, "أنت خارج اللعبة."

    target = find_player(game, str(target_id))

    if not target:
        return False, "الهدف غير موجود."

    if not target.get("alive", True):
        return False, "هذا اللاعب خرج من اللعبة."

    if str(target["id"]) == str(user_id):
        return False, "لا يمكنك اختيار نفسك."

    role = player.get("role")

    # ------------------------------------
    # NIGHT
    # ------------------------------------

    if game["phase"] == "night":

        if action == "kill":
            if role != "mafia":
                return False, "هذا الإجراء للمافيا فقط."

            if target.get("role") == "mafia":
                return False, "لا يمكنك استهداف مافيا."

            player["night_action"] = str(target["id"])

        elif action == "check":
            if role != "detective":
                return False, "هذا الإجراء للمحقق فقط."

            player["night_action"] = str(target["id"])

            if target.get("role") == "mafia":
                player["private_message"] = (
                    f"🔎 {target['name']} هو مافيا 🔴"
                )
            else:
                player["private_message"] = (
                    f"🔎 {target['name']} ليس مافيا 🟢"
                )

        elif action == "heal":
            if role != "doctor":
                return False, "هذا الإجراء للطبيب فقط."

            player["night_action"] = str(target["id"])

        else:
            return False, "إجراء غير معروف."

        if night_complete(game):
            resolve_night(game)
        else:
            save_games()

        return True, "تم تسجيل اختيارك."

    # ------------------------------------
    # DAY VOTE
    # ------------------------------------

    if game["phase"] == "day":

        if action != "vote":
            return False, "الآن وقت التصويت."

        player["vote"] = str(target["id"])

        all_voted = all(
            p.get("vote")
            for p in alive_players(game)
        )

        if all_voted:
            resolve_votes(game)
        else:
            save_games()

        return True, "تم تسجيل تصويتك."

    return False, "لا يمكن تنفيذ هذا الإجراء الآن."


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():
    logger.info("🚀 Starting Mafia FastAPI application...")
    load_games()
    logger.info("🌐 FastAPI started without Telegram polling.")


@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Shutting down Mafia application...")
    save_games()
    logger.info("✅ Mafia application stopped.")


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(GAME_HTML)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "mafia-game",
        "telegram_configured": bool(BOT_TOKEN),
        "webapp_url": WEBAPP_URL,
    }


@app.get("/api/status")
async def api_status():
    return {
        "ok": True,
        "games": len(games),
        "telegram_configured": bool(BOT_TOKEN),
    }


# ============================================================
# GAME API
# ============================================================

@app.post("/api/game/create")
async def api_create(data: dict):
    user_id = user_id_from(data)
    name = user_name_from(data)

    if not user_id:
        return JSONResponse(
            {"ok": False, "error": "معرف اللاعب مفقود."},
            status_code=400,
        )

    game = create_game(user_id, name)

    return {
        "ok": True,
        "game": game_public(game, user_id),
    }


@app.post("/api/game/join")
async def api_join(data: dict):
    user_id = user_id_from(data)
    name = user_name_from(data)
    code = str(data.get("code", "")).upper().strip()

    if not user_id:
        return JSONResponse(
            {"ok": False, "error": "معرف اللاعب مفقود."},
            status_code=400,
        )

    if not code:
        return JSONResponse(
            {"ok": False, "error": "أدخل رمز الغرفة."},
            status_code=400,
        )

    game, message = join_game(code, user_id, name)

    if not game:
        return JSONResponse(
            {"ok": False, "error": message},
            status_code=400,
        )

    return {
        "ok": True,
        "message": message,
        "game": game_public(game, user_id),
    }


@app.post("/api/game/leave")
async def api_leave(data: dict):
    user_id = user_id_from(data)
    code = str(data.get("code", "")).upper().strip()

    success, message = leave_game(code, user_id)

    return {
        "ok": success,
        "message": message,
    }


@app.post("/api/game/start")
async def api_start(data: dict):
    user_id = user_id_from(data)
    code = str(data.get("code", "")).upper().strip()

    success, message = start_game(code, user_id)

    if not success:
        return JSONResponse(
            {"ok": False, "error": message},
            status_code=400,
        )

    game = games[code]

    return {
        "ok": True,
        "message": message,
        "game": game_public(game, user_id),
    }


@app.post("/api/game/action")
async def api_action(data: dict):
    user_id = user_id_from(data)
    code = str(data.get("code", "")).upper().strip()
    action = str(data.get("action", "")).strip()
    target_id = str(data.get("target_id", "")).strip()

    success, message = perform_action(
        code,
        user_id,
        action,
        target_id,
    )

    if not success:
        return JSONResponse(
            {"ok": False, "error": message},
            status_code=400,
        )

    game = games.get(code)

    return {
        "ok": True,
        "message": message,
        "game": game_public(game, user_id),
    }


@app.get("/api/game/{code}")
async def api_get_game(code: str, user_id: str = ""):
    code = code.upper().strip()

    game = games.get(code)

    if not game:
        return JSONResponse(
            {"ok": False, "error": "الغرفة غير موجودة."},
            status_code=404,
        )

    return {
        "ok": True,
        "game": game_public(game, user_id),
    }


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": False}

    try:
        if "message" in update:
            message = update["message"]

            chat = message.get("chat", {})
            user = message.get("from", {})

            chat_id = chat.get("id")
            text = message.get("text", "")

            if text == "/start":
                await send_message(
                    chat_id,
                    "🎭 أهلاً بك في لعبة المافيا!\n\n"
                    "اضغط الزر بالأسفل لفتح اللعبة وإنشاء غرفة أو الانضمام إلى غرفة.",
                    main_keyboard(),
                )

            elif text == "/help":
                await send_message(
                    chat_id,
                    "🎭 طريقة اللعب:\n\n"
                    "1️⃣ افتح اللعبة.\n"
                    "2️⃣ أنشئ غرفة أو أدخل رمز غرفة.\n"
                    "3️⃣ اجمع 3 لاعبين أو أكثر.\n"
                    "4️⃣ صاحب الغرفة يبدأ اللعبة.\n"
                    "5️⃣ كل لاعب يحصل على دور سري.\n"
                    "6️⃣ يبدأ الليل ثم النهار والتصويت.\n"
                    "7️⃣ تستمر اللعبة حتى تفوز المافيا أو المواطنين.",
                    main_keyboard(),
                )

        if "callback_query" in update:
            callback = update["callback_query"]

            callback_id = callback.get("id")
            data = callback.get("data")
            message = callback.get("message", {})
            chat = message.get("chat", {})
            chat_id = chat.get("id")

            if callback_id:
                await answer_callback(callback_id)

            if data == "help":
                await send_message(
                    chat_id,
                    "🎭 لعبة المافيا\n\n"
                    "المافيا تحاول التخلص من المواطنين.\n"
                    "المحقق يستطيع كشف هوية لاعب.\n"
                    "الطبيب يستطيع إنقاذ لاعب.\n"
                    "المواطنون يحاولون اكتشاف المافيا عن طريق التصويت.",
                    main_keyboard(),
                )

    except Exception as exc:
        logger.exception("Webhook processing error: %s", exc)

    return {"ok": True}


# ============================================================
# TELEGRAM SETUP
# ============================================================

@app.get("/telegram-test")
async def telegram_test():
    result = await telegram_call("getMe")

    return {
        "ok": bool(result.get("ok")),
        "telegram": result,
    }


@app.get("/setup-webhook")
async def setup_webhook():
    result = await telegram_call(
        "setWebhook",
        {
            "url": WEBHOOK_URL,
            "drop_pending_updates": True,
        },
    )

    return {
        "ok": bool(result.get("ok")),
        "message": "تمت تهيئة Webhook بنجاح." if result.get("ok") else "فشل إعداد Webhook.",
        "webhook_url": WEBHOOK_URL,
        "telegram_result": result,
    }


@app.get("/webhook-info")
async def webhook_info():
    result = await telegram_call("getWebhookInfo")

    return result


# ============================================================
# GAME HTML
# ============================================================

GAME_HTML = r"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0,
             maximum-scale=1.0, user-scalable=no"
/>

<title>لعبة المافيا</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

* {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}

html,
body {
    margin: 0;
    padding: 0;
    min-height: 100%;
    background: #090b12;
    color: #ffffff;
    font-family:
        Arial,
        Tahoma,
        sans-serif;
}

body {
    min-height: 100vh;
}

button,
input {
    font: inherit;
}

button {
    border: 0;
    cursor: pointer;
}

.app {
    width: 100%;
    max-width: 520px;
    min-height: 100vh;
    margin: auto;
    padding: 18px;
}

.header {
    text-align: center;
    padding: 15px 0 20px;
}

.logo {
    font-size: 54px;
    margin-bottom: 5px;
}

.title {
    font-size: 30px;
    font-weight: 900;
}

.subtitle {
    color: #8f96a8;
    margin-top: 7px;
}

.card {
    background: #111521;
    border: 1px solid #242a3b;
    border-radius: 20px;
    padding: 18px;
    margin-bottom: 14px;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.22);
}

.section-title {
    font-size: 19px;
    font-weight: 900;
    margin-bottom: 14px;
}

input {
    width: 100%;
    background: #090c14;
    border: 1px solid #30384d;
    border-radius: 14px;
    color: white;
    padding: 15px;
    outline: none;
    text-align: center;
    margin-bottom: 10px;
}

input:focus {
    border-color: #8d2638;
}

.btn {
    width: 100%;
    padding: 15px;
    border-radius: 14px;
    font-weight: 900;
    margin-top: 9px;
    color: white;
    background: #272d3c;
}

.btn-primary {
    background: #8d2638;
}

.btn-green {
    background: #176b4d;
}

.btn-blue {
    background: #245a8d;
}

.btn-gold {
    background: #85651d;
}

.btn-danger {
    background: #7b1e2b;
}

.btn:disabled {
    opacity: 0.45;
}

.hidden {
    display: none !important;
}

.room-code {
    text-align: center;
    font-size: 38px;
    letter-spacing: 8px;
    font-weight: 900;
    color: #e6b84a;
    padding: 12px;
}

.player {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px;
    background: #090c14;
    border-radius: 13px;
    margin-top: 8px;
}

.player-name {
    font-weight: 800;
}

.player-status {
    font-size: 12px;
    color: #7f8799;
}

.dead {
    opacity: 0.4;
}

.phase {
    text-align: center;
    font-size: 24px;
    font-weight: 900;
    padding: 12px;
}

.role {
    text-align: center;
    padding: 20px;
    border-radius: 17px;
    background: #090c14;
    margin-bottom: 14px;
}

.role-icon {
    font-size: 42px;
}

.role-name {
    font-size: 24px;
    font-weight: 900;
    margin-top: 5px;
}

.message {
    background: #171c29;
    border-radius: 14px;
    padding: 13px;
    line-height: 1.7;
    margin-bottom: 14px;
}

.target {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #090c14;
    border: 1px solid #262d3f;
    border-radius: 14px;
    padding: 12px;
    margin-top: 8px;
}

.target-name {
    font-weight: 800;
}

.small {
    color: #8f96a8;
    font-size: 13px;
}

.notice {
    text-align: center;
    color: #8f96a8;
    line-height: 1.7;
}

.winner {
    text-align: center;
    font-size: 25px;
    font-weight: 900;
    padding: 25px 5px;
}

</style>
</head>

<body>

<div class="app">

    <div class="header">
        <div class="logo">🎭</div>
        <div class="title">لعبة المافيا</div>
        <div class="subtitle">لعبة اجتماعية داخل Telegram</div>
    </div>


    <!-- HOME -->

    <section id="home">

        <div class="card">

            <div class="section-title">
                🎮 ابدأ لعبة جديدة
            </div>

            <div class="notice">
                أنشئ غرفة وأرسل رمزها لأصدقائك.
            </div>

            <button
                class="btn btn-primary"
                onclick="createRoom()"
            >
                إنشاء غرفة
            </button>

        </div>


        <div class="card">

            <div class="section-title">
                🚪 الانضمام إلى غرفة
            </div>

            <input
                id="joinCode"
                placeholder="رمز الغرفة"
                maxlength="5"
                autocomplete="off"
            >

            <button
                class="btn btn-green"
                onclick="joinRoom()"
            >
                انضمام
            </button>

        </div>

    </section>


    <!-- ROOM -->

    <section id="room" class="hidden">

        <div class="card">

            <div class="section-title">
                🎭 غرفة اللعبة
            </div>

            <div id="roomCode" class="room-code">
                -----
            </div>

            <div class="notice">
                أرسل هذا الرمز إلى اللاعبين.
            </div>

            <button
                class="btn btn-blue"
                onclick="copyRoomCode()"
            >
                📋 نسخ الرمز
            </button>

        </div>


        <div class="card">

            <div class="section-title">
                👥 اللاعبين
            </div>

            <div id="roomPlayers"></div>

        </div>


        <div class="card">

            <button
                id="startButton"
                class="btn btn-primary"
                onclick="startGame()"
            >
                🚀 بدء اللعبة
            </button>

            <button
                class="btn btn-danger"
                onclick="leaveRoom()"
            >
                مغادرة الغرفة
            </button>

        </div>

    </section>


    <!-- GAME -->

    <section id="game" class="hidden">

        <div class="card">

            <div id="phase" class="phase">
                🌙 الليل
            </div>

            <div id="roleBox" class="role">
                <div class="role-icon">❓</div>
                <div class="role-name">جاري التحميل...</div>
            </div>

            <div id="message" class="message">
                ...
            </div>

            <div id="privateMessage"
                 class="message hidden">
            </div>

        </div>


        <div
            id="winnerBox"
            class="card hidden"
        >
            <div id="winnerText" class="winner"></div>

            <button
                class="btn btn-primary"
                onclick="goHome()"
            >
                🏠 العودة
            </button>
        </div>


        <div
            id="actionBox"
            class="card"
        >

            <div class="section-title">
                🎯 اختَر لاعبًا
            </div>

            <div
                id="targets"
            ></div>

        </div>


        <div class="card">

            <div class="section-title">
                👥 اللاعبون
            </div>

            <div id="gamePlayers"></div>

        </div>

    </section>

</div>


<script>

const tg =
    window.Telegram &&
    window.Telegram.WebApp
        ? window.Telegram.WebApp
        : null;

if (tg) {
    tg.ready();
    tg.expand();
}


let currentCode = "";
let currentUserId = "";
let currentName = "";
let currentGame = null;


function initUser() {

    if (
        tg &&
        tg.initDataUnsafe &&
        tg.initDataUnsafe.user
    ) {

        const user =
            tg.initDataUnsafe.user;

        currentUserId =
            String(user.id);

        currentName =
            user.first_name ||
            user.username ||
            "لاعب";

    } else {

        let demo =
            localStorage.getItem(
                "mafia_demo_user"
            );

        if (!demo) {

            demo =
                "demo_" +
                Math.random()
                    .toString(36)
                    .substring(2, 10);

            localStorage.setItem(
                "mafia_demo_user",
                demo
            );
        }

        currentUserId = demo;
        currentName = "لاعب تجريبي";
    }
}


initUser();


function show(id) {

    document
        .querySelectorAll(
            "#home, #room, #game"
        )
        .forEach(
            el => el.classList.add("hidden")
        );

    document
        .getElementById(id)
        .classList.remove("hidden");
}


function notify(message) {

    if (
        tg &&
        tg.showAlert
    ) {
        tg.showAlert(message);
    } else {
        alert(message);
    }
}


async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            {
                headers: {
                    "Content-Type":
                        "application/json",
                },
                ...options,
            }
        );

    const data =
        await response.json();

    if (!response.ok) {

        throw new Error(
            data.error ||
            "حدث خطأ."
        );
    }

    return data;
}


async function createRoom() {

    try {

        const data =
            await api(
                "/api/game/create",
                {
                    method: "POST",
                    body: JSON.stringify({
                        user_id:
                            currentUserId,
                        name:
                            currentName,
                    }),
                }
            );

        currentCode =
            data.game.code;

        currentGame =
            data.game;

        renderRoom();

        show("room");

        startPolling();

    } catch (error) {

        notify(error.message);

    }
}


async function joinRoom() {

    const input =
        document.getElementById(
            "joinCode"
        );

    const code =
        input.value
            .trim()
            .toUpperCase();

    if (!code) {

        notify(
            "اكتب رمز الغرفة أولاً."
        );

        return;
    }

    try {

        const data =
            await api(
                "/api/game/join",
                {
                    method: "POST",
                    body: JSON.stringify({
                        code: code,
                        user_id:
                            currentUserId,
                        name:
                            currentName,
                    }),
                }
            );

        currentCode =
            data.game.code;

        currentGame =
            data.game;

        renderRoom();

        show("room");

        startPolling();

    } catch (error) {

        notify(error.message);

    }
}


async function startGame() {

    if (!currentCode) {
        return;
    }

    try {

        const data =
            await api(
                "/api/game/start",
                {
                    method: "POST",
                    body: JSON.stringify({
                        code:
                            currentCode,
                        user_id:
                            currentUserId,
                    }),
                }
            );

        currentGame =
            data.game;

        show("game");

        renderGame();

    } catch (error) {

        notify(error.message);

    }
}


async function leaveRoom() {

    if (!currentCode) {
        return;
    }

    try {

        await api(
            "/api/game/leave",
            {
                method: "POST",
                body: JSON.stringify({
                    code:
                        currentCode,
                    user_id:
                        currentUserId,
                }),
            }
        );

    } catch (error) {
        console.log(error);
    }

    currentCode = "";
    currentGame = null;

    stopPolling();

    show("home");
}


function renderRoom() {

    if (!currentGame) {
        return;
    }

    document
        .getElementById("roomCode")
        .textContent =
        currentGame.code;

    const container =
        document.getElementById(
            "roomPlayers"
        );

    container.innerHTML = "";

    for (
        const player
        of currentGame.players
    ) {

        const div =
            document.createElement(
                "div"
            );

        div.className = "player";

        div.innerHTML = `
            <div>
                <div class="player-name">
                    ${escapeHtml(player.name)}
                </div>
                <div class="player-status">
                    لاعب
                </div>
            </div>
            <div>
                ${
                    String(player.id) ===
                    String(currentGame.host_id)
                    ? "👑"
                    : "👤"
                }
            </div>
        `;

        container.appendChild(div);
    }

    const startButton =
        document.getElementById(
            "startButton"
        );

    if (
        String(currentGame.host_id) ===
        String(currentUserId)
    ) {

        startButton.classList.remove(
            "hidden"
        );

        startButton.disabled =
            currentGame.players.length < 3;

    } else {

        startButton.classList.add(
            "hidden"
        );
    }
}


function renderGame() {

    if (!currentGame) {
        return;
    }

    document
        .getElementById("phase")
        .textContent =
        currentGame.phase_name;


    const roleBox =
        document.getElementById(
            "roleBox"
        );

    const role =
        currentGame.my_role;

    const roleIcons = {
        mafia: "🔴",
        detective: "🔎",
        doctor: "💉",
        citizen: "👤",
    };

    const roleNames = {
        mafia: "مافيا",
        detective: "محقق",
        doctor: "طبيب",
        citizen: "مواطن",
    };

    roleBox.innerHTML = `
        <div class="role-icon">
            ${roleIcons[role] || "❓"}
        </div>
        <div class="role-name">
            ${roleNames[role] || "غير معروف"}
        </div>
    `;


    document
        .getElementById("message")
        .textContent =
        currentGame.message ||
        "";


    const privateBox =
        document.getElementById(
            "privateMessage"
        );

    if (
        currentGame.private_message
    ) {

        privateBox.textContent =
            currentGame.private_message;

        privateBox.classList.remove(
            "hidden"
        );

    } else {

        privateBox.classList.add(
            "hidden"
        );
    }


    renderPlayers();
    renderTargets();


    if (
        currentGame.phase ===
            "finished" ||
        currentGame.winner
    ) {

        document
            .getElementById(
                "winnerBox"
            )
            .classList.remove(
                "hidden"
            );

        document
            .getElementById(
                "winnerText"
            )
            .textContent =
            currentGame.winner_text ||
            "انتهت اللعبة.";

    } else {

        document
            .getElementById(
                "winnerBox"
            )
            .classList.add(
                "hidden"
            );
    }
}


function renderPlayers() {

    const container =
        document.getElementById(
            "gamePlayers"
        );

    container.innerHTML = "";

    for (
        const player
        of currentGame.players
    ) {

        const div =
            document.createElement(
                "div"
            );

        div.className =
            "player" +
            (
                player.alive
                    ? ""
                    : " dead"
            );

        let roleText = "";

        if (
            player.role
        ) {

            const names = {
                mafia: "🔴 مافيا",
                detective: "🔎 محقق",
                doctor: "💉 طبيب",
                citizen: "👤 مواطن",
            };

            roleText =
                names[player.role] ||
                "";
        }

        div.innerHTML = `
            <div>
                <div class="player-name">
                    ${escapeHtml(player.name)}
                </div>
                <div class="player-status">
                    ${
                        player.alive
                            ? "على قيد الحياة"
                            : "خارج اللعبة"
                    }
                </div>
            </div>

            <div>
                ${
                    player.alive
                        ? "🟢"
                        : "💀"
                }

                ${
                    roleText
                        ? "<br>" +
                          roleText
                        : ""
                }
            </div>
        `;

        container.appendChild(div);
    }
}


function renderTargets() {

    const container =
        document.getElementById(
            "targets"
        );

    container.innerHTML = "";

    const targets =
        currentGame.targets || [];


    if (
        currentGame.phase ===
        "finished"
    ) {

        document
            .getElementById(
                "actionBox"
            )
            .classList.add(
                "hidden"
            );

        return;
    }


    if (
        !currentGame.my_alive
    ) {

        container.innerHTML = `
            <div class="notice">
                💀 أنت خارج اللعبة ولا يمكنك تنفيذ أي إجراء.
            </div>
        `;

        return;
    }


    if (
        currentGame.phase ===
        "night"
    ) {

        if (
            currentGame.my_role ===
            "citizen"
        ) {

            container.innerHTML = `
                <div class="notice">
                    🌙 أنت مواطن.
                    انتظر انتهاء الليل.
                </div>
            `;

            return;
        }

    }


    if (!targets.length) {

        container.innerHTML = `
            <div class="notice">
                لا يوجد هدف متاح حالياً.
            </div>
        `;

        return;
    }


    for (
        const target
        of targets
    ) {

        const row =
            document.createElement(
                "div"
            );

        row.className = "target";

        const action =
            getAction();

        const button =
            document.createElement(
                "button"
            );

        button.className =
            "btn btn-primary";

        button.style.width =
            "auto";

        button.style.margin =
            "0";

        button.style.padding =
            "10px 15px";

        button.textContent =
            action === "vote"
                ? "🗳️ تصويت"
                : "اختيار";


        button.onclick =
            () => sendAction(
                action,
                target.id
            );


        row.innerHTML = `
            <div class="target-name">
                ${escapeHtml(target.name)}
            </div>
        `;

        row.appendChild(button);

        container.appendChild(row);
    }
}


function getAction() {

    if (
        currentGame.phase ===
        "day"
    ) {
        return "vote";
    }

    if (
        currentGame.my_role ===
        "mafia"
    ) {
        return "kill";
    }

    if (
        currentGame.my_role ===
        "detective"
    ) {
        return "check";
    }

    if (
        currentGame.my_role ===
        "doctor"
    ) {
        return "heal";
    }

    return "";
}


async function sendAction(
    action,
    targetId
) {

    if (!action) {
        return;
    }

    try {

        const data =
            await api(
                "/api/game/action",
                {
                    method: "POST",
                    body: JSON.stringify({
                        code:
                            currentCode,
                        user_id:
                            currentUserId,
                        action:
                            action,
                        target_id:
                            targetId,
                    }),
                }
            );

        currentGame =
            data.game;

        renderGame();

    } catch (error) {

        notify(error.message);

    }
}


async function refreshGame() {

    if (!currentCode) {
        return;
    }

    try {

        const data =
            await api(
                "/api/game/" +
                encodeURIComponent(
                    currentCode
                ) +
                "?user_id=" +
                encodeURIComponent(
                    currentUserId
                )
            );

        currentGame =
            data.game;


        if (
            currentGame.started ||
            currentGame.phase !==
                "waiting"
        ) {

            show("game");
            renderGame();

        } else {

            show("room");
            renderRoom();
        }

    } catch (error) {

        console.log(
            "Polling:",
            error.message
        );
    }
}


let pollingTimer = null;


function startPolling() {

    stopPolling();

    pollingTimer =
        setInterval(
            refreshGame,
            1500
        );
}


function stopPolling() {

    if (
        pollingTimer
    ) {

        clearInterval(
            pollingTimer
        );

        pollingTimer =
            null;
    }
}


async function copyRoomCode() {

    if (!currentCode) {
        return;
    }

    try {

        await navigator.clipboard.writeText(
            currentCode
        );

        notify(
            "تم نسخ رمز الغرفة ✅"
        );

    } catch {

        notify(
            "رمز الغرفة: " +
            currentCode
        );
    }
}


function goHome() {

    currentCode = "";
    currentGame = null;

    stopPolling();

    show("home");
}


function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


show("home");

</script>

</body>
</html>
"""


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:web",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )