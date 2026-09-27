import os
import json
import random
import string
import sqlite3
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev"
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = WEBAPP_URL + WEBHOOK_PATH

DATABASE_FILE = "mafia.db"

MIN_PLAYERS = 3
MAX_PLAYERS = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("mafia-game")


# ============================================================
# DATABASE
# ============================================================

db_lock = asyncio.Lock()


def db_connect():
    connection = sqlite3.connect(
        DATABASE_FILE,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():
    connection = db_connect()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host_id TEXT NOT NULL,
            max_players INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            phase TEXT NOT NULL DEFAULT 'waiting',
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT,
            alive INTEGER NOT NULL DEFAULT 1,
            is_ai INTEGER NOT NULL DEFAULT 0,
            night_action TEXT,
            vote TEXT,
            private_message TEXT DEFAULT '',
            UNIQUE(room_code, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()

    logger.info("SQLite database initialized.")


def db_execute(query, params=(), fetch=False, many=False):
    connection = db_connect()
    cursor = connection.cursor()

    if many:
        cursor.executemany(query, params)
    else:
        cursor.execute(query, params)

    if fetch:
        result = cursor.fetchall()
    else:
        result = None

    connection.commit()
    connection.close()

    return result


def db_one(query, params=()):
    connection = db_connect()
    cursor = connection.cursor()

    cursor.execute(query, params)
    result = cursor.fetchone()

    connection.close()

    return result


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Mafia FastAPI application...")

    init_database()

    logger.info("🌐 FastAPI started successfully.")

    yield

    logger.info("🛑 Shutting down Mafia application...")


app = FastAPI(
    title="Mafia Game",
    lifespan=lifespan
)

web = app


# ============================================================
# WEBSOCKET CONNECTIONS
# ============================================================

connections = {}


class ConnectionManager:

    def __init__(self):
        self.rooms = {}

    async def connect(self, room_code, websocket):

        if room_code not in self.rooms:
            self.rooms[room_code] = set()

        self.rooms[room_code].add(websocket)

        await websocket.accept()

    def disconnect(self, room_code, websocket):

        if room_code in self.rooms:

            self.rooms[room_code].discard(websocket)

            if not self.rooms[room_code]:
                del self.rooms[room_code]

    async def broadcast(self, room_code, message):

        sockets = self.rooms.get(room_code, set())

        dead = []

        for websocket in sockets:

            try:
                await websocket.send_json(message)

            except Exception:
                dead.append(websocket)

        for websocket in dead:
            self.disconnect(room_code, websocket)


manager = ConnectionManager()


# ============================================================
# TELEGRAM API
# ============================================================

async def telegram_call(method, payload=None):

    if not BOT_TOKEN:

        return {
            "ok": False,
            "description": "BOT_TOKEN is not configured"
        }

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:

        async with httpx.AsyncClient(
            timeout=20
        ) as client:

            response = await client.post(
                url,
                json=payload or {}
            )

        try:
            return response.json()

        except Exception:

            return {
                "ok": False,
                "description": response.text
            }

    except Exception as exc:

        logger.error(
            "Telegram API error: %s",
            exc
        )

        return {
            "ok": False,
            "description": str(exc)
        }


async def send_message(
    chat_id,
    text,
    keyboard=None
):

    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        payload["reply_markup"] = keyboard

    return await telegram_call(
        "sendMessage",
        payload
    )


async def answer_callback(
    callback_id,
    text=""
):

    return await telegram_call(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text
        }
    )


def telegram_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "🎭 فتح لعبة المافيا",
                    "web_app": {
                        "url": WEBAPP_URL
                    }
                }
            ],

            [
                {
                    "text": "ℹ️ طريقة اللعب",
                    "callback_data": "help"
                }
            ]

        ]
    }


# ============================================================
# GENERAL HELPERS
# ============================================================

def now():

    return datetime.utcnow().isoformat()


def create_room_code():

    characters = (
        string.ascii_uppercase +
        string.digits
    )

    while True:

        code = "".join(
            random.choice(characters)
            for _ in range(5)
        )

        room = db_one(
            "SELECT code FROM rooms WHERE code = ?",
            (code,)
        )

        if not room:
            return code


def clean_name(name):

    name = str(name or "").strip()

    if not name:
        name = "لاعب"

    return name[:30]


def clean_text(text):

    text = str(text or "").strip()

    return text[:500]


def role_name(role):

    names = {

        "mafia":
            "🔴 المافيا",

        "detective":
            "🔎 المحقق",

        "doctor":
            "💉 الطبيب",

        "citizen":
            "👤 المواطن"

    }

    return names.get(
        role,
        "👤 لاعب"
    )


def phase_name(phase):

    names = {

        "waiting":
            "🏠 انتظار اللاعبين",

        "night":
            "🌙 الليل",

        "day":
            "☀️ النهار",

        "vote":
            "🗳️ التصويت",

        "finished":
            "🏆 انتهت اللعبة"

    }

    return names.get(
        phase,
        phase
    )


# ============================================================
# ROLE DISTRIBUTION
# ============================================================

def distribute_roles(count):

    if count < 3:
        return []

    mafia = max(
        1,
        round(count / 4)
    )

    detective = 1 if count >= 4 else 0

    doctor = 1 if count >= 5 else 0

    citizen = (
        count -
        mafia -
        detective -
        doctor
    )

    if citizen < 1:

        citizen = 1

        while (
            mafia +
            detective +
            doctor +
            citizen
            > count
        ):

            if mafia > 1:
                mafia -= 1

            elif doctor:
                doctor -= 1

            elif detective:
                detective -= 1

            else:
                break

    roles = []

    roles += ["mafia"] * mafia
    roles += ["detective"] * detective
    roles += ["doctor"] * doctor
    roles += ["citizen"] * citizen

    while len(roles) < count:
        roles.append("citizen")

    while len(roles) > count:
        roles.pop()

    random.shuffle(roles)

    return roles


# ============================================================
# ROOM DATA
# ============================================================

def get_room(code):

    return db_one(
        """
        SELECT *
        FROM rooms
        WHERE code = ?
        """,
        (code,)
    )


def get_players(code):

    return db_execute(
        """
        SELECT *
        FROM players
        WHERE room_code = ?
        ORDER BY id ASC
        """,
        (code,),
        fetch=True
    )


def get_player(code, user_id):

    return db_one(
        """
        SELECT *
        FROM players
        WHERE room_code = ?
        AND user_id = ?
        """,
        (
            code,
            str(user_id)
        )
    )


def get_alive_players(code):

    return db_execute(
        """
        SELECT *
        FROM players
        WHERE room_code = ?
        AND alive = 1
        ORDER BY id ASC
        """,
        (code,),
        fetch=True
    )


def player_exists(code, user_id):

    player = get_player(
        code,
        user_id
    )

    return player is not None


# ============================================================
# PUBLIC ROOM LIST
# ============================================================

def public_rooms():

    rooms = db_execute(
        """
        SELECT
            r.code,
            r.name,
            r.host_id,
            r.max_players,
            r.status,
            r.phase,
            COUNT(p.id) AS player_count
        FROM rooms r
        LEFT JOIN players p
            ON p.room_code = r.code
        WHERE r.status = 'waiting'
        GROUP BY r.code
        ORDER BY r.created_at DESC
        """,
        fetch=True
    )

    result = []

    for room in rooms:

        if room["player_count"] >= room["max_players"]:
            continue

        result.append({

            "code":
                room["code"],

            "name":
                room["name"],

            "host_id":
                room["host_id"],

            "players":
                room["player_count"],

            "max_players":
                room["max_players"],

            "status":
                room["status"],

            "phase":
                room["phase"]

        })

    return result


