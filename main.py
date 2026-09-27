# ============================================================
# 🎭 لعبة المافيا - Telegram Bot + FastAPI
#
# يعمل على:
# - FastAPI Cloud
# - Telegram Bot
# - Telegram Mini App
#
# Python 3.10+
# python-telegram-bot 22.x
# ============================================================

import asyncio
import json
import logging
import os
import random

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from telegram.constants import ChatType

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


# ============================================================
# ⚙️ الإعدادات
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

WEBAPP_URL = os.environ.get(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev"
).strip()

MIN_PLAYERS = 6
MAX_PLAYERS = 12

NIGHT_SECONDS = 45
DISCUSSION_SECONDS = 60
VOTE_SECONDS = 45

DATA_FILE = "mafia_games.json"


# ============================================================
# 📝 Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("MafiaBot")


# ============================================================
# 🎴 الأدوار
# ============================================================

ROLE_INFO = {

    "mafia": {
        "name": "🔪 المافيا",
        "team": "mafia",
        "description": (
            "أنت من المافيا. "
            "تعمل على التخلص من المواطنين دون أن يتم كشفك."
        ),
    },

    "doctor": {
        "name": "💉 الطبيب",
        "team": "citizen",
        "description": (
            "كل ليلة تستطيع إنقاذ لاعب واحد."
        ),
    },

    "detective": {
        "name": "🔎 المحقق",
        "team": "citizen",
        "description": (
            "كل ليلة تستطيع فحص لاعب لمعرفة هل هو من المافيا."
        ),
    },

    "jester": {
        "name": "🃏 المهرج",
        "team": "neutral",
        "description": (
            "هدفك أن يتم التصويت عليك وإخراجك من اللعبة."
        ),
    },

    "citizen": {
        "name": "👤 المواطن",
        "team": "citizen",
        "description": (
            "ليس لديك قدرة خاصة. "
            "حاول اكتشاف المافيا مع بقية المواطنين."
        ),
    },
}


# ============================================================
# 👤 Player
# ============================================================

@dataclass
class Player:

    user_id: int
    name: str

    role: Optional[str] = None

    alive: bool = True

    is_human: bool = True

    ai_personality: str = "normal"

    joined: bool = True

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(data):
        return Player(**data)


# ============================================================
# 🎮 Game
# ============================================================

@dataclass
class Game:

    chat_id: int

    players: dict = field(default_factory=dict)

    phase: str = "lobby"

    day: int = 0

    started: bool = False

    finished: bool = False

    mode: str = "multiplayer"

    night_kill: Optional[int] = None
    doctor_save: Optional[int] = None
    detective_check: Optional[int] = None

    votes: dict = field(default_factory=dict)

    winner: Optional[str] = None

    lobby_message_id: Optional[int] = None

    def to_dict(self):

        return {
            "chat_id": self.chat_id,

            "players": {
                str(uid): player.to_dict()
                for uid, player in self.players.items()
            },

            "phase": self.phase,
            "day": self.day,
            "started": self.started,
            "finished": self.finished,

            "mode": self.mode,

            "night_kill": self.night_kill,
            "doctor_save": self.doctor_save,
            "detective_check": self.detective_check,

            "votes": {
                str(uid): vote
                for uid, vote in self.votes.items()
            },

            "winner": self.winner,

            "lobby_message_id": self.lobby_message_id,
        }

    @staticmethod
    def from_dict(data):

        game = Game(
            chat_id=data["chat_id"],
            phase=data.get("phase", "lobby"),
            day=data.get("day", 0),
            started=data.get("started", False),
            finished=data.get("finished", False),
            mode=data.get("mode", "multiplayer"),
            night_kill=data.get("night_kill"),
            doctor_save=data.get("doctor_save"),
            detective_check=data.get("detective_check"),
            winner=data.get("winner"),
            lobby_message_id=data.get("lobby_message_id"),
        )

        game.players = {
            int(uid): Player.from_dict(player)
            for uid, player in data.get(
                "players",
                {}
            ).items()
        }

        game.votes = {
            int(uid): int(vote)
            for uid, vote in data.get(
                "votes",
                {}
            ).items()
        }

        return game


# ============================================================
# 🌍 Global state
# ============================================================

games = {}

game_tasks = {}

telegram_application: Optional[Application] = None


# ============================================================
# 💾 Save / Load
# ============================================================

