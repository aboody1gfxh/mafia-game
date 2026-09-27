import os
import json
import asyncio
import logging
import random
from dataclasses import dataclass, field, asdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

WEBAPP_URL = os.environ.get(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev"
).strip()

DATA_FILE = "mafia_games.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mafia-game")


# =========================================================
# ROLES
# =========================================================

ROLES = [
    "🔪 المافيا",
    "🕵️ المحقق",
    "👨‍⚕️ الطبيب",
    "👤 مواطن",
]


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

    def get_player(self, user_id: int) -> Optional[Player]:
        for player in self.players:
            if player.user_id == user_id:
                return player
        return None

    def alive_players(self):
        return [p for p in self.players if p.alive]

    def mafia_players(self):
        return [
            p for p in self.players
            if p.role == "🔪 المافيا" and p.alive
        ]


# =========================================================
# GLOBAL STATE
# =========================================================

games: dict[int, Game] = {}
game_tasks: dict[int, asyncio.Task] = {}

telegram_application: Optional[Application] = None


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

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception:
        logger.exception("Failed to save games")


def load_games():
    global games

    if not os.path.exists(DATA_FILE):
        logger.info("No saved games file found.")
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        games = {}

        for chat_id, game_data in data.items():

            players = [
                Player(**player)
                for player in game_data.get("players", [])
            ]

            game = Game(
                chat_id=int(game_data.get("chat_id", chat_id)),
                players=players,
                phase=game_data.get("phase", "waiting"),
                day=game_data.get("day", 0),
                night=game_data.get("night", 0),
                started=game_data.get("started", False),
                winner=game_data.get("winner", ""),
            )

            games[int(chat_id)] = game

        logger.info("Loaded %s games.", len(games))

    except Exception:
        logger.exception("Failed to load games")


# =========================================================
# KEYBOARDS
# =========================================================

def lobby_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ انضمام",
                callback_data="join"
            ),
            InlineKeyboardButton(
                "🚪 خروج",
                callback_data="leave"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎮 بدء اللعبة",
                callback_data="start_game"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 تحديث",
                callback_data="refresh"
            ),
        ],
    ])


def game_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🌙 بدء الليل",
                callback_data="night"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗳 التصويت",
                callback_data="vote"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 الحالة",
                callback_data="status"
            ),
        ],
    ])


# =========================================================
# GAME TEXT
# =========================================================

def lobby_text(game: Game) -> str:

    if not game.players:
        players_text = "لا يوجد لاعبون حتى الآن."

    else:
        players_text = "\n".join(
            f"{i + 1}. {player.name}"
            for i, player in enumerate(game.players)
        )

    return (
        "🎭 *لعبة المافيا*\n\n"
        "👥 *اللاعبون:*\n"
        f"{players_text}\n\n"
        f"🔢 العدد: {len(game.players)}\n\n"
        "اضغط «انضمام» للدخول إلى اللعبة."
    )