# ============================================================
# ROOM CREATION
# ============================================================

async def create_room(
    user_id,
    player_name,
    room_name,
    max_players
):

    room_name = clean_name(room_name)

    max_players = int(max_players)

    if max_players < MIN_PLAYERS:
        max_players = MIN_PLAYERS

    if max_players > MAX_PLAYERS:
        max_players = MAX_PLAYERS

    code = create_room_code()

    db_execute(
        """
        INSERT INTO rooms
        (
            code,
            name,
            host_id,
            max_players,
            status,
            phase,
            created_at
        )
        VALUES (?, ?, ?, ?, 'waiting', 'waiting', ?)
        """,
        (
            code,
            room_name,
            str(user_id),
            max_players,
            now()
        )
    )

    db_execute(
        """
        INSERT INTO players
        (
            room_code,
            user_id,
            name,
            alive,
            is_ai
        )
        VALUES (?, ?, ?, 1, 0)
        """,
        (
            code,
            str(user_id),
            clean_name(player_name)
        )
    )

    return code


# ============================================================
# JOIN ROOM
# ============================================================

async def join_room(
    code,
    user_id,
    player_name
):

    code = str(code).upper().strip()

    room = get_room(code)

    if not room:
        return False, "الغرفة غير موجودة."

    if room["status"] != "waiting":
        return False, "اللعبة بدأت بالفعل."

    existing = get_player(
        code,
        user_id
    )

    if existing:
        return True, "أنت داخل الغرفة بالفعل."

    players = get_players(code)

    if len(players) >= room["max_players"]:
        return False, "الغرفة ممتلئة."

    db_execute(
        """
        INSERT INTO players
        (
            room_code,
            user_id,
            name,
            alive,
            is_ai
        )
        VALUES (?, ?, ?, 1, 0)
        """,
        (
            code,
            str(user_id),
            clean_name(player_name)
        )
    )

    await manager.broadcast(
        code,
        {
            "type": "room_update"
        }
    )

    return True, "تم الانضمام."


# ============================================================
# REMOVE PLAYER
# ============================================================

async def leave_room(
    code,
    user_id
):

    room = get_room(code)

    if not room:
        return False, "الغرفة غير موجودة."

    player = get_player(
        code,
        user_id
    )

    if not player:
        return False, "أنت لست داخل الغرفة."

    if room["status"] != "waiting":
        return False, "لا يمكنك مغادرة اللعبة الآن."

    db_execute(
        """
        DELETE FROM players
        WHERE room_code = ?
        AND user_id = ?
        """,
        (
            code,
            str(user_id)
        )
    )

    remaining = get_players(code)

    if not remaining:

        db_execute(
            "DELETE FROM rooms WHERE code = ?",
            (code,)
        )

        db_execute(
            "DELETE FROM messages WHERE room_code = ?",
            (code,)
        )

        return True, "تم حذف الغرفة."

    if str(room["host_id"]) == str(user_id):

        new_host = remaining[0]["user_id"]

        db_execute(
            """
            UPDATE rooms
            SET host_id = ?
            WHERE code = ?
            """,
            (
                str(new_host),
                code
            )
        )

    await manager.broadcast(
        code,
        {
            "type": "room_update"
        }
    )

    return True, "تمت المغادرة."


# ============================================================
# START ONLINE GAME
# ============================================================

async def start_online_game(
    code,
    user_id
):

    room = get_room(code)

    if not room:
        return False, "الغرفة غير موجودة."

    if str(room["host_id"]) != str(user_id):
        return False, "فقط صاحب الغرفة يستطيع بدء اللعبة."

    players = get_players(code)

    if len(players) < MIN_PLAYERS:
        return False, "تحتاج إلى 3 لاعبين على الأقل."

    if len(players) > room["max_players"]:
        return False, "عدد اللاعبين أكبر من الحد."

    roles = distribute_roles(
        len(players)
    )

    for player, role in zip(
        players,
        roles
    ):

        db_execute(
            """
            UPDATE players
            SET
                role = ?,
                alive = 1,
                night_action = NULL,
                vote = NULL,
                private_message = ''
            WHERE id = ?
            """,
            (
                role,
                player["id"]
            )
        )

    db_execute(
        """
        UPDATE rooms
        SET
            status = 'playing',
            phase = 'night'
        WHERE code = ?
        """,
        (code,)
    )

    await manager.broadcast(
        code,
        {
            "type": "game_started"
        }
    )

    return True, "بدأت اللعبة."


# ============================================================
# AI PLAYERS
# ============================================================

def create_ai_name(existing):

    names = [
        "سالم",
        "كرار",
        "علي",
        "محمد",
        "حسين",
        "زهراء",
        "نور",
        "مصطفى",
        "عباس",
        "سجاد",
        "مريم",
        "ياسين",
        "حيدر",
        "فاطمة",
        "مرتضى",
        "آدم",
        "ليان",
        "عمر",
        "زينب",
        "رؤى"
    ]

    used = set(existing)

    available = [
        name
        for name in names
        if name not in used
    ]

    if available:
        return random.choice(
            available
        )

    return "AI-" + str(
        random.randint(100, 999)
    )


async def create_single_player_game(
    user_id,
    player_name,
    count
):

    count = int(count)

    if count < 3:
        count = 3

    if count > 20:
        count = 20

    code = create_room_code()

    db_execute(
        """
        INSERT INTO rooms
        (
            code,
            name,
            host_id,
            max_players,
            status,
            phase,
            created_at
        )
        VALUES (?, ?, ?, ?, 'playing', 'night', ?)
        """,
        (
            code,
            "🤖 لعب فردي",
            str(user_id),
            count,
            now()
        )
    )

    db_execute(
        """
        INSERT INTO players
        (
            room_code,
            user_id,
            name,
            alive,
            is_ai
        )
        VALUES (?, ?, ?, 1, 0)
        """,
        (
            code,
            str(user_id),
            clean_name(player_name)
        )
    )

    existing_names = [
        clean_name(player_name)
    ]

    for index in range(count - 1):

        name = create_ai_name(
            existing_names
        )

        existing_names.append(name)

        ai_id = (
            "ai_" +
            code +
            "_" +
            str(index)
        )

        db_execute(
            """
            INSERT INTO players
            (
                room_code,
                user_id,
                name,
                alive,
                is_ai
            )
            VALUES (?, ?, ?, 1, 1)
            """,
            (
                code,
                ai_id,
                name
            )
        )

    players = get_players(code)

    roles = distribute_roles(
        len(players)
    )

    for player, role in zip(
        players,
        roles
    ):

        db_execute(
            """
            UPDATE players
            SET role = ?
            WHERE id = ?
            """,
            (
                role,
                player["id"]
            )
        )

    return code


# ============================================================
# AI LOGIC
# ============================================================

def ai_choose_target(
    code,
    player
):

    alive = get_alive_players(code)

    candidates = [
        p
        for p in alive
        if p["id"] != player["id"]
    ]

    if not candidates:
        return None

    if player["role"] == "mafia":

        non_mafia = [
            p
            for p in candidates
            if p["role"] != "mafia"
        ]

        if non_mafia:
            candidates = non_mafia

    return random.choice(candidates)