def save_games():

    try:

        data = {
            str(chat_id): game.to_dict()
            for chat_id, game in games.items()
        }

        with open(
            DATA_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

    except Exception:

        logger.exception(
            "Failed to save games"
        )


def load_games():

    global games

    if not os.path.exists(DATA_FILE):
        return

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        games = {
            int(chat_id): Game.from_dict(game)
            for chat_id, game in data.items()
        }

        logger.info(
            "Loaded %s saved games",
            len(games)
        )

    except Exception:

        logger.exception(
            "Failed to load games"
        )


# ============================================================
# 🔧 Helpers
# ============================================================

def get_game(chat_id):

    return games.get(chat_id)


def alive_players(game):

    return [
        player
        for player in game.players.values()
        if player.alive
    ]


def human_players(game):

    return [
        player
        for player in game.players.values()
        if player.is_human
    ]


def mafia_players(game):

    return [
        player
        for player in alive_players(game)
        if player.role == "mafia"
    ]


def cancel_game_task(chat_id):

    task = game_tasks.get(chat_id)

    if task and not task.done():
        task.cancel()

    game_tasks.pop(
        chat_id,
        None
    )


def create_game_task(chat_id, coroutine):

    cancel_game_task(chat_id)

    task = asyncio.create_task(
        coroutine
    )

    game_tasks[chat_id] = task

    return task


# ============================================================
# 🏠 Lobby keyboard
# ============================================================

def lobby_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🎮 انضمام",
                callback_data="mf_join"
            ),

            InlineKeyboardButton(
                "🚪 مغادرة",
                callback_data="mf_leave"
            ),
        ],

        [
            InlineKeyboardButton(
                "🚀 بدء اللعبة",
                callback_data="mf_start"
            ),
        ],

    ])


# ============================================================
# 🏠 Lobby text
# ============================================================

def lobby_text(game):

    count = len(game.players)

    lines = [
        "🎭 **لعبة المافيا**",
        "",
        "🏠 **غرفة الانتظار**",
        "",
        f"👥 اللاعبين: **{count}/{MAX_PLAYERS}**",
        "",
    ]

    if game.players:

        for player in game.players.values():

            if player.is_human:

                lines.append(
                    f"• 👤 {player.name}"
                )

            else:

                lines.append(
                    f"• 🤖 {player.name}"
                )

    else:

        lines.append(
            "لا يوجد لاعبون بعد."
        )

    lines.extend([
        "",
        f"🔸 الحد الأدنى للعبة الجماعية: {MIN_PLAYERS}",
        "",
        "💡 إذا كنت وحدك تستطيع بدء اللعبة،",
        "وسيضيف البوت لاعبين AI تلقائيًا.",
    ])

    return "\n".join(lines)


