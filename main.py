import os
import json
import asyncio
import logging
import random
from dataclasses import dataclass, field, asdict
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

WEBAPP_URL = os.environ.get(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev"
).strip()

DATA_FILE = "mafia_games.json"

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_URL = (
    WEBAPP_URL.rstrip("/")
    + WEBHOOK_PATH
)

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mafia-game")


# =========================================================
# HTTP CLIENT
# =========================================================

http_client: Optional[httpx.AsyncClient] = None


async def get_http_client():
    global http_client

    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=15.0,
                write=15.0,
                pool=10.0,
            )
        )

    return http_client


async def telegram_api(
    method: str,
    payload: Optional[dict] = None,
):
    """
    اتصال مباشر إلى Telegram Bot API.
    لا يستخدم python-telegram-bot.
    """

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    client = await get_http_client()

    url = f"{TELEGRAM_API}/{method}"

    try:
        response = await client.post(
            url,
            json=payload or {},
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            raise RuntimeError(
                data.get(
                    "description",
                    "Telegram API error"
                )
            )

        return data.get("result")

    except Exception as e:
        logger.error(
            "Telegram API error [%s]: %s",
            method,
            e,
        )
        raise


# =========================================================
# ROLES
# =========================================================

MAFIA = "🔪 المافيا"
DETECTIVE = "🕵️ المحقق"
DOCTOR = "👨‍⚕️ الطبيب"
CITIZEN = "👤 مواطن"


# =========================================================
# PLAYER
# =========================================================

@dataclass
class Player:
    user_id: int
    name: str
    role: str = ""
    alive: bool = True
    is_ai: bool = False


# =========================================================
# GAME
# =========================================================

@dataclass
class Game:
    chat_id: int
    players: list[Player] = field(default_factory=list)
    phase: str = "waiting"
    day: int = 0
    night: int = 0
    started: bool = False
    winner: str = ""

    def get_player(
        self,
        user_id: int,
    ) -> Optional[Player]:

        for player in self.players:

            if player.user_id == user_id:
                return player

        return None

    def alive_players(self):

        return [
            p
            for p in self.players
            if p.alive
        ]

    def mafia_players(self):

        return [
            p
            for p in self.players
            if p.role == MAFIA and p.alive
        ]


# =========================================================
# GLOBAL STATE
# =========================================================

games: dict[int, Game] = {}

games_lock = asyncio.Lock()


# =========================================================
# SAVE / LOAD
# =========================================================

def save_games():

    try:

        data = {}

        for chat_id, game in games.items():

            data[str(chat_id)] = {
                "chat_id": game.chat_id,
                "players": [
                    asdict(player)
                    for player in game.players
                ],
                "phase": game.phase,
                "day": game.day,
                "night": game.night,
                "started": game.started,
                "winner": game.winner,
            }

        with open(
            DATA_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception:

        logger.exception(
            "Failed to save games."
        )


def load_games():

    global games

    if not os.path.exists(DATA_FILE):

        logger.info(
            "No saved games file found."
        )

        return

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        games = {}

        for chat_id, game_data in data.items():

            players = [
                Player(**player)
                for player in game_data.get(
                    "players",
                    [],
                )
            ]

            game = Game(
                chat_id=int(
                    game_data.get(
                        "chat_id",
                        chat_id,
                    )
                ),
                players=players,
                phase=game_data.get(
                    "phase",
                    "waiting",
                ),
                day=game_data.get(
                    "day",
                    0,
                ),
                night=game_data.get(
                    "night",
                    0,
                ),
                started=game_data.get(
                    "started",
                    False,
                ),
                winner=game_data.get(
                    "winner",
                    "",
                ),
            )

            games[int(chat_id)] = game

        logger.info(
            "Loaded %s games.",
            len(games),
        )

    except Exception:

        logger.exception(
            "Failed to load games."
        )


# =========================================================
# TELEGRAM HELPERS
# =========================================================

async def send_message(
    chat_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: Optional[str] = None,
):

    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    if parse_mode:
        payload["parse_mode"] = parse_mode

    return await telegram_api(
        "sendMessage",
        payload,
    )


async def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: Optional[str] = None,
):

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    if parse_mode:
        payload["parse_mode"] = parse_mode

    return await telegram_api(
        "editMessageText",
        payload,
    )


async def answer_callback(
    callback_id: str,
    text: Optional[str] = None,
    show_alert: bool = False,
):

    payload = {
        "callback_query_id": callback_id,
        "show_alert": show_alert,
    }

    if text:
        payload["text"] = text

    try:

        return await telegram_api(
            "answerCallbackQuery",
            payload,
        )

    except Exception:

        logger.exception(
            "Failed to answer callback."
        )

        return None


async def send_private_message(
    user_id: int,
    text: str,
):

    try:

        await send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
        )

    except Exception:

        logger.warning(
            "Could not send private message to %s.",
            user_id,
        )