def ai_night_actions(code):

    players = get_alive_players(code)

    for player in players:

        if not player["is_ai"]:
            continue

        role = player["role"]

        target = ai_choose_target(
            code,
            player
        )

        if not target:
            continue

        if role == "mafia":

            db_execute(
                """
                UPDATE players
                SET night_action = ?
                WHERE id = ?
                """,
                (
                    target["user_id"],
                    player["id"]
                )
            )

        elif role == "detective":

            if target["user_id"] != player["user_id"]:

                result = (
                    f"🔎 نتيجة التحقيق: "
                    f"{target['name']} "
                    +
                    (
                        "هو مافيا 🔴"
                        if target["role"] == "mafia"
                        else "ليس مافيا 🟢"
                    )
                )

                db_execute(
                    """
                    UPDATE players
                    SET
                        night_action = ?,
                        private_message = ?
                    WHERE id = ?
                    """,
                    (
                        target["user_id"],
                        result,
                        player["id"]
                    )
                )

        elif role == "doctor":

            db_execute(
                """
                UPDATE players
                SET night_action = ?
                WHERE id = ?
                """,
                (
                    target["user_id"],
                    player["id"]
                )
            )


# ============================================================
# NIGHT RESOLUTION
# ============================================================

async def resolve_night(code):

    room = get_room(code)

    if not room:
        return

    players = get_alive_players(code)

    mafia = [
        p
        for p in players
        if p["role"] == "mafia"
    ]

    doctors = [
        p
        for p in players
        if p["role"] == "doctor"
    ]

    kill_votes = []

    for mafia_player in mafia:

        action = mafia_player[
            "night_action"
        ]

        if action:
            kill_votes.append(
                action
            )

    kill_target = None

    if kill_votes:

        kill_target = max(
            set(kill_votes),
            key=kill_votes.count
        )

    healed = set()

    for doctor in doctors:

        if doctor["night_action"]:
            healed.add(
                doctor["night_action"]
            )

    message = (
        "☀️ انتهى الليل."
    )

    if kill_target:

        target = get_player(
            code,
            kill_target
        )

        if target:

            if kill_target in healed:

                message = (
                    "💉 الطبيب أنقذ "
                    f"{target['name']} "
                    "هذه الليلة!"
                )

            else:

                db_execute(
                    """
                    UPDATE players
                    SET alive = 0
                    WHERE id = ?
                    """,
                    (
                        target["id"],
                    )
                )

                message = (
                    "💀 في الصباح، اكتشف "
                    f"الجميع أن {target['name']} "
                    "خرج من اللعبة."
                )

    db_execute(
        """
        UPDATE players
        SET night_action = NULL
        WHERE room_code = ?
        """,
        (code,)
    )

    result = check_winner(code)

    if result:

        await finish_game(
            code,
            result
        )

        return

    db_execute(
        """
        UPDATE rooms
        SET phase = 'day'
        WHERE code = ?
        """,
        (code,)
    )

    await add_system_message(
        code,
        message
    )

    await manager.broadcast(
        code,
        {
            "type": "phase_changed"
        }
    )


# ============================================================
# WINNER
# ============================================================

def check_winner(code):

    players = get_alive_players(code)

    mafia_count = sum(
        1
        for p in players
        if p["role"] == "mafia"
    )

    others = len(players) - mafia_count

    if mafia_count == 0:
        return "citizens"

    if mafia_count >= others:
        return "mafia"

    return None


async def finish_game(
    code,
    winner
):

    db_execute(
        """
        UPDATE rooms
        SET
            status = 'finished',
            phase = 'finished'
        WHERE code = ?
        """,
        (code,)
    )

    if winner == "mafia":

        text = (
            "🔴 المافيا فازت!"
        )

    else:

        text = (
            "🟢 المواطنين فازوا!"
        )

    await add_system_message(
        code,
        text
    )

    await manager.broadcast(
        code,
        {
            "type": "game_finished",
            "winner": winner
        }
    )


# ============================================================
# VOTING
# ============================================================

async def submit_vote(
    code,
    user_id,
    target_id
):

    room = get_room(code)

    if not room:
        return False, "الغرفة غير موجودة."

    if room["phase"] != "vote":
        return False, "التصويت غير مفتوح."

    player = get_player(
        code,
        user_id
    )

    if not player:
        return False, "أنت لست داخل اللعبة."

    if not player["alive"]:
        return False, "أنت خارج اللعبة."

    target = get_player(
        code,
        target_id
    )

    if not target:
        return False, "اللاعب غير موجود."

    if not target["alive"]:
        return False, "هذا اللاعب خرج من اللعبة."

    db_execute(
        """
        UPDATE players
        SET vote = ?
        WHERE id = ?
        """,
        (
            target_id,
            player["id"]
        )
    )

    alive = get_alive_players(code)

    all_voted = all(
        p["vote"]
        for p in alive
    )

    if all_voted:

        await resolve_votes(code)

    else:

        await manager.broadcast(
            code,
            {
                "type": "vote_update"
            }
        )

    return True, "تم تسجيل التصويت."


async def resolve_votes(code):

    players = get_alive_players(code)

    counts = {}

    for player in players:

        vote = player["vote"]

        if vote:

            counts[vote] = (
                counts.get(vote, 0) +
                1
            )

    if not counts:

        db_execute(
            """
            UPDATE rooms
            SET phase = 'night'
            WHERE code = ?
            """,
            (code,)
        )

        return

    highest = max(
        counts.values()
    )

    winners = [
        player_id
        for player_id, count
        in counts.items()
        if count == highest
    ]

    if len(winners) > 1:

        await add_system_message(
            code,
            "⚖️ حصل تعادل، ولم يتم إخراج أي لاعب."
        )

    else:

        eliminated = get_player(
            code,
            winners[0]
        )

        if eliminated:

            db_execute(
                """
                UPDATE players
                SET alive = 0
                WHERE id = ?
                """,
                (
                    eliminated["id"],
                )
            )

            await add_system_message(
                code,
                f"🗳️ تم إخراج "
                f"{eliminated['name']} "
                "بالتصويت."
            )

    db_execute(
        """
        UPDATE players
        SET vote = NULL
        WHERE room_code = ?
        """,
        (code,)
    )

    result = check_winner(code)

    if result:

        await finish_game(
            code,
            result
        )

        return

    db_execute(
        """
        UPDATE rooms
        SET phase = 'night'
        WHERE code = ?
        """,
        (code,)
    )

    await manager.broadcast(
        code,
        {
            "type": "phase_changed"
        }
    )


# ============================================================
# CHAT
# ============================================================