# ============================================================
# /start
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🎭 فتح لعبة المافيا",
                web_app=WebAppInfo(
                    url=WEBAPP_URL
                )
            )
        ]

    ])

    text = (
        "🎭 **لعبة المافيا**\n\n"

        "أهلًا بك في لعبة المافيا!\n\n"

        "يمكنك فتح واجهة اللعبة من الزر بالأسفل.\n\n"

        "ولإنشاء لعبة داخل مجموعة استخدم:\n"
        "🎮 `/game`"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ============================================================
# /game
# ============================================================

async def game_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        await update.effective_message.reply_text(
            "❌ استخدم /game داخل المجموعة."
        )

        return

    existing = get_game(chat.id)

    if existing and not existing.finished:

        await update.effective_message.reply_text(
            "⚠️ توجد لعبة أو غرفة انتظار بالفعل."
        )

        return

    game = Game(
        chat_id=chat.id
    )

    games[chat.id] = game

    save_games()

    message = await update.effective_message.reply_text(
        lobby_text(game),
        parse_mode="Markdown",
        reply_markup=lobby_keyboard()
    )

    game.lobby_message_id = message.message_id

    save_games()


# ============================================================
# 👤 Join
# ============================================================

async def join_game(
    query,
    game
):

    user = query.from_user

    if game.started:

        await query.answer(
            "اللعبة بدأت بالفعل.",
            show_alert=True
        )

        return

    if user.id in game.players:

        await query.answer(
            "أنت داخل اللعبة بالفعل.",
            show_alert=True
        )

        return

    if len(game.players) >= MAX_PLAYERS:

        await query.answer(
            "الغرفة ممتلئة.",
            show_alert=True
        )

        return

    game.players[user.id] = Player(
        user_id=user.id,
        name=user.full_name,
        is_human=True
    )

    save_games()

    await query.answer(
        "تم انضمامك 🎭"
    )

    try:

        await query.edit_message_text(
            lobby_text(game),
            parse_mode="Markdown",
            reply_markup=lobby_keyboard()
        )

    except Exception:

        pass


# ============================================================
# 🚪 Leave
# ============================================================

async def leave_game(
    query,
    game
):

    user = query.from_user

    if game.started:

        await query.answer(
            "لا يمكنك المغادرة بعد بداية اللعبة.",
            show_alert=True
        )

        return

    if user.id not in game.players:

        await query.answer(
            "أنت لست داخل اللعبة.",
            show_alert=True
        )

        return

    del game.players[user.id]

    save_games()

    await query.answer(
        "تمت مغادرة اللعبة."
    )

    try:

        await query.edit_message_text(
            lobby_text(game),
            parse_mode="Markdown",
            reply_markup=lobby_keyboard()
        )

    except Exception:

        pass


# ============================================================
# 🤖 Create AI
# ============================================================

def add_ai_players(
    game,
    needed
):

    personalities = [
        "normal",
        "aggressive",
        "quiet",
        "suspicious",
        "logical",
        "random",
    ]

    existing_ai = len([
        p for p in game.players.values()
        if not p.is_human
    ])

    for index in range(
        existing_ai + 1,
        existing_ai + needed + 1
    ):

        ai_id = -(
            abs(game.chat_id) * 100
            + index
        )

        player = Player(
            user_id=ai_id,
            name=f"AI {index}",
            is_human=False,
            ai_personality=random.choice(
                personalities
            )
        )

        game.players[ai_id] = player


# ============================================================
# 🎴 Assign roles
# ============================================================

def assign_roles(game):

    players = list(
        game.players.values()
    )

    random.shuffle(players)

    count = len(players)

    if count <= 7:
        mafia_count = 1

    elif count <= 10:
        mafia_count = 2

    else:
        mafia_count = 3

    roles = []

    roles.extend(
        ["mafia"] * mafia_count
    )

    remaining = count - mafia_count

    if remaining >= 3:

        roles.append("doctor")
        roles.append("detective")

    if remaining >= 5:

        roles.append("jester")

    while len(roles) < count:

        roles.append("citizen")

    random.shuffle(roles)

    for player, role in zip(
        players,
        roles
    ):

        player.role = role
        player.alive = True


# ============================================================
# 🚀 Start game
# ============================================================

async def start_game(
    query,
    game
):

    if game.started:

        await query.answer(
            "اللعبة بدأت بالفعل.",
            show_alert=True
        )

        return

    count = len(game.players)

    if count < MIN_PLAYERS:

        needed = MIN_PLAYERS - count

        if count == 0:

            await query.answer(
                "يجب أن تنضم أنت أولًا.",
                show_alert=True
            )

            return

        add_ai_players(
            game,
            needed
        )

        game.mode = "single"

    else:

        game.mode = "multiplayer"

    game.started = True

    game.phase = "night"

    game.day = 1

    assign_roles(game)

    save_games()

    await query.answer(
        "🎭 بدأت اللعبة!"
    )

    await send_roles(
        query.get_bot(),
        game
    )

    await show_night(
        query.get_bot(),
        game
    )


# ============================================================
# 📩 Send roles
# ============================================================

async def send_roles(
    bot,
    game
):

    for player in game.players.values():

        if not player.is_human:
            continue

        role = ROLE_INFO[
            player.role
        ]

        text = (
            "🎭 **لعبة المافيا**\n\n"

            "🎴 **دورك:** "
            f"{role['name']}\n\n"

            f"📖 {role['description']}\n\n"
        )

        if player.role == "mafia":

            teammates = [
                p
                for p in mafia_players(game)
                if p.user_id != player.user_id
            ]

            if teammates:

                text += "🔪 **أعضاء المافيا:**\n"

                for teammate in teammates:

                    text += (
                        f"• {teammate.name}\n"
                    )

                text += "\n"

        try:

            await bot.send_message(
                chat_id=player.user_id,
                text=text,
                parse_mode="Markdown"
            )

        except Exception as error:

            logger.warning(
                "Could not send role to %s: %s",
                player.user_id,
                error
            )


# ============================================================
# 🌙 Night
# ============================================================

def night_text(game):

    return (
        "🌙 **بدأ الليل**\n\n"
        f"🌑 اليوم: {game.day}\n"
        f"👥 الأحياء: {len(alive_players(game))}\n\n"
        "الأدوار الخاصة تقوم بأفعالها الآن.\n\n"
        "🔒 راقب رسائلك الخاصة."
    )


async def show_night(
    bot,
    game
):

    game.phase = "night"

    game.night_kill = None
    game.doctor_save = None
    game.detective_check = None

    save_games()

    try:

        await bot.send_message(
            chat_id=game.chat_id,
            text=night_text(game),
            parse_mode="Markdown"
        )

    except Exception:

        pass

    await send_night_actions(
        bot,
        game
    )

    create_game_task(
        game.chat_id,
        night_timer(
            bot,
            game
        )
    )


# ============================================================
# 🌙 Night actions
# ============================================================

async def send_night_actions(
    bot,
    game
):

    await ai_night_actions(game)

    for player in alive_players(game):

        if not player.is_human:
            continue

        buttons = []

        if player.role == "mafia":

            targets = [
                p
                for p in alive_players(game)
                if p.role != "mafia"
            ]

            for target in targets:

                buttons.append([
                    InlineKeyboardButton(
                        f"🔪 {target.name}",
                        callback_data=(
                            f"mf_kill_{target.user_id}"
                        )
                    )
                ])

            if buttons:

                try:

                    await bot.send_message(
                        chat_id=player.user_id,
                        text="🔪 **اختر هدف المافيا**",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            buttons
                        )
                    )

                except Exception:
                    pass

        elif player.role == "doctor":

            for target in alive_players(game):

                buttons.append([
                    InlineKeyboardButton(
                        f"💉 إنقاذ {target.name}",
                        callback_data=(
                            f"mf_save_{target.user_id}"
                        )
                    )
                ])

            try:

                await bot.send_message(
                    chat_id=player.user_id,
                    text="💉 **اختر من تريد إنقاذه**",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        buttons
                    )
                )

            except Exception:
                pass

        elif player.role == "detective":

            for target in alive_players(game):

                if target.user_id == player.user_id:
                    continue

                buttons.append([
                    InlineKeyboardButton(
                        f"🔎 فحص {target.name}",
                        callback_data=(
                            f"mf_check_{target.user_id}"
                        )
                    )
                ])

            try:

                await bot.send_message(
                    chat_id=player.user_id,
                    text="🔎 **اختر شخصًا للتحقيق معه**",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        buttons
                    )
                )

            except Exception:
                pass