def game_text(game: Game) -> str:

    alive = game.alive_players()

    alive_text = "\n".join(
        f"• {p.name}"
        for p in alive
    )

    phase_names = {
        "waiting": "⏳ انتظار",
        "day": "☀️ النهار",
        "night": "🌙 الليل",
        "finished": "🏁 انتهت",
    }

    phase = phase_names.get(
        game.phase,
        game.phase
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
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_chat:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎭 فتح لعبة المافيا",
                web_app=WebAppInfo(
                    url=WEBAPP_URL
                )
            )
        ],
        [
            InlineKeyboardButton(
                "🎮 إنشاء لعبة",
                callback_data="create_game"
            )
        ],
    ])

    await update.message.reply_text(
        "🎭 *مرحباً بك في لعبة المافيا!*\n\n"
        "افتح اللعبة من الزر بالأسفل أو أنشئ لعبة داخل المجموعة.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# =========================================================
# GAME COMMAND
# =========================================================

async def game_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    game = games.get(chat_id)

    if game is None:
        game = Game(chat_id=chat_id)
        games[chat_id] = game
        save_games()

    await update.message.reply_text(
        lobby_text(game),
        parse_mode="Markdown",
        reply_markup=lobby_keyboard(),
    )


# =========================================================
# RESET
# =========================================================

async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    task = game_tasks.pop(chat_id, None)

    if task:
        task.cancel()

    games.pop(chat_id, None)

    save_games()

    await update.message.reply_text(
        "🗑 تم حذف اللعبة وإعادة ضبط الغرفة."
    )


# =========================================================
# STATUS
# =========================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    game = games.get(chat_id)

    if not game:
        await update.message.reply_text(
            "❌ لا توجد لعبة حالياً.\n"
            "استخدم /game لإنشاء واحدة."
        )
        return

    await update.message.reply_text(
        game_text(game),
        parse_mode="Markdown",
        reply_markup=game_keyboard(),
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    user = query.from_user

    if not user:
        return

    chat = update.effective_chat

    if not chat:
        await query.answer()
        return

    chat_id = chat.id
    action = query.data

    # -----------------------------------------------------
    # CREATE GAME
    # -----------------------------------------------------

    if action == "create_game":

        game = games.get(chat_id)

        if game is None:
            game = Game(chat_id=chat_id)
            games[chat_id] = game
            save_games()

        await query.answer("🎮 تم إنشاء الغرفة")

        try:
            await query.edit_message_text(
                lobby_text(game),
                parse_mode="Markdown",
                reply_markup=lobby_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # JOIN
    # -----------------------------------------------------

    if action == "join":

        game = games.get(chat_id)

        if game is None:
            game = Game(chat_id=chat_id)
            games[chat_id] = game

        if game.started:
            await query.answer(
                "❌ اللعبة بدأت بالفعل.",
                show_alert=True
            )
            return

        existing = game.get_player(user.id)

        if existing:
            await query.answer(
                "أنت داخل اللعبة بالفعل."
            )
            return

        name = user.full_name or user.first_name or "لاعب"

        game.players.append(
            Player(
                user_id=user.id,
                name=name,
            )
        )

        save_games()

        await query.answer(
            "✅ انضممت إلى اللعبة!"
        )

        try:
            await query.edit_message_text(
                lobby_text(game),
                parse_mode="Markdown",
                reply_markup=lobby_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # LEAVE
    # -----------------------------------------------------

    if action == "leave":

        game = games.get(chat_id)

        if not game:
            await query.answer(
                "لا توجد لعبة."
            )
            return

        player = game.get_player(user.id)

        if not player:
            await query.answer(
                "أنت لست داخل اللعبة."
            )
            return

        if game.started:
            await query.answer(
                "❌ لا يمكنك الخروج بعد بدء اللعبة.",
                show_alert=True
            )
            return

        game.players.remove(player)

        save_games()

        await query.answer(
            "🚪 خرجت من اللعبة."
        )

        try:
            await query.edit_message_text(
                lobby_text(game),
                parse_mode="Markdown",
                reply_markup=lobby_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # START GAME
    # -----------------------------------------------------

    if action == "start_game":

        game = games.get(chat_id)

        if not game:
            await query.answer(
                "❌ لا توجد غرفة.",
                show_alert=True
            )
            return

        if game.started:
            await query.answer(
                "اللعبة بدأت بالفعل."
            )
            return

        if len(game.players) < 3:
            await query.answer(
                "❌ تحتاج اللعبة إلى 3 لاعبين على الأقل.",
                show_alert=True
            )
            return

        assign_roles(game)

        game.started = True
        game.phase = "day"
        game.day = 1

        save_games()

        await query.answer(
            "🎭 بدأت اللعبة!"
        )

        try:
            await query.edit_message_text(
                game_text(game),
                parse_mode="Markdown",
                reply_markup=game_keyboard(),
            )
        except Exception:
            pass

        await send_roles(game, context)

        return

    # -----------------------------------------------------
    # NIGHT
    # -----------------------------------------------------

    if action == "night":

        game = games.get(chat_id)

        if not game or not game.started:
            await query.answer(
                "❌ لا توجد لعبة فعالة."
            )
            return

        game.phase = "night"
        game.night += 1

        save_games()

        await query.answer(
            "🌙 بدأ الليل."
        )

        try:
            await query.edit_message_text(
                game_text(game),
                parse_mode="Markdown",
                reply_markup=game_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # VOTE
    # -----------------------------------------------------

    if action == "vote":

        game = games.get(chat_id)

        if not game or not game.started:
            await query.answer(
                "❌ لا توجد لعبة فعالة."
            )
            return

        game.phase = "day"

        save_games()

        await query.answer(
            "🗳 مرحلة التصويت."
        )

        try:
            await query.edit_message_text(
                game_text(game),
                parse_mode="Markdown",
                reply_markup=game_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if action == "status":

        game = games.get(chat_id)

        if not game:
            await query.answer(
                "❌ لا توجد لعبة."
            )
            return

        await query.answer(
            "📊 تم تحديث الحالة."
        )

        try:
            await query.edit_message_text(
                game_text(game),
                parse_mode="Markdown",
                reply_markup=game_keyboard(),
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # REFRESH
    # -----------------------------------------------------

    if action == "refresh":

        game = games.get(chat_id)

        if not game:
            await query.answer(
                "❌ لا توجد لعبة."
            )
            return

        await query.answer(
            "🔄 تم التحديث."
        )

        try:
            await query.edit_message_text(
                lobby_text(game)
                if not game.started
                else game_text(game),
                parse_mode="Markdown",
                reply_markup=(
                    lobby_keyboard()
                    if not game.started
                    else game_keyboard()
                ),
            )
        except Exception:
            pass

        return

    await query.answer()


# =========================================================
# ROLE ASSIGNMENT
# =========================================================

def assign_roles(game: Game):

    players = game.players.copy()

    random.shuffle(players)

    count = len(players)

    roles = []

    if count >= 3:
        roles.append("🔪 المافيا")

    if count >= 4:
        roles.append("🕵️ المحقق")

    if count >= 5:
        roles.append("👨‍⚕️ الطبيب")

    while len(roles) < count:
        roles.append("👤 مواطن")

    random.shuffle(roles)

    for player, role in zip(players, roles):
        player.role = role


# =========================================================
# SEND PRIVATE ROLES
# =========================================================

async def send_roles(
    game: Game,
    context: ContextTypes.DEFAULT_TYPE,
):

    for player in game.players:

        if player.is_ai:
            continue

        try:
            await context.bot.send_message(
                chat_id=player.user_id,
                text=(
                    "🎭 *دورك في اللعبة*\n\n"
                    f"دورك هو: {player.role}\n\n"
                    "لا تخبر اللاعبين الآخرين بدورك."
                ),
                parse_mode="Markdown",
            )

        except Exception:
            logger.warning(
                "Could not send role to user %s",
                player.user_id,
            )


# =========================================================
# BUILD TELEGRAM APPLICATION
# =========================================================

def build_application() -> Application:

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "game",
            game_command
        )
    )

    application.add_handler(
        CommandHandler(
            "reset",
            reset_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    logger.info("Telegram application built successfully.")

    return application


# =========================================================
# TELEGRAM BACKGROUND START
# =========================================================

async def telegram_worker():

    global telegram_application

    if not BOT_TOKEN:
        logger.error(
            "❌ BOT_TOKEN is missing. "
            "Telegram bot will not start."
        )
        return

    for attempt in range(1, 6):

        try:

            logger.info(
                "📡 Connecting to Telegram... attempt %s/5",
                attempt,
            )

            telegram_application = build_application()

            await telegram_application.initialize()

            logger.info(
                "✅ Telegram application initialized."
            )

            await telegram_application.start()

            logger.info(
                "✅ Telegram application started."
            )

            if telegram_application.updater:

                await telegram_application.updater.start_polling(
                    drop_pending_updates=True
                )

                logger.info(
                    "✅ Telegram polling started successfully."
                )

            return

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "❌ Telegram startup failed "
                "on attempt %s/5",
                attempt,
            )

            if telegram_application:

                try:
                    if telegram_application.updater:
                        await telegram_application.updater.stop()
                except Exception:
                    pass

                try:
                    await telegram_application.stop()
                except Exception:
                    pass

                try:
                    await telegram_application.shutdown()
                except Exception:
                    pass

            telegram_application = None

            if attempt < 5:

                logger.info(
                    "⏳ Retrying Telegram connection "
                    "in 10 seconds..."
                )

                await asyncio.sleep(10)

    logger.error(
        "❌ Telegram could not connect after 5 attempts."
    )


# =========================================================
# MINI APP HTML
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

    background: rgba(255,255,255,0.06);

    border: 1px solid rgba(255,255,255,0.1);

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

    background: rgba(255,255,255,0.06);

    color: #aaa;

    font-size: 14px;
}

</style>

</head>

<body>

<div class="container">

    <div class="logo">🎭</div>

    <h1>لعبة المافيا</h1>

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
        🟢 Mini App يعمل بنجاح
    </div>

</div>

<script>

const tg = window.Telegram.WebApp;

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
        document.getElementById("status");

    status.innerText =
        "⏳ جاري فحص السيرفر...";

    try {

        const response =
            await fetch("/health");

        const data =
            await response.json();

        status.innerText =
            "🟢 السيرفر يعمل: " +
            data.status;

    } catch (error) {

        status.innerText =
            "🔴 تعذر الاتصال بالسيرفر.";

    }

}

</script>

</body>
</html>
"""


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info(
        "🚀 Starting Mafia FastAPI application..."
    )

    load_games()

    # Telegram يعمل بالخلفية
    # ولن يمنع FastAPI من الإقلاع
    telegram_task = asyncio.create_task(
        telegram_worker()
    )

    try:

        yield

    finally:

        logger.info(
            "🛑 Shutting down Mafia application..."
        )

        telegram_task.cancel()

        try:
            await telegram_task
        except asyncio.CancelledError:
            pass

        if telegram_application:

            try:
                if telegram_application.updater:
                    await telegram_application.updater.stop()
            except Exception:
                logger.exception(
                    "Error stopping updater"
                )

            try:
                await telegram_application.stop()
            except Exception:
                logger.exception(
                    "Error stopping Telegram application"
                )

            try:
                await telegram_application.shutdown()
            except Exception:
                logger.exception(
                    "Error shutting down Telegram application"
                )

        save_games()

        logger.info(
            "✅ Mafia application stopped."
        )


# =========================================================
# FASTAPI APP
# =========================================================

web = FastAPI(
    title="Mafia Game",
    description="Telegram Mafia Game",
    version="1.0.0",
    lifespan=lifespan,
)


# =========================================================
# ROUTES
# =========================================================

@web.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return MINI_APP_HTML


@web.get("/health")
async def health():

    return {
        "status": "ok",
        "telegram": (
            "connected"
            if telegram_application
            else "starting"
        ),
    }


@web.get("/api/status")
async def api_status():

    return {
        "app": "mafia-game",
        "status": "online",
        "telegram": (
            "running"
            if telegram_application
            else "offline"
        ),
        "games": len(games),
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
                "8000"
            )
        ),
    )