async def add_chat_message(
    code,
    user_id,
    text
):

    player = get_player(
        code,
        user_id
    )

    if not player:
        return False, "أنت لست داخل الغرفة."

    text = clean_text(text)

    if not text:
        return False, "الرسالة فارغة."

    if len(text) > 500:
        return False, "الرسالة طويلة جداً."

    if not player["alive"]:
        return False, "اللاعب الميت لا يستطيع الكتابة في الشات العام."

    db_execute(
        """
        INSERT INTO messages
        (
            room_code,
            user_id,
            name,
            text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            code,
            str(user_id),
            player["name"],
            text,
            now()
        )
    )

    await manager.broadcast(
        code,
        {
            "type": "chat",
            "user_id": str(user_id),
            "name": player["name"],
            "text": text
        }
    )

    return True, "تم الإرسال."


async def add_system_message(
    code,
    text
):

    db_execute(
        """
        INSERT INTO messages
        (
            room_code,
            user_id,
            name,
            text,
            created_at
        )
        VALUES (?, ?, 'النظام', ?, ?)
        """,
        (
            code,
            text,
            now()
        )
    )

    await manager.broadcast(
        code,
        {
            "type": "system",
            "text": text
        }
    )


def get_messages(code):

    messages = db_execute(
        """
        SELECT
            user_id,
            name,
            text,
            created_at
        FROM messages
        WHERE room_code = ?
        ORDER BY id DESC
        LIMIT 100
        """,
        (code,),
        fetch=True
    )

    messages = list(
        reversed(messages)
    )

    return [
        {
            "user_id":
                str(m["user_id"]),

            "name":
                m["name"],

            "text":
                m["text"],

            "created_at":
                m["created_at"]
        }
        for m in messages
    ]


# ============================================================
# PUBLIC GAME STATE
# ============================================================

def game_state(
    code,
    user_id
):

    room = get_room(code)

    if not room:
        return None

    player = get_player(
        code,
        user_id
    )

    players = get_players(code)

    public_players = []

    for p in players:

        item = {

            "id":
                str(p["user_id"]),

            "name":
                p["name"],

            "alive":
                bool(p["alive"]),

            "is_ai":
                bool(p["is_ai"])

        }

        if room["phase"] == "finished":

            item["role"] = p["role"]

        elif player and str(
            p["user_id"]
        ) == str(user_id):

            item["role"] = p["role"]

        public_players.append(item)

    targets = []

    if player and player["alive"]:

        phase = room["phase"]

        if phase == "night":

            role = player["role"]

            for target in players:

                if not target["alive"]:
                    continue

                if target["id"] == player["id"]:
                    continue

                if role == "mafia":

                    if target["role"] != "mafia":

                        targets.append({
                            "id":
                                str(target["user_id"]),
                            "name":
                                target["name"]
                        })

                elif role in (
                    "detective",
                    "doctor"
                ):

                    targets.append({
                        "id":
                            str(target["user_id"]),
                        "name":
                            target["name"]
                    })

        elif phase == "vote":

            for target in players:

                if not target["alive"]:
                    continue

                if target["id"] == player["id"]:
                    continue

                targets.append({
                    "id":
                        str(target["user_id"]),
                    "name":
                        target["name"]
                })

    private_message = ""

    if player:

        private_message = (
            player["private_message"]
            or ""
        )

    winner = None

    if room["status"] == "finished":

        winner = check_winner_finished(
            code
        )

    return {

        "code":
            room["code"],

        "name":
            room["name"],

        "host_id":
            str(room["host_id"]),

        "max_players":
            room["max_players"],

        "phase":
            room["phase"],

        "phase_name":
            phase_name(room["phase"]),

        "status":
            room["status"],

        "players":
            public_players,

        "targets":
            targets,

        "my_role":
            player["role"] if player else None,

        "my_alive":
            bool(player["alive"])
            if player else False,

        "private_message":
            private_message,

        "messages":
            get_messages(code),

        "winner":
            winner

    }


def check_winner_finished(code):

    players = get_players(code)

    mafia = [
        p
        for p in players
        if p["role"] == "mafia"
    ]

    alive_mafia = [
        p
        for p in mafia
        if p["alive"]
    ]

    alive_others = [
        p
        for p in players
        if p["alive"]
        and p["role"] != "mafia"
    ]

    if alive_mafia and (
        len(alive_mafia)
        >= len(alive_others)
    ):

        return "mafia"

    if not alive_mafia:

        return "citizens"

    return None


# ============================================================
# AI TURN
# ============================================================

async def process_ai(
    code
):

    room = get_room(code)

    if not room:
        return

    if room["phase"] == "night":

        ai_night_actions(code)

        await asyncio.sleep(0.5)

        players = get_alive_players(code)

        required = []

        for p in players:

            if p["role"] in (
                "mafia",
                "detective",
                "doctor"
            ):

                required.append(p)

        if all(
            p["night_action"]
            for p in required
        ):

            await resolve_night(code)

    elif room["phase"] == "vote":

        players = get_alive_players(code)

        for player in players:

            if not player["is_ai"]:
                continue

            targets = [
                p
                for p in players
                if p["id"] != player["id"]
            ]

            if not targets:
                continue

            target = random.choice(
                targets
            )

            db_execute(
                """
                UPDATE players
                SET vote = ?
                WHERE id = ?
                """,
                (
                    target["user_id"],
                    player["id"]
                )
            )

        players = get_alive_players(code)

        if all(
            p["vote"]
            for p in players
        ):

            await resolve_votes(code)


# ============================================================
# API ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():

    return HTMLResponse(
        GAME_HTML
    )


@app.get("/health")
async def health():

    return {
        "ok": True,
        "service": "mafia-game",
        "database": DATABASE_FILE,
        "telegram_configured":
            bool(BOT_TOKEN),
        "webapp_url":
            WEBAPP_URL
    }


@app.get("/api/status")
async def status():

    rooms = public_rooms()

    return {
        "ok": True,
        "rooms": len(rooms),
        "telegram_configured":
            bool(BOT_TOKEN)
    }


@app.get("/api/rooms")
async def rooms():

    return {
        "ok": True,
        "rooms":
            public_rooms()
    }


@app.post("/api/rooms/create")
async def api_create_room(
    data: dict
):

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    player_name = clean_name(
        data.get(
            "player_name",
            "لاعب"
        )
    )

    room_name = clean_name(
        data.get(
            "room_name",
            "غرفة مافيا"
        )
    )

    max_players = int(
        data.get(
            "max_players",
            8
        )
    )

    if not user_id:

        return JSONResponse(
            {
                "ok": False,
                "error":
                    "معرف اللاعب مفقود."
            },
            status_code=400
        )

    code = await create_room(
        user_id,
        player_name,
        room_name,
        max_players
    )

    return {
        "ok": True,
        "code": code
    }


@app.post("/api/rooms/join")
async def api_join_room(
    data: dict
):

    code = str(
        data.get(
            "code",
            ""
        )
    ).upper().strip()

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    player_name = clean_name(
        data.get(
            "player_name",
            "لاعب"
        )
    )

    success, message = await join_room(
        code,
        user_id,
        player_name
    )

    if not success:

        return JSONResponse(
            {
                "ok": False,
                "error": message
            },
            status_code=400
        )

    return {
        "ok": True,
        "message": message
    }


@app.post("/api/rooms/leave")
async def api_leave_room(
    data: dict
):

    code = str(
        data.get(
            "code",
            ""
        )
    ).upper().strip()

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    success, message = await leave_room(
        code,
        user_id
    )

    return {
        "ok": success,
        "message": message
    }


@app.post("/api/rooms/start")
async def api_start_room(
    data: dict
):

    code = str(
        data.get(
            "code",
            ""
        )
    ).upper().strip()

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    success, message = await start_online_game(
        code,
        user_id
    )

    if not success:

        return JSONResponse(
            {
                "ok": False,
                "error": message
            },
            status_code=400
        )

    return {
        "ok": True,
        "message": message
    }


@app.get("/api/room/{code}")
async def api_room(
    code: str,
    user_id: str = ""
):

    code = code.upper().strip()

    state = game_state(
        code,
        user_id
    )

    if not state:

        return JSONResponse(
            {
                "ok": False,
                "error":
                    "الغرفة غير موجودة."
            },
            status_code=404
        )

    return {
        "ok": True,
        "game": state
    }


@app.post("/api/chat")
async def api_chat(
    data: dict
):

    code = str(
        data.get(
            "code",
            ""
        )
    ).upper().strip()

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    text = clean_text(
        data.get(
            "text",
            ""
        )
    )

    success, message = await add_chat_message(
        code,
        user_id,
        text
    )

    if not success:

        return JSONResponse(
            {
                "ok": False,
                "error": message
            },
            status_code=400
        )

    return {
        "ok": True
    }


@app.post("/api/action")
async def api_action(
    data: dict
):

    code = str(
        data.get(
            "code",
            ""
        )
    ).upper().strip()

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    action = str(
        data.get(
            "action",
            ""
        )
    )

    target_id = str(
        data.get(
            "target_id",
            ""
        )
    )

    room = get_room(code)

    if not room:

        return JSONResponse(
            {
                "ok": False,
                "error":
                    "الغرفة غير موجودة."
            },
            status_code=404
        )

    if action == "vote":

        success, message = await submit_vote(
            code,
            user_id,
            target_id
        )

        if not success:

            return JSONResponse(
                {
                    "ok": False,
                    "error": message
                },
                status_code=400
            )

    else:

        player = get_player(
            code,
            user_id
        )

        if not player:

            return JSONResponse(
                {
                    "ok": False,
                    "error":
                        "أنت لست داخل اللعبة."
                },
                status_code=400
            )

        if not player["alive"]:

            return JSONResponse(
                {
                    "ok": False,
                    "error":
                        "أنت خارج اللعبة."
                },
                status_code=400
            )

        target = get_player(
            code,
            target_id
        )

        if not target:

            return JSONResponse(
                {
                    "ok": False,
                    "error":
                        "الهدف غير موجود."
                },
                status_code=400
            )

        if room["phase"] != "night":

            return JSONResponse(
                {
                    "ok": False,
                    "error":
                        "ليس وقت تنفيذ هذا الدور."
                },
                status_code=400
            )

        if action == "kill":

            if player["role"] != "mafia":

                return JSONResponse(
                    {
                        "ok": False,
                        "error":
                            "هذا الإجراء للمافيا فقط."
                    },
                    status_code=400
                )

            if target["role"] == "mafia":

                return JSONResponse(
                    {
                        "ok": False,
                        "error":
                            "لا يمكنك استهداف مافيا."
                    },
                    status_code=400
                )

        elif action == "check":

            if player["role"] != "detective":

                return JSONResponse(
                    {
                        "ok": False,
                        "error":
                            "هذا الإجراء للمحقق فقط."
                    },
                    status_code=400
                )

            if target["role"] == "mafia":

                private = (
                    f"🔎 نتيجة التحقيق: "
                    f"{target['name']} هو مافيا 🔴"
                )

            else:

                private = (
                    f"🔎 نتيجة التحقيق: "
                    f"{target['name']} ليس مافيا 🟢"
                )

            db_execute(
                """
                UPDATE players
                SET private_message = ?
                WHERE id = ?
                """,
                (
                    private,
                    player["id"]
                )
            )

        elif action == "heal":

            if player["role"] != "doctor":

                return JSONResponse(
                    {
                        "ok": False,
                        "error":
                            "هذا الإجراء للطبيب فقط."
                    },
                    status_code=400
                )

        else:

            return JSONResponse(
                {
                    "ok": False,
                    "error":
                        "إجراء غير معروف."
                },
                status_code=400
            )

        db_execute(
            """
            UPDATE players
            SET night_action = ?
            WHERE id = ?
            """,
            (
                target_id,
                player["id"]
            )
        )

        alive = get_alive_players(code)

        required = [
            p
            for p in alive
            if p["role"] in (
                "mafia",
                "detective",
                "doctor"
            )
        ]

        # AI takes its actions
        await process_ai(code)

        alive = get_alive_players(code)

        required = [
            p
            for p in alive
            if p["role"] in (
                "mafia",
                "detective",
                "doctor"
            )
        ]

        if required and all(
            p["night_action"]
            for p in required
        ):

            await resolve_night(code)

    return {
        "ok": True
    }


# ============================================================
# SINGLE PLAYER
# ============================================================

@app.post("/api/single/create")
async def api_single_create(
    data: dict
):

    user_id = str(
        data.get(
            "user_id",
            ""
        )
    ).strip()

    player_name = clean_name(
        data.get(
            "player_name",
            "لاعب"
        )
    )

    count = int(
        data.get(
            "count",
            8
        )
    )

    if not user_id:

        return JSONResponse(
            {
                "ok": False,
                "error":
                    "معرف اللاعب مفقود."
            },
            status_code=400
        )

    code = await create_single_player_game(
        user_id,
        player_name,
        count
    )

    return {
        "ok": True,
        "code": code
    }


# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws/{code}/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    code: str,
    user_id: str
):

    code = code.upper().strip()

    room = get_room(code)

    if not room:

        await websocket.close(
            code=4004
        )

        return

    player = get_player(
        code,
        user_id
    )

    if not player:

        await websocket.close(
            code=4003
        )

        return

    await manager.connect(
        code,
        websocket
    )

    try:

        await websocket.send_json(
            {
                "type":
                    "state",
                "game":
                    game_state(
                        code,
                        user_id
                    )
            }
        )

        while True:

            data = await websocket.receive_json()

            action = data.get(
                "action"
            )

            if action == "chat":

                text = clean_text(
                    data.get(
                        "text",
                        ""
                    )
                )

                await add_chat_message(
                    code,
                    user_id,
                    text
                )

            elif action == "refresh":

                await websocket.send_json(
                    {
                        "type":
                            "state",
                        "game":
                            game_state(
                                code,
                                user_id
                            )
                    }
                )

    except WebSocketDisconnect:

        manager.disconnect(
            code,
            websocket
        )

    except Exception as exc:

        logger.error(
            "WebSocket error: %s",
            exc
        )

        manager.disconnect(
            code,
            websocket
        )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request
):

    try:

        update = await request.json()

    except Exception:

        return {
            "ok": False
        }

    try:

        if "message" in update:

            message = update[
                "message"
            ]

            chat = message.get(
                "chat",
                {}
            )

            chat_id = chat.get(
                "id"
            )

            text = message.get(
                "text",
                ""
            )

            if text == "/start":

                await send_message(
                    chat_id,
                    "🎭 أهلاً بك في لعبة المافيا!\n\n"
                    "ادخل إلى اللعبة وأنشئ غرفة أو "
                    "انضم إلى اللاعبين الموجودين.",
                    telegram_keyboard()
                )

            elif text == "/help":

                await send_message(
                    chat_id,
                    "🎭 لعبة المافيا\n\n"
                    "🌐 لعب أونلاين\n"
                    "🤖 لعب فردي ضد الذكاء الاصطناعي\n"
                    "💬 شات مباشر\n"
                    "🌙 أدوار ليلية\n"
                    "☀️ نقاش وتصويت نهاري",
                    telegram_keyboard()
                )

        if "callback_query" in update:

            callback = update[
                "callback_query"
            ]

            callback_id = callback.get(
                "id"
            )

            if callback_id:

                await answer_callback(
                    callback_id
                )

            if callback.get(
                "data"
            ) == "help":

                chat = callback.get(
                    "message",
                    {}
                ).get(
                    "chat",
                    {}
                )

                await send_message(
                    chat.get("id"),
                    "🎭 طريقة اللعب:\n\n"
                    "اجمع اللاعبين داخل غرفة، "
                    "ثم تبدأ أدوار المافيا والمحقق والطبيب "
                    "والمواطنين.\n\n"
                    "في النهار يتناقش اللاعبون ويصوتون، "
                    "وفي الليل تتحرك الأدوار الخاصة.",
                    telegram_keyboard()
                )

    except Exception as exc:

        logger.exception(
            "Webhook error: %s",
            exc
        )

    return {
        "ok": True
    }


# ============================================================
# TELEGRAM TEST
# ============================================================

@app.get("/telegram-test")
async def telegram_test():

    result = await telegram_call(
        "getMe"
    )

    return {
        "ok":
            bool(
                result.get("ok")
            ),
        "telegram":
            result
    }


@app.get("/setup-webhook")
async def setup_webhook():

    result = await telegram_call(
        "setWebhook",
        {
            "url":
                WEBHOOK_URL,

            "drop_pending_updates":
                True
        }
    )

    return {
        "ok":
            bool(
                result.get("ok")
            ),

        "message":
            "تمت تهيئة Webhook بنجاح."
            if result.get("ok")
            else
            "فشل إعداد Webhook.",

        "webhook_url":
            WEBHOOK_URL,

        "telegram_result":
            result
    }


@app.get("/webhook-info")
async def webhook_info():

    return await telegram_call(
        "getWebhookInfo"
    )


# ============================================================
# HTML
# ============================================================

GAME_HTML = r"""
<!DOCTYPE html>

<html lang="ar" dir="rtl">

<head>

<meta charset="UTF-8">

<meta
name="viewport"
content="width=device-width,
maximum-scale=1,
user-scalable=no"
>

<title>لعبة المافيا</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

* {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at top,
            #202536,
            #080a10 65%
        );
    color: #fff;
    font-family:
        Arial,
        Tahoma,
        sans-serif;
}