# =========================================================
# KEYBOARDS
# =========================================================

def start_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🎭 فتح لعبة المافيا",
                    "web_app": {
                        "url": WEBAPP_URL
                    },
                }
            ],
            [
                {
                    "text": "🎮 إنشاء لعبة",
                    "callback_data": "create_game",
                }
            ],
        ]
    }


def lobby_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "➕ انضمام",
                    "callback_data": "join",
                },
                {
                    "text": "🚪 خروج",
                    "callback_data": "leave",
                },
            ],
            [
                {
                    "text": "🎮 بدء اللعبة",
                    "callback_data": "start_game",
                }
            ],
            [
                {
                    "text": "🔄 تحديث",
                    "callback_data": "refresh",
                }
            ],
        ]
    }


def game_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🌙 بدء الليل",
                    "callback_data": "night",
                }
            ],
            [
                {
                    "text": "🗳 التصويت",
                    "callback_data": "vote",
                }
            ],
            [
                {
                    "text": "📊 الحالة",
                    "callback_data": "status",
                }
            ],
        ]
    }


# =========================================================
# TEXT
# =========================================================

def lobby_text(
    game: Game,
) -> str:

    if not game.players:

        players_text = (
            "لا يوجد لاعبون حتى الآن."
        )

    else:

        players_text = "\n".join(
            f"{i + 1}. {player.name}"
            for i, player in enumerate(
                game.players
            )
        )

    return (
        "🎭 *لعبة المافيا*\n\n"
        "👥 *اللاعبون:*\n"
        f"{players_text}\n\n"
        f"🔢 العدد: {len(game.players)}\n\n"
        "اضغط «انضمام» للدخول إلى اللعبة."
    )


def game_text(
    game: Game,
) -> str:

    alive = game.alive_players()

    if alive:

        alive_text = "\n".join(
            f"• {p.name}"
            for p in alive
        )

    else:

        alive_text = "لا يوجد لاعبون أحياء."

    phase_names = {
        "waiting": "⏳ انتظار",
        "day": "☀️ النهار",
        "night": "🌙 الليل",
        "finished": "🏁 انتهت",
    }

    phase = phase_names.get(
        game.phase,
        game.phase,
    )

    return (
        "🎭 *لعبة المافيا*\n\n"
        f"📅 اليوم: {game.day}\n"
        f"🌙 الليل: {game.night}\n"
        f"📍 المرحلة: {phase}\n\n"
        "👥 *الأحياء:*\n"
        f"{alive_text}\n"
    )


# =========================================================
# ROLE ASSIGNMENT
# =========================================================

def assign_roles(
    game: Game,
):

    players = game.players.copy()

    random.shuffle(players)

    count = len(players)

    roles = []

    if count >= 3:

        roles.append(MAFIA)

    if count >= 4:

        roles.append(DETECTIVE)

    if count >= 5:

        roles.append(DOCTOR)

    while len(roles) < count:

        roles.append(CITIZEN)

    random.shuffle(roles)

    for player, role in zip(
        players,
        roles,
    ):

        player.role = role


# =========================================================
# SEND ROLES
# =========================================================

async def send_roles(
    game: Game,
):

    for player in game.players:

        if player.is_ai:
            continue

        await send_private_message(
            player.user_id,
            (
                "🎭 *دورك في اللعبة*\n\n"
                f"دورك هو: {player.role}\n\n"
                "لا تخبر اللاعبين الآخرين بدورك."
            ),
        )


# =========================================================
# UPDATE LOBBY / GAME
# =========================================================