# ============================================================
# 🤖 AI Night
# ============================================================

async def ai_night_actions(game):

    alive = alive_players(game)

    ai_mafia = [
        p
        for p in alive
        if not p.is_human
        and p.role == "mafia"
    ]

    if ai_mafia:

        targets = [
            p
            for p in alive
            if p.role != "mafia"
        ]

        if targets:

            target = random.choice(targets)

            game.night_kill = target.user_id

    doctors = [
        p
        for p in alive
        if not p.is_human
        and p.role == "doctor"
    ]

    if doctors:

        game.doctor_save = random.choice(
            alive
        ).user_id

    detectives = [
        p
        for p in alive
        if not p.is_human
        and p.role == "detective"
    ]

    if detectives:

        targets = [
            p
            for p in alive
            if p.role != "detective"
        ]

        if targets:

            game.detective_check = random.choice(
                targets
            ).user_id


# ============================================================
# ⏱ Night timer
# ============================================================

async def night_timer(
    bot,
    game
):

    try:

        await asyncio.sleep(
            NIGHT_SECONDS
        )

        if game.finished:
            return

        if game.phase != "night":
            return

        await resolve_night(
            bot,
            game
        )

    except asyncio.CancelledError:
        return

    except Exception:
        logger.exception(
            "Night timer error"
        )


# ============================================================
# 🌅 Resolve night
# ============================================================

async def resolve_night(
    bot,
    game
):

    killed = game.night_kill
    saved = game.doctor_save

    dead_name = None

    if killed and killed != saved:

        player = game.players.get(killed)

        if player and player.alive:

            player.alive = False
            dead_name = player.name

    game.night_kill = None
    game.doctor_save = None
    game.detective_check = None

    save_games()

    if await check_winner(bot, game):
        return

    await show_day(
        bot,
        game,
        dead_name
    )


# ============================================================
# ☀️ Day
# ============================================================