button,
input,
select {
    font: inherit;
}

button {
    border: 0;
}

.app {
    max-width: 560px;
    margin: auto;
    padding: 16px;
    min-height: 100vh;
}

.header {
    text-align: center;
    padding: 20px 0;
}

.logo {
    font-size: 55px;
}

.title {
    font-size: 30px;
    font-weight: 900;
}

.sub {
    color: #858ca0;
    margin-top: 6px;
}

.card {
    background: rgba(
        18,
        22,
        34,
        .95
    );

    border: 1px solid #292f40;

    border-radius: 20px;

    padding: 16px;

    margin-bottom: 14px;
}

.section {
    font-size: 19px;
    font-weight: 900;
    margin-bottom: 13px;
}

.btn {
    width: 100%;
    padding: 14px;
    border-radius: 14px;
    color: white;
    background: #292f40;
    font-weight: 900;
    margin-top: 9px;
}

.primary {
    background: #8e273b;
}

.green {
    background: #17694c;
}

.blue {
    background: #245d91;
}

.gold {
    background: #80601e;
}

.red {
    background: #741e2c;
}

input,
select {
    width: 100%;
    padding: 14px;
    border-radius: 13px;
    border: 1px solid #30384c;
    background: #090c13;
    color: white;
    outline: none;
    margin-bottom: 9px;
}