async def update_game_message(
    chat_id: int,
    message_id: int,
):

    game = games.get(chat_id)

    if not game:
        return

    if game.started:

        text = game_text(game)

        keyboard = game_keyboard()

    else:

        text = lobby_text(game)

        keyboard = lobby_keyboard()

    try:

        await edit_message(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:

        logger.warning(
            "Could not edit game message: %s",
            e,
        )


# =========================================================
# COMMAND: /START
# =========================================================

async def handle_start(
    message: dict,
):

    chat = message.get("chat")

    if not chat:
        return

    await send_message(
        chat_id=chat["id"],
        text=(
            "🎭 *مرحباً بك في لعبة المافيا!*\n\n"
            "افتح اللعبة من الزر بالأسفل "
            "أو أنشئ لعبة داخل المجموعة."
        ),
        parse_mode="Markdown",
        reply_markup=start_keyboard(),
    )


# =========================================================
# COMMAND: /GAME
# =========================================================

async def handle_game(
    message: dict,
):

    chat = message.get("chat")

    if not chat:
        return

    chat_id = chat["id"]

    async with games_lock:

        game = games.get(chat_id)

        if game is None:

            game = Game(
                chat_id=chat_id,
            )

            games[chat_id] = game

            save_games()

    await send_message(
        chat_id=chat_id,
        text=lobby_text(game),
        parse_mode="Markdown",
        reply_markup=lobby_keyboard(),
    )


# =========================================================
# COMMAND: /RESET
# =========================================================

async def handle_reset(
    message: dict,
):

    chat = message.get("chat")

    if not chat:
        return

    chat_id = chat["id"]

    async with games_lock:

        games.pop(
            chat_id,
            None,
        )

        save_games()

    await send_message(
        chat_id=chat_id,
        text="🗑 تم حذف اللعبة وإعادة ضبط الغرفة.",
    )


# =========================================================
# COMMAND: /STATUS
# =========================================================

async def handle_status(
    message: dict,
):

    chat = message.get("chat")

    if not chat:
        return

    chat_id = chat["id"]

    game = games.get(chat_id)

    if not game:

        await send_message(
            chat_id=chat_id,
            text=(
                "❌ لا توجد لعبة حالياً.\n"
                "استخدم /game لإنشاء واحدة."
            ),
        )

        return

    await send_message(
        chat_id=chat_id,
        text=game_text(game),
        parse_mode="Markdown",
        reply_markup=game_keyboard(),
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def handle_callback(
    callback: dict,
):

    callback_id = callback.get("id")

    data = callback.get(
        "data",
        "",
    )

    from_user = callback.get(
        "from",
        {},
    )

    user_id = from_user.get(
        "id"
    )

    message = callback.get(
        "message"
    )

    if not message:

        await answer_callback(
            callback_id,
        )

        return

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    message_id = message.get(
        "message_id"
    )

    if not user_id or not chat_id:

        await answer_callback(
            callback_id,
        )

        return

    # -----------------------------------------------------
    # CREATE
    # -----------------------------------------------------

    if data == "create_game":

        async with games_lock:

            game = games.get(chat_id)

            if game is None:

                game = Game(
                    chat_id=chat_id,
                )

                games[chat_id] = game

                save_games()

        await answer_callback(
            callback_id,
            "🎮 تم إنشاء الغرفة!",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # JOIN
    # -----------------------------------------------------

    if data == "join":

        async with games_lock:

            game = games.get(chat_id)

            if game is None:

                game = Game(
                    chat_id=chat_id,
                )

                games[chat_id] = game

            if game.started:

                await answer_callback(
                    callback_id,
                    "❌ اللعبة بدأت بالفعل.",
                    True,
                )

                return

            existing = game.get_player(
                user_id
            )

            if existing:

                await answer_callback(
                    callback_id,
                    "أنت داخل اللعبة بالفعل.",
                )

                return

            name = (
                from_user.get("first_name")
                or from_user.get("username")
                or "لاعب"
            )

            last_name = from_user.get(
                "last_name"
            )

            if last_name:

                name += f" {last_name}"

            game.players.append(
                Player(
                    user_id=user_id,
                    name=name,
                )
            )

            save_games()

        await answer_callback(
            callback_id,
            "✅ انضممت إلى اللعبة!",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # LEAVE
    # -----------------------------------------------------

    if data == "leave":

        async with games_lock:

            game = games.get(chat_id)

            if not game:

                await answer_callback(
                    callback_id,
                    "لا توجد لعبة.",
                )

                return

            player = game.get_player(
                user_id
            )

            if not player:

                await answer_callback(
                    callback_id,
                    "أنت لست داخل اللعبة.",
                )

                return

            if game.started:

                await answer_callback(
                    callback_id,
                    "❌ لا يمكنك الخروج بعد بدء اللعبة.",
                    True,
                )

                return

            game.players.remove(
                player
            )

            save_games()

        await answer_callback(
            callback_id,
            "🚪 خرجت من اللعبة.",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # START GAME
    # -----------------------------------------------------

    if data == "start_game":

        async with games_lock:

            game = games.get(chat_id)

            if not game:

                await answer_callback(
                    callback_id,
                    "❌ لا توجد غرفة.",
                    True,
                )

                return

            if game.started:

                await answer_callback(
                    callback_id,
                    "اللعبة بدأت بالفعل.",
                )

                return

            if len(game.players) < 3:

                await answer_callback(
                    callback_id,
                    "❌ تحتاج اللعبة إلى 3 لاعبين على الأقل.",
                    True,
                )

                return

            assign_roles(game)

            game.started = True
            game.phase = "day"
            game.day = 1

            save_games()

        await answer_callback(
            callback_id,
            "🎭 بدأت اللعبة!",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        asyncio.create_task(
            send_roles(game)
        )

        return

    # -----------------------------------------------------
    # NIGHT
    # -----------------------------------------------------

    if data == "night":

        game = games.get(chat_id)

        if not game or not game.started:

            await answer_callback(
                callback_id,
                "❌ لا توجد لعبة فعالة.",
            )

            return

        game.phase = "night"
        game.night += 1

        save_games()

        await answer_callback(
            callback_id,
            "🌙 بدأ الليل.",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # VOTE
    # -----------------------------------------------------

    if data == "vote":

        game = games.get(chat_id)

        if not game or not game.started:

            await answer_callback(
                callback_id,
                "❌ لا توجد لعبة فعالة.",
            )

            return

        game.phase = "day"

        save_games()

        await answer_callback(
            callback_id,
            "🗳 مرحلة التصويت.",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if data == "status":

        game = games.get(chat_id)

        if not game:

            await answer_callback(
                callback_id,
                "❌ لا توجد لعبة.",
            )

            return

        await answer_callback(
            callback_id,
            "📊 تم تحديث الحالة.",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    # -----------------------------------------------------
    # REFRESH
    # -----------------------------------------------------

    if data == "refresh":

        game = games.get(chat_id)

        if not game:

            await answer_callback(
                callback_id,
                "❌ لا توجد لعبة.",
            )

            return

        await answer_callback(
            callback_id,
            "🔄 تم التحديث.",
        )

        await update_game_message(
            chat_id,
            message_id,
        )

        return

    await answer_callback(
        callback_id,
    )


# =========================================================
# PROCESS TELEGRAM UPDATE
# =========================================================

async def process_update(
    update: dict,
):

    # -----------------------------------------------------
    # MESSAGE
    # -----------------------------------------------------

    message = update.get(
        "message"
    )

    if message:

        text = message.get(
            "text",
            "",
        )

        if text:

            command = text.split(
                "@"
            )[0].split(
                " "
            )[0].lower()

            if command == "/start":

                await handle_start(
                    message
                )

            elif command == "/game":

                await handle_game(
                    message
                )

            elif command == "/reset":

                await handle_reset(
                    message
                )

            elif command == "/status":

                await handle_status(
                    message
                )

        return

    # -----------------------------------------------------
    # CALLBACK
    # -----------------------------------------------------

    callback = update.get(
        "callback_query"
    )

    if callback:

        await handle_callback(
            callback
        )


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI
):

    logger.info(
        "🚀 Starting Mafia FastAPI application..."
    )

    load_games()

    logger.info(
        "🌐 FastAPI started without Telegram polling."
    )

    yield

    logger.info(
        "🛑 Shutting down Mafia application..."
    )

    global http_client

    save_games()

    if http_client:

        try:

            await http_client.aclose()

        except Exception:

            pass

        http_client = None

    logger.info(
        "✅ Mafia application stopped."
    )


# =========================================================
# FASTAPI
# =========================================================

web = FastAPI(
    title="Mafia Game",
    description="Telegram Mafia Game",
    version="3.0.0",
    lifespan=lifespan,
)


# =========================================================
# MINI APP
# =========================================================

MINI_APP_HTML = r"""
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

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    min-height: 100vh;

    font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:
        radial-gradient(
            circle at top,
            #292929,
            #111111 55%,
            #080808
        );

    color: white;

    display: flex;

    justify-content: center;

    align-items: center;

    padding: 20px;
}

.container {

    width: 100%;

    max-width: 500px;

    text-align: center;

    background:
        rgba(255,255,255,0.06);

    border:
        1px solid rgba(255,255,255,0.1);

    border-radius: 24px;

    padding: 30px 20px;

    backdrop-filter: blur(12px);

    box-shadow:
        0 20px 60px rgba(0,0,0,0.45);
}

.logo {

    font-size: 70px;

    margin-bottom: 10px;
}

h1 {

    margin: 0 0 10px;

    font-size: 32px;
}

.subtitle {

    color: #bdbdbd;

    margin-bottom: 30px;
}

button {

    width: 100%;

    border: none;

    border-radius: 15px;

    padding: 16px;

    margin-top: 12px;

    font-size: 17px;

    font-weight: bold;

    cursor: pointer;
}

.primary {

    background: #8b1e1e;

    color: white;
}

.secondary {

    background: #292929;

    color: white;
}

.status {

    margin-top: 20px;

    padding: 14px;

    border-radius: 12px;

    background:
        rgba(255,255,255,0.06);

    color: #aaa;

    font-size: 14px;
}

</style>

</head>

<body>

<div class="container">

    <div class="logo">
        🎭
    </div>

    <h1>
        لعبة المافيا
    </h1>

    <div class="subtitle">
        لعبة تحقيق وخداع اجتماعي
    </div>

    <button
        class="primary"
        onclick="openTelegram()"
    >
        📱 العودة إلى Telegram
    </button>

    <button
        class="secondary"
        onclick="checkServer()"
    >
        🔄 فحص الاتصال
    </button>

    <div
        class="status"
        id="status"
    >
        🟢 السيرفر جاهز
    </div>

</div>

<script>

const tg =
    window.Telegram.WebApp;

tg.ready();

tg.expand();

function openTelegram() {

    try {

        tg.close();

    } catch (e) {

        console.log(e);

    }

}

async function checkServer() {

    const status =
        document.getElementById(
            "status"
        );

    status.innerText =
        "⏳ جاري الفحص...";

    try {

        const response =
            await fetch(
                "/health"
            );

        if (!response.ok) {

            throw new Error(
                "Server error"
            );

        }

        const data =
            await response.json();

        status.innerText =
            "🟢 السيرفر يعمل";

    } catch (error) {

        status.innerText =
            "🔴 تعذر الاتصال بالسيرفر";

    }

}

</script>

</body>

</html>
"""


@web.get(
    "/",
    response_class=HTMLResponse,
)
async def home():

    return MINI_APP_HTML


# =========================================================
# HEALTH
# =========================================================

@web.get("/health")
async def health():

    return {
        "status": "ok",
        "telegram_api": (
            "configured"
            if BOT_TOKEN
            else "missing_token"
        ),
        "webhook_url": WEBHOOK_URL,
    }


# =========================================================
# API STATUS
# =========================================================

@web.get("/api/status")
async def api_status():

    return {
        "app": "mafia-game",
        "status": "online",
        "telegram_api": (
            "configured"
            if BOT_TOKEN
            else "missing",
        ),
        "games": len(games),
        "webhook": WEBHOOK_URL,
    }


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@web.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
):

    try:

        update = await request.json()

        # Telegram expects a quick successful response.
        # We process the update in the background.
        asyncio.create_task(
            process_update(update)
        )

        return {
            "ok": True
        }

    except Exception as e:

        logger.exception(
            "Webhook request failed: %s",
            e,
        )

        return {
            "ok": False,
            "error": "invalid update",
        }


# =========================================================
# SET WEBHOOK
# =========================================================

@web.get("/setup-webhook")
async def setup_webhook():

    try:

        result = await telegram_api(
            "setWebhook",
            {
                "url": WEBHOOK_URL,
                "drop_pending_updates": True,
                "allowed_updates": [
                    "message",
                    "callback_query",
                ],
            },
        )

        logger.info(
            "✅ Webhook configured: %s",
            WEBHOOK_URL,
        )

        return {
            "ok": True,
            "message": "Webhook configured successfully.",
            "webhook_url": WEBHOOK_URL,
            "telegram_result": result,
        }

    except Exception as e:

        logger.exception(
            "Webhook setup failed."
        )

        return {
            "ok": False,
            "error": str(e),
            "webhook_url": WEBHOOK_URL,
        }


# =========================================================
# WEBHOOK INFO
# =========================================================

@web.get("/webhook-info")
async def webhook_info():

    try:

        result = await telegram_api(
            "getWebhookInfo"
        )

        return {
            "ok": True,
            "url": result.get(
                "url",
                "",
            ),
            "pending_update_count": result.get(
                "pending_update_count",
                0,
            ),
            "last_error_message": result.get(
                "last_error_message",
                "",
            ),
            "last_error_date": result.get(
                "last_error_date",
                None,
            ),
            "max_connections": result.get(
                "max_connections",
                None,
            ),
        }

    except Exception as e:

        logger.exception(
            "Could not get webhook info."
        )

        return {
            "ok": False,
            "error": str(e),
        }


# =========================================================
# TELEGRAM BOT INFO
# =========================================================

@web.get("/telegram-test")
async def telegram_test():

    try:

        result = await telegram_api(
            "getMe"
        )

        return {
            "ok": True,
            "telegram": result,
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e),
        }


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:web",
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "8000",
            )
        ),
    )