async def show_day(
    bot,
    game,
    dead_name=None
):

    game.phase = "day"

    save_games()

    if dead_name:

        text = (
            "☀️ **صباح جديد**\n\n"
            f"💀 خلال الليل خرج: **{dead_name}**\n\n"
            "💬 بدأ وقت النقاش."
        )

    else:

        text = (
            "☀️ **صباح جديد**\n\n"
            "✨ لم يخرج أي لاعب هذه الليلة.\n\n"
            "💬 بدأ وقت النقاش."
        )

    try:

        await bot.send_message(
            chat_id=game.chat_id,
            text=text,
            parse_mode="Markdown"
        )

    except Exception:
        pass

    await ai_day_behavior(game)

    create_game_task(
        game.chat_id,
        discussion_timer(
            bot,
            game
        )
    )


# ============================================================
# 🤖 AI Day
# ============================================================

async def ai_day_behavior(game):
    return


# ============================================================
# ⏱ Discussion timer
# ============================================================

async def discussion_timer(
    bot,
    game
):

    try:

        await asyncio.sleep(
            DISCUSSION_SECONDS
        )

        if game.finished:
            return

        if game.phase != "day":
            return

        await start_voting(
            bot,
            game
        )

    except asyncio.CancelledError:
        return

    except Exception:
        logger.exception(
            "Discussion timer error"
        )


# ============================================================
# 🗳 Start voting
# ============================================================

async def start_voting(
    bot,
    game
):

    game.phase = "vote"

    game.votes = {}

    save_games()

    ai_votes(game)

    buttons = []

    targets = alive_players(game)

    for target in targets:

        buttons.append([
            InlineKeyboardButton(
                f"🗳 {target.name}",
                callback_data=(
                    f"mf_vote_{target.user_id}"
                )
            )
        ])

    text = (
        "🗳 **بدأ التصويت**\n\n"
        "اختر اللاعب الذي تشك به.\n\n"
        f"⏱ الوقت: {VOTE_SECONDS} ثانية"
    )

    try:

        await bot.send_message(
            chat_id=game.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                buttons
            )
        )

    except Exception:
        pass

    create_game_task(
        game.chat_id,
        vote_timer(
            bot,
            game
        )
    )


# ============================================================
# 🤖 AI Voting
# ============================================================

def ai_votes(game):

    alive = alive_players(game)

    ai_players = [
        p
        for p in alive
        if not p.is_human
    ]

    targets = [
        p
        for p in alive
        if p.alive
    ]

    if not targets:
        return

    for ai in ai_players:

        possible = [
            p
            for p in targets
            if p.user_id != ai.user_id
        ]

        if not possible:
            continue

        if ai.role == "mafia":

            citizen_targets = [
                p
                for p in possible
                if p.role != "mafia"
            ]

            if citizen_targets:
                target = random.choice(
                    citizen_targets
                )
            else:
                target = random.choice(
                    possible
                )

        else:

            mafia_targets = [
                p
                for p in possible
                if p.role == "mafia"
            ]

            if mafia_targets and random.random() < 0.65:
                target = random.choice(
                    mafia_targets
                )
            else:
                target = random.choice(
                    possible
                )

        game.votes[
            ai.user_id
        ] = target.user_id


# ============================================================
# ⏱ Vote timer
# ============================================================

async def vote_timer(
    bot,
    game
):

    try:

        await asyncio.sleep(
            VOTE_SECONDS
        )

        if game.finished:
            return

        if game.phase != "vote":
            return

        await resolve_votes(
            bot,
            game
        )

    except asyncio.CancelledError:
        return

    except Exception:
        logger.exception(
            "Vote timer error"
        )


# ============================================================
# 🧮 Resolve votes
# ============================================================