.hidden {
    display: none !important;
}

.rooms {
    display: flex;
    flex-direction: column;
    gap: 9px;
}

.room {
    background: #090c13;
    border: 1px solid #282f42;
    border-radius: 15px;
    padding: 13px;
}

.room-top {
    display: flex;
    justify-content: space-between;
    gap: 10px;
}

.room-name {
    font-weight: 900;
}

.room-count {
    color: #8e96aa;
    font-size: 13px;
}

.chat {
    height: 360px;
    overflow-y: auto;
    background: #080b12;
    border-radius: 15px;
    padding: 10px;
}

.msg {
    margin-bottom: 9px;
    background: #151a27;
    border-radius: 12px;
    padding: 9px;
}

.msg-name {
    font-size: 12px;
    color: #d8ad43;
    font-weight: 900;
}

.msg-text {
    margin-top: 3px;
    line-height: 1.5;
}

.system {
    text-align: center;
    color: #aab1c1;
}

.chat-input {
    display: flex;
    gap: 7px;
    margin-top: 8px;
}

.chat-input input {
    margin: 0;
}

.chat-input button {
    width: 80px;
    border-radius: 12px;
    background: #8e273b;
    color: white;
    font-weight: 900;
}

.code {
    text-align: center;
    font-size: 36px;
    color: #e5b848;
    letter-spacing: 7px;
    font-weight: 900;
    margin: 12px 0;
}

.phase {
    text-align: center;
    font-size: 24px;
    font-weight: 900;
}

.role {
    text-align: center;
    margin: 14px 0;
    background: #090c13;
    border-radius: 15px;
    padding: 15px;
}

.role-icon {
    font-size: 40px;
}

.role-name {
    font-size: 22px;
    font-weight: 900;
}

.players {
    display: flex;
    flex-direction: column;
    gap: 7px;
}

.player {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 11px;
    background: #090c13;
    border-radius: 12px;
}

.dead {
    opacity: .4;
}