async def resolve_votes(
    bot,
    game
):

    if game.phase != "vote":
        return

    game.phase = "resolving"

    if not game.votes:

        await bot.send_message(
            chat_id=game.chat_id,
            text=(
                "🗳 لم يصوّت أحد.\n\n"
                "لم يتم إخراج أي لاعب."
            )
        )

        game.day += 1

        save_games()

        if await check_winner(bot, game):
            return

        await show_night(bot, game)

        return

    counts = {}

    for target_id in game.votes.values():

        counts[target_id] = (
            counts.get(target_id, 0) + 1
        )

    highest = max(
        counts.values()
    )

    winners = [
        uid
        for uid, count in counts.items()
        if count == highest
    ]

    if len(winners) != 1:

        await bot.send_message(
            chat_id=game.chat_id,
            text=(
                "⚖️ **تعادل!**\n\n"
                "لم يتم إخراج أي لاعب."
            ),
            parse_mode="Markdown"
        )

    else:

        eliminated_id = winners[0]

        player = game.players.get(
            eliminated_id
        )

        if player and player.alive:

            player.alive = False

            role_name = ROLE_INFO[
                player.role
            ]["name"]

            if player.role == "jester":

                game.finished = True
                game.winner = "jester"

                await bot.send_message(
                    chat_id=game.chat_id,
                    text=(
                        "🃏 **المهرج فاز!**\n\n"
                        f"😂 تم إخراج **{player.name}** "
                        "بالتصويت.\n\n"
                        "وكان هذا هو هدفه!"
                    ),
                    parse_mode="Markdown"
                )

                await finish_game(
                    bot,
                    game
                )

                return

            await bot.send_message(
                chat_id=game.chat_id,
                text=(
                    "⚰️ **تم إخراج لاعب**\n\n"
                    f"👤 {player.name}\n"
                    f"🎴 دوره: **{role_name}**"
                ),
                parse_mode="Markdown"
            )

    game.votes = {}

    save_games()

    if await check_winner(bot, game):
        return

    game.day += 1

    await show_night(bot, game)


# ============================================================
# 🏆 Check winner
# ============================================================

async def check_winner(
    bot,
    game
):

    alive = alive_players(game)

    mafia_count = len([
        p
        for p in alive
        if p.role == "mafia"
    ])

    other_count = len([
        p
        for p in alive
        if p.role != "mafia"
        and p.role != "jester"
    ])

    if mafia_count == 0:

        game.finished = True
        game.winner = "citizens"

        await bot.send_message(
            chat_id=game.chat_id,
            text=(
                "🎉 **المواطنون فازوا!**\n\n"
                "🔎 تم القضاء على جميع أفراد المافيا."
            ),
            parse_mode="Markdown"
        )

        await finish_game(bot, game)

        return True

    if mafia_count >= other_count:

        game.finished = True
        game.winner = "mafia"

        await bot.send_message(
            chat_id=game.chat_id,
            text=(
                "🔪 **المافيا فازت!**\n\n"
                "لم يعد بإمكان المواطنين إيقاف المافيا."
            ),
            parse_mode="Markdown"
        )

        await finish_game(bot, game)

        return True

    return False


# ============================================================
# 🏁 Finish
# ============================================================