.targets {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.target {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #090c13;
    padding: 10px;
    border-radius: 12px;
}

.target button {
    width: auto;
    margin: 0;
    padding: 9px 13px;
}

.notice {
    text-align: center;
    color: #8e96aa;
    line-height: 1.7;
}

.private {
    background: #392d12;
    border: 1px solid #71591e;
    padding: 11px;
    border-radius: 12px;
    margin-top: 9px;
    line-height: 1.6;
}

</style>

</head>

<body>

<div class="app">

<div class="header">

<div class="logo">
🎭
</div>

<div class="title">
لعبة المافيا
</div>

<div class="sub">
Mafia Online
</div>

</div>


<!-- HOME -->

<div id="home">

<div class="card">

<div class="section">
🌐 الرومات العامة
</div>

<div
id="rooms"
class="rooms"
>

<div class="notice">
جاري تحميل الرومات...
</div>

</div>

<button
class="btn blue"
onclick="loadRooms()"
>
🔄 تحديث الرومات
</button>

</div>


<div class="card">

<div class="section">
➕ إنشاء غرفة
</div>

<input
id="roomName"
placeholder="اسم الغرفة"
maxlength="30"
>

<select id="maxPlayers">

<option value="3">
3 لاعبين
</option>

<option value="4">
4 لاعبين
</option>

<option value="5">
5 لاعبين
</option>

<option value="6">
6 لاعبين
</option>

<option value="8" selected>
8 لاعبين
</option>

<option value="10">
10 لاعبين
</option>

<option value="12">
12 لاعب
</option>

<option value="15">
15 لاعب
</option>

<option value="20">
20 لاعب
</option>

</select>

<button
class="btn primary"
onclick="createRoom()"
>
🎭 إنشاء غرفة
</button>

</div>


<div class="card">

<div class="section">
🤖 لعب فردي
</div>

<div class="notice">
العب ضد لاعبين يتحكم بهم الذكاء الاصطناعي.
</div>

<select id="singleCount">

<option value="5">
5 لاعبين
</option>

<option value="8" selected>
8 لاعبين
</option>

<option value="10">
10 لاعبين
</option>

<option value="12">
12 لاعب
</option>

</select>

<button
class="btn gold"
onclick="createSingle()"
>
🤖 ابدأ اللعب الفردي
</button>

</div>

</div>


<!-- ROOM -->

<div
id="lobby"
class="hidden"
>

<div class="card">

<div class="section">
🎭 الغرفة
</div>

<div
id="lobbyName"
class="notice"
></div>

<div
id="roomCode"
class="code"
>
-----
</div>

<div
id="lobbyCount"
class="notice"
>
</div>

</div>


<div class="card">

<div class="section">
👥 اللاعبين
</div>

<div
id="lobbyPlayers"
class="players"
></div>

</div>


<div class="card">

<button
id="startBtn"
class="btn primary"
onclick="startGame()"
>
🚀 بدء اللعبة
</button>

<button
class="btn red"
onclick="leaveRoom()"
>
🚪 مغادرة
</button>

</div>

</div>


<!-- GAME -->

<div
id="game"
class="hidden"
>

<div class="card">

<div
id="phase"
class="phase"
>
🌙 الليل
</div>

<div
id="role"
class="role"
></div>

<div
id="private"
class="private hidden"
></div>

</div>


<div class="card">

<div class="section">
💬 الشات
</div>

<div
id="chat"
class="chat"
></div>

<div class="chat-input">

<input
id="chatInput"
placeholder="اكتب رسالتك..."
maxlength="500"
onkeydown="chatKey(event)"
>

<button
onclick="sendChat()"
>
إرسال
</button>

</div>

</div>


<div class="card">

<div class="section">
🎯 الإجراءات
</div>

<div
id="targets"
class="targets"
></div>

</div>


<div class="card">

<div class="section">
👥 اللاعبون
</div>

<div
id="gamePlayers"
class="players"
></div>

</div>


<button
class="btn red"
onclick="leaveRoom()"
>
🚪 الخروج
</button>

</div>

</div>


<script>

const tg =
window.Telegram &&
window.Telegram.WebApp
?
window.Telegram.WebApp
:
null;

if (tg) {
    tg.ready();
    tg.expand();
}


let userId = "";
let playerName = "";

let currentCode = "";
let currentGame = null;

let socket = null;


function initUser() {

    if (
        tg &&
        tg.initDataUnsafe &&
        tg.initDataUnsafe.user
    ) {

        const user =
            tg.initDataUnsafe.user;

        userId =
            String(user.id);

        playerName =
            user.first_name ||
            user.username ||
            "لاعب";

    } else {

        let saved =
            localStorage.getItem(
                "mafia_user_id"
            );

        if (!saved) {

            saved =
                "demo_" +
                Math.random()
                    .toString(36)
                    .substring(2, 10);

            localStorage.setItem(
                "mafia_user_id",
                saved
            );
        }

        userId = saved;

        playerName =
            localStorage.getItem(
                "mafia_player_name"
            ) ||
            "لاعب تجريبي";
    }
}


initUser();


function show(id) {

    [
        "home",
        "lobby",
        "game"
    ].forEach(
        section => {

            document
                .getElementById(section)
                .classList.add(
                    "hidden"
                );
        }
    );

    document
        .getElementById(id)
        .classList.remove(
            "hidden"
        );
}


function notify(text) {

    if (
        tg &&
        tg.showAlert
    ) {

        tg.showAlert(text);

    } else {

        alert(text);
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
                        "application/json"
                },
                ...options
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


async function loadRooms() {

    try {

        const data =
            await api(
                "/api/rooms"
            );

        renderRooms(
            data.rooms
        );

    } catch (error) {

        notify(
            error.message
        );
    }
}


function renderRooms(rooms) {

    const box =
        document.getElementById(
            "rooms"
        );

    box.innerHTML = "";

    if (!rooms.length) {

        box.innerHTML = `
            <div class="notice">
                لا توجد رومات حالياً.
                <br>
                كن أول شخص ينشئ غرفة 🔥
            </div>
        `;

        return;
    }


    rooms.forEach(
        room => {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "room";

            div.innerHTML = `

                <div class="room-top">

                    <div>

                        <div class="room-name">
                            🎭 ${escapeHtml(
                                room.name
                            )}
                        </div>

                        <div class="room-count">
                            👥 ${
                                room.players
                            } / ${
                                room.max_players
                            }
                        </div>

                    </div>

                    <button
                        class="btn green"
                        style="
                            width:auto;
                            margin:0;
                        "
                    >
                        دخول
                    </button>

                </div>

            `;

            div
                .querySelector("button")
                .onclick =
                () =>
                    joinRoom(
                        room.code
                    );

            box.appendChild(
                div
            );
        }
    );
}


async function createRoom() {

    const name =
        document
            .getElementById(
                "roomName"
            )
            .value
            .trim()
            ||
            "غرفة مافيا";

    const maxPlayers =
        Number(
            document
                .getElementById(
                    "maxPlayers"
                )
                .value
        );

    localStorage.setItem(
        "mafia_player_name",
        playerName
    );

    try {

        const data =
            await api(
                "/api/rooms/create",
                {
                    method:
                        "POST",

                    body:
                        JSON.stringify({

                            user_id:
                                userId,

                            player_name:
                                playerName,

                            room_name:
                                name,

                            max_players:
                                maxPlayers

                        })
                }
            );

        openRoom(
            data.code
        );

    } catch (error) {

        notify(
            error.message
        );
    }
}


async function joinRoom(code) {

    try {

        await api(
            "/api/rooms/join",
            {
                method:
                    "POST",

                body:
                    JSON.stringify({

                        code:
                            code,

                        user_id:
                            userId,

                        player_name:
                            playerName

                    })
            }
        );

        openRoom(
            code
        );

    } catch (error) {

        notify(
            error.message
        );
    }
}


async function openRoom(code) {

    currentCode =
        code;

    connectSocket();

    await refresh();

    show(
        currentGame &&
        currentGame.status ===
            "waiting"
        ?
        "lobby"
        :
        "game"
    );
}


function connectSocket() {

    if (socket) {

        try {
            socket.close();
        } catch {}
    }

    const protocol =
        location.protocol ===
        "https:"
        ?
        "wss"
        :
        "ws";

    socket =
        new WebSocket(
            protocol +
            "://" +
            location.host +
            "/ws/" +
            encodeURIComponent(
                currentCode
            ) +
            "/" +
            encodeURIComponent(
                userId
            )
        );


    socket.onmessage =
        event => {

            const data =
                JSON.parse(
                    event.data
                );

            if (
                data.type ===
                "state"
            ) {

                currentGame =
                    data.game;

                renderCurrent();
            }

            else {

                refresh();
            }
        };


    socket.onclose =
        () => {

            setTimeout(
                () => {

                    if (
                        currentCode
                    ) {

                        connectSocket();
                    }

                },
                2500
            );
        };
}


async function refresh() {

    if (!currentCode) {
        return;
    }

    try {

        const data =
            await api(
                "/api/room/" +
                encodeURIComponent(
                    currentCode
                ) +
                "?user_id=" +
                encodeURIComponent(
                    userId
                )
            );

        currentGame =
            data.game;

        renderCurrent();

    } catch (error) {

        console.log(
            error.message
        );
    }
}


function renderCurrent() {

    if (!currentGame) {
        return;
    }

    if (
        currentGame.status ===
        "waiting"
    ) {

        renderLobby();

        show("lobby");

    } else {

        renderGame();

        show("game");
    }
}


function renderLobby() {

    document
        .getElementById(
            "lobbyName"
        )
        .textContent =
        currentGame.name;

    document
        .getElementById(
            "roomCode"
        )
        .textContent =
        currentGame.code;

    document
        .getElementById(
            "lobbyCount"
        )
        .textContent =
        "👥 " +
        currentGame.players.length +
        " / " +
        currentGame.max_players;


    const box =
        document.getElementById(
            "lobbyPlayers"
        );

    box.innerHTML = "";


    currentGame.players.forEach(
        player => {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "player";

            div.innerHTML = `

                <div>
                    ${escapeHtml(
                        player.name
                    )}
                </div>

                <div>
                    ${
                        String(
                            player.id
                        ) ===
                        String(
                            currentGame.host_id
                        )
                        ?
                        "👑"
                        :
                        "👤"
                    }
                </div>

            `;

            box.appendChild(
                div
            );
        }
    );


    const start =
        document.getElementById(
            "startBtn"
        );

    if (
        String(
            currentGame.host_id
        ) ===
        String(userId)
    ) {

        start.classList.remove(
            "hidden"
        );

        start.disabled =
            currentGame.players.length < 3;

    } else {

        start.classList.add(
            "hidden"
        );
    }
}


async function startGame() {

    try {

        await api(
            "/api/rooms/start",
            {
                method:
                    "POST",

                body:
                    JSON.stringify({

                        code:
                            currentCode,

                        user_id:
                            userId

                    })
            }
        );

        await refresh();

    } catch (error) {

        notify(
            error.message
        );
    }
}


function renderGame() {

    document
        .getElementById(
            "phase"
        )
        .textContent =
        currentGame.phase_name;


    const roles = {

        mafia: [
            "🔴",
            "المافيا"
        ],

        detective: [
            "🔎",
            "المحقق"
        ],

        doctor: [
            "💉",
            "الطبيب"
        ],

        citizen: [
            "👤",
            "المواطن"
        ]

    };


    const role =
        roles[
            currentGame.my_role
        ];


    document
        .getElementById(
            "role"
        )
        .innerHTML = role

        ? `

            <div class="role-icon">
                ${role[0]}
            </div>

            <div class="role-name">
                ${role[1]}
            </div>

        `

        : `

            <div class="role-name">
                جاري التحميل...
            </div>

        `;


    const privateBox =
        document.getElementById(
            "private"
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


    renderChat();
    renderTargets();
    renderGamePlayers();
}


function renderChat() {

    const box =
        document.getElementById(
            "chat"
        );

    const oldScroll =
        box.scrollTop;

    const atBottom =
        box.scrollHeight -
        box.clientHeight -
        oldScroll <
        80;


    box.innerHTML = "";


    (
        currentGame.messages ||
        []
    ).forEach(
        message => {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "msg";


            if (
                message.name ===
                "النظام"
            ) {

                div.classList.add(
                    "system"
                );

                div.innerHTML =
                    escapeHtml(
                        message.text
                    );

            } else {

                div.innerHTML = `

                    <div class="msg-name">
                        ${escapeHtml(
                            message.name
                        )}
                    </div>

                    <div class="msg-text">
                        ${escapeHtml(
                            message.text
                        )}
                    </div>

                `;
            }

            box.appendChild(
                div
            );
        }
    );


    if (atBottom) {

        box.scrollTop =
            box.scrollHeight;
    }
}


function renderTargets() {

    const box =
        document.getElementById(
            "targets"
        );

    box.innerHTML = "";


    if (
        !currentGame.my_alive
    ) {

        box.innerHTML = `
            <div class="notice">
                💀 أنت خارج اللعبة.
            </div>
        `;

        return;
    }


    const targets =
        currentGame.targets ||
        [];


    if (!targets.length) {

        if (
            currentGame.phase ===
            "night"
        ) {

            box.innerHTML = `
                <div class="notice">
                    🌙 انتظر دورك.
                </div>
            `;

        } else {

            box.innerHTML = `
                <div class="notice">
                    لا يوجد إجراء متاح الآن.
                </div>
            `;
        }

        return;
    }


    targets.forEach(
        target => {

            const row =
                document.createElement(
                    "div"
                );

            row.className =
                "target";


            const button =
                document.createElement(
                    "button"
                );

            button.className =
                "btn primary";

            button.textContent =
                currentGame.phase ===
                "vote"
                ?
                "🗳️ تصويت"
                :
                "اختيار";


            button.onclick =
                () => {

                    const action =
                        currentGame.phase ===
                        "vote"
                        ?
                        "vote"
                        :
                        getNightAction();

                    sendAction(
                        action,
                        target.id
                    );
                };


            row.innerHTML = `
                <div>
                    ${escapeHtml(
                        target.name
                    )}
                </div>
            `;

            row.appendChild(
                button
            );

            box.appendChild(
                row
            );
        }
    );
}


function getNightAction() {

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

        await api(
            "/api/action",
            {
                method:
                    "POST",

                body:
                    JSON.stringify({

                        code:
                            currentCode,

                        user_id:
                            userId,

                        action:
                            action,

                        target_id:
                            targetId

                    })
            }
        );

        await refresh();

    } catch (error) {

        notify(
            error.message
        );
    }
}


function renderGamePlayers() {

    const box =
        document.getElementById(
            "gamePlayers"
        );

    box.innerHTML = "";


    currentGame.players.forEach(
        player => {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "player";

            if (
                !player.alive
            ) {

                div.classList.add(
                    "dead"
                );
            }


            let role = "";

            if (
                player.role
            ) {

                const roles = {

                    mafia:
                        "🔴 مافيا",

                    detective:
                        "🔎 محقق",

                    doctor:
                        "💉 طبيب",

                    citizen:
                        "👤 مواطن"

                };

                role =
                    roles[
                        player.role
                    ] || "";
            }


            div.innerHTML = `

                <div>
                    <b>
                        ${escapeHtml(
                            player.name
                        )}
                    </b>

                    <div
                        style="
                            font-size:12px;
                            color:#7f8798;
                        "
                    >
                        ${
                            player.alive
                            ?
                            "على قيد الحياة"
                            :
                            "💀 خارج اللعبة"
                        }
                    </div>

                </div>

                <div>
                    ${
                        player.alive
                        ?
                        "🟢"
                        :
                        "💀"
                    }

                    ${
                        role
                        ?
                        "<br>" + role
                        :
                        ""
                    }

                </div>

            `;

            box.appendChild(
                div
            );
        }
    );
}


function sendChat() {

    const input =
        document.getElementById(
            "chatInput"
        );

    const text =
        input.value.trim();

    if (!text) {
        return;
    }


    if (
        socket &&
        socket.readyState ===
        WebSocket.OPEN
    ) {

        socket.send(
            JSON.stringify({

                action:
                    "chat",

                text:
                    text

            })
        );

        input.value = "";

    } else {

        api(
            "/api/chat",
            {
                method:
                    "POST",

                body:
                    JSON.stringify({

                        code:
                            currentCode,

                        user_id:
                            userId,

                        text:
                            text

                    })
            }
        ).then(
            () => {
                input.value = "";
                refresh();
            }
        );
    }
}


function chatKey(event) {

    if (
        event.key ===
        "Enter"
    ) {

        sendChat();
    }
}


async function createSingle() {

    const count =
        Number(
            document
                .getElementById(
                    "singleCount"
                )
                .value
        );


    try {

        const data =
            await api(
                "/api/single/create",
                {
                    method:
                        "POST",

                    body:
                        JSON.stringify({

                            user_id:
                                userId,

                            player_name:
                                playerName,

                            count:
                                count

                        })
                }
            );


        currentCode =
            data.code;

        connectSocket();

        await refresh();

        show("game");

    } catch (error) {

        notify(
            error.message
        );
    }
}


async function leaveRoom() {

    if (!currentCode) {
        return;
    }


    try {

        await api(
            "/api/rooms/leave",
            {
                method:
                    "POST",

                body:
                    JSON.stringify({

                        code:
                            currentCode,

                        user_id:
                            userId

                    })
            }
        );

    } catch {}


    if (socket) {

        try {
            socket.close();
        } catch {}
    }


    currentCode = "";
    currentGame = null;

    show("home");

    loadRooms();
}


function escapeHtml(value) {

    return String(value)
        .replaceAll(
            "&",
            "&amp;"
        )
        .replaceAll(
            "<",
            "&lt;"
        )
        .replaceAll(
            ">",
            "&gt;"
        )
        .replaceAll(
            '"',
            "&quot;"
        )
        .replaceAll(
            "'",
            "&#039;"
        );
}


show("home");

loadRooms();

setInterval(
    () => {

        if (
            !currentCode &&
            !document
                .getElementById(
                    "home"
                )
                .classList.contains(
                    "hidden"
                )
        ) {

            loadRooms();
        }

    },
    3000
);

</script>

</body>

</html>
"""


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:web",
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080"
            )
        )
    )