async def finish_game(
    bot,
    game
):

    cancel_game_task(
        game.chat_id
    )

    game.finished = True
    game.phase = "finished"

    save_games()

    lines = [
        "🎭 **انتهت اللعبة**",
        "",
        "📜 **الأدوار النهائية:**",
        "",
    ]

    for player in game.players.values():

        role_name = ROLE_INFO.get(
            player.role,
            {}
        ).get(
            "name",
            "غير معروف"
        )

        status = (
            "❤️ حي"
            if player.alive
            else "💀 ميت"
        )

        robot = (
            "🤖 "
            if not player.is_human
            else "👤 "
        )

        lines.append(
            f"{robot}{player.name} — "
            f"{role_name} — {status}"
        )

    lines.append("")

    if game.winner == "mafia":

        lines.append(
            "🏆 الفائز: 🔪 المافيا"
        )

    elif game.winner == "citizens":

        lines.append(
            "🏆 الفائز: 👥 المواطنون"
        )

    elif game.winner == "jester":

        lines.append(
            "🏆 الفائز: 🃏 المهرج"
        )

    if game.mode == "single":

        lines.extend([
            "",
            "🤖 هذه كانت مباراة فردية مع لاعبين AI."
        ])

    try:

        await bot.send_message(
            chat_id=game.chat_id,
            text="\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception:
        pass


# ============================================================
# 🎛️ Callback handler
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    data = query.data

    if not query.message:
        return

    chat_id = query.message.chat.id

    game = get_game(chat_id)

    if not game:

        await query.answer(
            "لا توجد لعبة.",
            show_alert=True
        )

        return

    if data == "mf_join":

        await join_game(
            query,
            game
        )

        return

    if data == "mf_leave":

        await leave_game(
            query,
            game
        )

        return

    if data == "mf_start":

        await start_game(
            query,
            game
        )

        return

    if data.startswith("mf_kill_"):

        if game.phase != "night":

            await query.answer(
                "انتهى الليل.",
                show_alert=True
            )

            return

        player = game.players.get(
            query.from_user.id
        )

        if not player or not player.alive:

            await query.answer(
                "لا يمكنك استخدام هذا.",
                show_alert=True
            )

            return

        if player.role != "mafia":

            await query.answer(
                "هذا الزر ليس لك.",
                show_alert=True
            )

            return

        target_id = int(
            data.split("_")[-1]
        )

        target = game.players.get(
            target_id
        )

        if not target or not target.alive:

            await query.answer(
                "الهدف غير متاح.",
                show_alert=True
            )

            return

        if target.role == "mafia":

            await query.answer(
                "لا يمكنك اختيار عضو من المافيا.",
                show_alert=True
            )

            return

        game.night_kill = target_id

        save_games()

        await query.answer(
            f"تم اختيار {target.name} 🔪"
        )

        return

    if data.startswith("mf_save_"):

        if game.phase != "night":

            await query.answer(
                "انتهى الليل.",
                show_alert=True
            )

            return

        player = game.players.get(
            query.from_user.id
        )

        if not player or player.role != "doctor":

            await query.answer(
                "هذا الزر ليس لك.",
                show_alert=True
            )

            return

        target_id = int(
            data.split("_")[-1]
        )

        target = game.players.get(
            target_id
        )

        if not target or not target.alive:

            await query.answer(
                "اللاعب غير متاح.",
                show_alert=True
            )

            return

        game.doctor_save = target_id

        save_games()

        await query.answer(
            f"تم إنقاذ {target.name} 💉"
        )

        return

    if data.startswith("mf_check_"):

        if game.phase != "night":

            await query.answer(
                "انتهى الليل.",
                show_alert=True
            )

            return

        player = game.players.get(
            query.from_user.id
        )

        if not player or player.role != "detective":

            await query.answer(
                "هذا الزر ليس لك.",
                show_alert=True
            )

            return

        target_id = int(
            data.split("_")[-1]
        )

        target = game.players.get(
            target_id
        )

        if not target or not target.alive:

            await query.answer(
                "اللاعب غير متاح.",
                show_alert=True
            )

            return

        result = (
            "🔪 مافيا"
            if target.role == "mafia"
            else "👥 ليس من المافيا"
        )

        try:

            await context.bot.send_message(
                chat_id=player.user_id,
                text=(
                    "🔎 **نتيجة التحقيق**\n\n"
                    f"👤 اللاعب: {target.name}\n\n"
                    f"📋 النتيجة: **{result}**"
                ),
                parse_mode="Markdown"
            )

        except Exception:
            pass

        await query.answer(
            "تم التحقيق 🔎"
        )

        return

    if data.startswith("mf_vote_"):

        if game.phase != "vote":

            await query.answer(
                "التصويت غير مفتوح.",
                show_alert=True
            )

            return

        voter = game.players.get(
            query.from_user.id
        )

        if not voter or not voter.alive:

            await query.answer(
                "لا يمكنك التصويت.",
                show_alert=True
            )

            return

        target_id = int(
            data.split("_")[-1]
        )

        target = game.players.get(
            target_id
        )

        if not target or not target.alive:

            await query.answer(
                "اللاعب غير متاح.",
                show_alert=True
            )

            return

        if voter.user_id == target.user_id:

            await query.answer(
                "لا يمكنك التصويت لنفسك.",
                show_alert=True
            )

            return

        game.votes[
            voter.user_id
        ] = target.user_id

        save_games()

        await query.answer(
            f"تم التصويت على {target.name} 🗳"
        )

        return


# ============================================================
# 🧹 /reset
# ============================================================

async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        return

    member = await context.bot.get_chat_member(
        chat.id,
        update.effective_user.id
    )

    if member.status not in (
        "administrator",
        "creator",
    ):

        await update.effective_message.reply_text(
            "❌ هذا الأمر للمشرفين فقط."
        )

        return

    cancel_game_task(chat.id)

    if chat.id in games:

        del games[chat.id]

    save_games()

    await update.effective_message.reply_text(
        "🧹 تم حذف اللعبة وإعادة الغرفة."
    )


# ============================================================
# 📊 /status
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    game = get_game(chat.id)

    if not game:

        await update.effective_message.reply_text(
            "❌ لا توجد لعبة حاليًا."
        )

        return

    alive = len(
        alive_players(game)
    )

    humans = len(
        human_players(game)
    )

    ai = len([
        p
        for p in game.players.values()
        if not p.is_human
    ])

    await update.effective_message.reply_text(
        "🎭 **حالة اللعبة**\n\n"
        f"👥 اللاعبين: {len(game.players)}\n"
        f"👤 الحقيقيون: {humans}\n"
        f"🤖 AI: {ai}\n"
        f"❤️ الأحياء: {alive}\n"
        f"🌙 المرحلة: {game.phase}\n"
        f"📅 اليوم: {game.day}\n"
        f"🎮 الوضع: "
        f"{'فردي' if game.mode == 'single' else 'جماعي'}",
        parse_mode="Markdown"
    )


# ============================================================
# ❌ Error handler
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# ============================================================
# 🏗️ Build Telegram application
# ============================================================

def build_application():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
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

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# 🌐 Mini App HTML
# ============================================================

MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>لعبة المافيا</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;

    font-family:
        Arial,
        Tahoma,
        sans-serif;

    background:
        radial-gradient(
            circle at top,
            #24213b,
            #0d0d16 65%
        );

    color: white;

    display: flex;
    align-items: center;
    justify-content: center;
}

.container {
    width: 92%;
    max-width: 430px;
}

.card {
    background: rgba(25, 25, 40, 0.95);

    border: 1px solid
        rgba(255,255,255,0.08);

    border-radius: 24px;

    padding: 28px 22px;

    text-align: center;

    box-shadow:
        0 20px 60px
        rgba(0,0,0,0.45);
}

.logo {
    font-size: 64px;
    margin-bottom: 10px;
}

h1 {
    margin: 0 0 10px;
    font-size: 30px;
}

p {
    color: #b9b9c8;
    line-height: 1.7;
}

.button {
    width: 100%;

    border: 0;

    border-radius: 16px;

    padding: 16px;

    margin-top: 12px;

    font-size: 17px;

    font-weight: bold;

    cursor: pointer;

    background:
        linear-gradient(
            135deg,
            #6c5ce7,
            #8e44ad
        );

    color: white;
}

.info {
    margin-top: 18px;

    font-size: 13px;

    color: #858598;
}

</style>

</head>

<body>

<div class="container">

    <div class="card">

        <div class="logo">
            🎭
        </div>

        <h1>
            لعبة المافيا
        </h1>

        <p>
            أهلاً بك في لعبة المافيا.
            <br>
            لإنشاء غرفة واللعب مع الآخرين،
            استخدم أمر <b>/game</b> داخل المجموعة.
        </p>

        <button
            class="button"
            onclick="openTelegram()"
        >
            🎮 العودة إلى Telegram
        </button>

        <div class="info">
            Mafia Game
            <br>
            Telegram Mini App
        </div>

    </div>

</div>

<script>

function openTelegram() {

    if (
        window.Telegram &&
        window.Telegram.WebApp
    ) {

        window.Telegram.WebApp.close();

    }

}

</script>

</body>
</html>
"""


# ============================================================
# 🌐 FastAPI Lifespan
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    global telegram_application

    logger.info(
        "Starting Mafia application..."
    )

    load_games()

    telegram_application = build_application()

    await telegram_application.initialize()

    logger.info(
        "Telegram bot initialized."
    )

    await telegram_application.start()

    logger.info(
        "Telegram bot started."
    )

    await telegram_application.updater.start_polling(
        drop_pending_updates=True
    )

    logger.info(
        "🎭 Telegram polling started."
    )

    try:

        yield

    finally:

        logger.info(
            "Stopping Mafia application..."
        )

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
                    "Error stopping application"
                )

            try:

                await telegram_application.shutdown()

            except Exception:

                logger.exception(
                    "Error shutting down application"
                )

        logger.info(
            "Mafia application stopped."
        )


# ============================================================
# 🚀 FastAPI Application
# ============================================================

web = FastAPI(
    title="Mafia Game",
    description="Telegram Mafia Game",
    version="1.0.0",
    lifespan=lifespan
)


# ============================================================
# 🌐 Home
# ============================================================

@web.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return MINI_APP_HTML


# ============================================================
# ❤️ Health
# ============================================================

@web.get("/health")
async def health():

    return {
        "status": "ok",
        "telegram_bot": (
            telegram_application is not None
        ),
        "webapp_url": WEBAPP_URL,
    }


# ============================================================
# ℹ️ API status
# ============================================================

@web.get("/api/status")
async def api_status():

    return {
        "app": "mafia-game",
        "status": "running",
        "telegram": (
            "running"
            if telegram_application
            else "not_running"
        ),
        "games": len(games),
    }


# ============================================================
# ▶️ Local execution
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        "main:web",
        host="0.0.0.0",
        port=port,
        reload=False
    )