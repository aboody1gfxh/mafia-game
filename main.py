import os
import json
import random
import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "https://mafia-game.fastapicloud.dev"
).strip().rstrip("/")

DATA_FILE = "mafia_games.json"

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = WEBAPP_URL + WEBHOOK_PATH

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN
    else ""
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | mafia-game | %(message)s"
)

logger = logging.getLogger("mafia-game")


# ============================================================
# GLOBALS
# ============================================================

games = {}

http_client: Optional[httpx.AsyncClient] = None


# ============================================================
# STORAGE
# ============================================================

def save_games():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                games,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception:
        logger.exception("Could not save games")


def load_games():
    global games

    if not os.path.exists(DATA_FILE):
        games = {}
        logger.info("No saved games file found.")
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            games = json.load(f)

        logger.info(
            "Loaded %s games.",
            len(games)
        )

    except Exception:
        logger.exception("Could not load games")
        games = {}


# ============================================================
# TELEGRAM API
# ============================================================

async def telegram_api(
    method: str,
    payload: Optional[dict] = None
):
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if http_client is None:
        raise RuntimeError(
            "HTTP client is not ready."
        )

    try:
        response = await http_client.post(
            f"{TELEGRAM_API}/{method}",
            json=payload or {},
            timeout=httpx.Timeout(
                connect=10,
                read=25,
                write=10,
                pool=10
            )
        )

        response.raise_for_status()

        data = response.json()

        return data

    except Exception:
        logger.exception(
            "Telegram API error: %s",
            method
        )
        raise


async def send_message(
    chat_id,
    text,
    reply_markup=None
):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return await telegram_api(
        "sendMessage",
        payload
    )


async def answer_callback(
    callback_id,
    text=None
):
    payload = {
        "callback_query_id": callback_id
    }

    if text:
        payload["text"] = text

    return await telegram_api(
        "answerCallbackQuery",
        payload
    )


# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def bot_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🎮 دخول اللعبة",
                    "web_app": {
                        "url": WEBAPP_URL
                    }
                }
            ],
            [
                {
                    "text": "📖 طريقة اللعب",
                    "callback_data": "help"
                }
            ]
        ]
    }


# ============================================================
# PLAYER / GAME HELPERS
# ============================================================

def new_player(user):

    return {
        "id": int(user["id"]),
        "name": (
            user.get("first_name")
            or user.get("username")
            or "لاعب"
        ),
        "username": user.get(
            "username",
            ""
        ),
        "role": None,
        "alive": True,
        "action": None,
        "vote": None
    }


def find_player(
    game,
    user_id
):

    for player in game["players"]:

        if int(player["id"]) == int(user_id):
            return player

    return None


def alive_players(game):

    return [
        player
        for player in game["players"]
        if player["alive"]
    ]


def generate_room_code():

    while True:

        code = str(
            random.randint(
                100000,
                999999
            )
        )

        if code not in games:
            return code


# ============================================================
# ROLE SYSTEM
# ============================================================

def get_role_counts(number):

    if number == 3:

        return {
            "mafia": 1,
            "detective": 0,
            "doctor": 0,
            "citizen": 2
        }

    if number == 4:

        return {
            "mafia": 1,
            "detective": 1,
            "doctor": 0,
            "citizen": 2
        }

    if number <= 6:

        return {
            "mafia": 1,
            "detective": 1,
            "doctor": 1,
            "citizen": number - 3
        }

    if number <= 9:

        return {
            "mafia": 2,
            "detective": 1,
            "doctor": 1,
            "citizen": number - 4
        }

    mafia = max(
        2,
        number // 4
    )

    return {
        "mafia": mafia,
        "detective": 1,
        "doctor": 1,
        "citizen": number - mafia - 2
    }


def assign_roles(game):

    roles = []

    counts = get_role_counts(
        len(game["players"])
    )

    for role, count in counts.items():

        for _ in range(count):
            roles.append(role)

    random.shuffle(roles)

    for player, role in zip(
        game["players"],
        roles
    ):

        player["role"] = role
        player["alive"] = True
        player["action"] = None
        player["vote"] = None


# ============================================================
# ROLE INFO
# ============================================================

ROLE_NAMES = {
    "mafia": "🔪 المافيا",
    "detective": "🕵️ المحقق",
    "doctor": "🩺 الطبيب",
    "citizen": "👤 المواطن"
}


ROLE_DESCRIPTIONS = {

    "mafia":
        "أنت من المافيا. في الليل اختر لاعباً للقضاء عليه.",

    "detective":
        "أنت المحقق. في الليل اختر لاعباً لمعرفة هل هو من المافيا.",

    "doctor":
        "أنت الطبيب. في الليل اختر لاعباً لحمايته.",

    "citizen":
        "أنت مواطن. ساعد فريق المواطنين باكتشاف المافيا والتصويت عليها."
}


# ============================================================
# WIN SYSTEM
# ============================================================

def get_winner(game):

    alive = alive_players(game)

    mafia_count = sum(
        1
        for p in alive
        if p["role"] == "mafia"
    )

    others = len(alive) - mafia_count

    if mafia_count == 0:
        return "citizens"

    if mafia_count >= others:
        return "mafia"

    return None


# ============================================================
# GAME CREATION
# ============================================================

def create_game(user):

    code = generate_room_code()

    game = {

        "code": code,

        "creator_id": int(
            user["id"]
        ),

        "players": [
            new_player(user)
        ],

        "phase": "waiting",

        "round": 0,

        "winner": None,

        "message": "بانتظار اللاعبين..."

    }

    games[code] = game

    save_games()

    return game


# ============================================================
# PUBLIC GAME
# ============================================================

def public_game(
    game,
    user_id=None
):

    players = []

    for player in game["players"]:

        players.append({

            "id": player["id"],

            "name": player["name"],

            "alive": player["alive"]

        })

    data = {

        "code": game["code"],

        "phase": game["phase"],

        "round": game["round"],

        "winner": game["winner"],

        "message": game["message"],

        "creator_id": game["creator_id"],

        "players": players

    }

    if user_id is not None:

        player = find_player(
            game,
            user_id
        )

        if player:

            data["my_role"] = player["role"]

            data["role_name"] = (
                ROLE_NAMES.get(
                    player["role"],
                    "🎭"
                )
            )

            data["role_description"] = (
                ROLE_DESCRIPTIONS.get(
                    player["role"],
                    ""
                )
            )

            data["alive"] = player["alive"]

            data["my_id"] = player["id"]

            # معلومات خاصة بالمافيا
            if (
                player["role"] == "mafia"
                and player["alive"]
                and game["phase"] == "night"
            ):

                data["mafia_targets"] = [

                    {
                        "id": p["id"],
                        "name": p["name"]
                    }

                    for p in alive_players(game)

                    if (
                        p["id"] != player["id"]
                        and p["alive"]
                    )
                ]

            # المحقق
            if (
                player["role"] == "detective"
                and player["alive"]
                and game["phase"] == "night"
            ):

                data["detective_targets"] = [

                    {
                        "id": p["id"],
                        "name": p["name"]
                    }

                    for p in alive_players(game)

                    if p["id"] != player["id"]
                ]

            # الطبيب
            if (
                player["role"] == "doctor"
                and player["alive"]
                and game["phase"] == "night"
            ):

                data["doctor_targets"] = [

                    {
                        "id": p["id"],
                        "name": p["name"]
                    }

                    for p in alive_players(game)
                ]

            # التصويت
            if (
                game["phase"] == "day"
                and player["alive"]
            ):

                data["vote_targets"] = [

                    {
                        "id": p["id"],
                        "name": p["name"]
                    }

                    for p in alive_players(game)

                    if p["id"] != player["id"]
                ]

    return data


# ============================================================
# NIGHT RESOLUTION
# ============================================================

def night_actions_complete(game):

    for player in alive_players(game):

        if player["role"] in (
            "mafia",
            "detective",
            "doctor"
        ):

            if player["action"] is None:
                return False

    return True


async def resolve_night(game):

    mafia_targets = []

    doctor_target = None

    for player in alive_players(game):

        if (
            player["role"] == "mafia"
            and player["action"] is not None
        ):

            mafia_targets.append(
                player["action"]
            )

        elif (
            player["role"] == "doctor"
            and player["action"] is not None
        ):

            doctor_target = player["action"]

    target_id = None

    if mafia_targets:

        counts = {}

        for target in mafia_targets:

            counts[target] = (
                counts.get(
                    target,
                    0
                ) + 1
            )

        target_id = max(
            counts,
            key=counts.get
        )

    victim = None

    if (
        target_id is not None
        and target_id != doctor_target
    ):

        victim = find_player(
            game,
            target_id
        )

        if victim:

            victim["alive"] = False

    for player in game["players"]:

        player["action"] = None
        player["vote"] = None

    winner = get_winner(game)

    if winner:

        game["winner"] = winner
        game["phase"] = "finished"

        if winner == "mafia":
            game["message"] = (
                "🔪 المافيا فازت!"
            )
        else:
            game["message"] = (
                "🏆 المواطنون فازوا!"
            )

    else:

        game["phase"] = "day"

        if victim:

            game["message"] = (
                f"💀 خلال الليل خرج "
                f"{victim['name']} من اللعبة."
            )

        else:

            game["message"] = (
                "🌙 انتهت الليلة ولم يخرج أي لاعب."
            )

    save_games()


# ============================================================
# VOTE RESOLUTION
# ============================================================

def votes_complete(game):

    for player in alive_players(game):

        if player["vote"] is None:
            return False

    return True


async def resolve_votes(game):

    counts = {}

    for player in alive_players(game):

        target = player["vote"]

        if target is not None:

            counts[target] = (
                counts.get(
                    target,
                    0
                ) + 1
            )

    eliminated = None

    if counts:

        highest = max(
            counts.values()
        )

        candidates = [

            target
            for target, count
            in counts.items()
            if count == highest

        ]

        if len(candidates) == 1:

            eliminated = find_player(
                game,
                candidates[0]
            )

    if eliminated:

        eliminated["alive"] = False

        game["message"] = (
            f"🗳️ تم إخراج "
            f"{eliminated['name']} "
            f"بالتصويت."
        )

    else:

        game["message"] = (
            "⚖️ حدث تعادل، ولم يخرج أي لاعب."
        )

    for player in game["players"]:

        player["vote"] = None

    winner = get_winner(game)

    if winner:

        game["winner"] = winner
        game["phase"] = "finished"

        if winner == "mafia":
            game["message"] = (
                "🔪 المافيا فازت!"
            )
        else:
            game["message"] = (
                "🏆 المواطنون فازوا!"
            )

    else:

        game["round"] += 1
        game["phase"] = "night"

        game["message"] = (
            "🌙 بدأت ليلة جديدة."
        )

    save_games()


# ============================================================
# TELEGRAM /START
# ============================================================

async def handle_message(message):

    text = message.get(
        "text",
        ""
    )

    if not text:
        return

    chat_id = message["chat"]["id"]

    if text.startswith("/start"):

        await send_message(

            chat_id,

            (
                "🎭 <b>مرحباً بك في لعبة المافيا!</b>\n\n"

                "لعبة جماعية تعتمد على "
                "الخداع والتحقيق والتصويت.\n\n"

                "👥 من 3 إلى 20 لاعب\n"
                "🔪 مافيا\n"
                "🕵️ محقق\n"
                "🩺 طبيب\n"
                "👤 مواطنون\n\n"

                "اضغط الزر للدخول إلى اللعبة:"
            ),

            bot_keyboard()
        )

        return

    if text.startswith("/help"):

        await send_message(

            chat_id,

            (
                "📖 <b>طريقة اللعب</b>\n\n"

                "🌙 في الليل:\n"
                "🔪 المافيا تختار ضحية.\n"
                "🕵️ المحقق يحقق في لاعب.\n"
                "🩺 الطبيب يحمي لاعباً.\n\n"

                "☀️ في النهار:\n"
                "يناقش اللاعبون ويصوتون.\n\n"

                "🏆 يفوز المواطنون بإخراج كل المافيا.\n"
                "🔪 تفوز المافيا عندما تصبح مساوية "
                "لعدد بقية اللاعبين."
            ),

            bot_keyboard()
        )


# ============================================================
# TELEGRAM CALLBACK
# ============================================================

async def handle_callback(callback):

    callback_id = callback["id"]

    await answer_callback(
        callback_id
    )

    data = callback.get(
        "data",
        ""
    )

    if data == "help":

        message = callback.get(
            "message"
        )

        if message:

            await send_message(

                message["chat"]["id"],

                (
                    "📖 <b>طريقة اللعب</b>\n\n"
                    "افتح اللعبة، أنشئ غرفة، "
                    "شارك الكود مع أصدقائك، "
                    "ثم ابدأ اللعبة عندما يصل "
                    "عدد اللاعبين إلى 3 على الأقل."
                ),

                bot_keyboard()
            )


# ============================================================
# UPDATE
# ============================================================

async def process_update(update):

    try:

        if "message" in update:

            await handle_message(
                update["message"]
            )

        elif "callback_query" in update:

            await handle_callback(
                update["callback_query"]
            )

    except Exception:

        logger.exception(
            "Update processing error"
        )


# ============================================================
# HTML GAME
# ============================================================

HTML_PAGE = r"""
<!DOCTYPE html>

<html lang="ar" dir="rtl">

<head>

<meta charset="UTF-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"
>

<title>لعبة المافيا</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

*{
box-sizing:border-box;
-webkit-tap-highlight-color:transparent;
}

body{

margin:0;

min-height:100vh;

background:
radial-gradient(
circle at top,
#292c45,
#10111a 48%,
#06070b
);

color:#fff;

font-family:
Arial,
Tahoma,
sans-serif;

}

.app{

width:100%;
max-width:650px;
margin:auto;
padding:16px;

}

.header{

text-align:center;
padding:15px 5px 22px;

}

.logo{

font-size:60px;

}

.title{

font-size:30px;
font-weight:900;

}

.subtitle{

color:#9ea3b8;
margin-top:5px;

}

.card{

background:
rgba(25,27,41,.96);

border:
1px solid rgba(255,255,255,.07);

border-radius:22px;

padding:18px;

margin-bottom:14px;

box-shadow:
0 12px 40px rgba(0,0,0,.28);

}

.title2{

font-size:20px;
font-weight:900;
margin-bottom:14px;

}

input{

width:100%;

background:#10121c;

border:
1px solid #373b50;

border-radius:15px;

padding:16px;

font-size:18px;

color:#fff;

outline:none;

text-align:center;

}

button{

width:100%;

border:0;

border-radius:15px;

padding:16px;

margin-top:10px;

font-size:17px;

font-weight:900;

background:#635bff;

color:#fff;

}

button.green{

background:#198754;

}

button.red{

background:#a62d3d;

}

button.gray{

background:#292d40;

}

.hidden{

display:none !important;

}

.code{

text-align:center;

font-size:38px;

font-weight:900;

letter-spacing:7px;

padding:12px;

}

.player{

display:flex;

justify-content:space-between;

align-items:center;

background:#10121c;

border-radius:14px;

padding:13px;

margin:7px 0;

}

.alive{

color:#54d99a;

}

.dead{

color:#777;

}

.phase{

text-align:center;

font-size:26px;

font-weight:900;

margin-bottom:10px;

}

.message{

text-align:center;

line-height:1.7;

color:#b7bbcc;

}

.role{

text-align:center;

padding:10px;

}

.role-icon{

font-size:70px;

}

.role-name{

font-size:28px;

font-weight:900;

margin:8px;

}

.target{

display:flex;

align-items:center;

justify-content:space-between;

background:#10121c;

border:1px solid #2c3042;

border-radius:14px;

padding:13px;

margin:7px 0;

cursor:pointer;

}

.target:hover{

background:#191c2a;

}

.result{

text-align:center;

padding:25px 10px;

font-size:25px;

font-weight:900;

}

.small{

font-size:13px;

color:#74798d;

text-align:center;

margin-top:15px;

}

</style>

</head>

<body>

<div class="app">

<div class="header">

<div class="logo">🎭</div>

<div class="title">
لعبة المافيا
</div>

<div class="subtitle">
خداع • تحقيق • تصويت
</div>

</div>


<!-- HOME -->

<div id="home">

<div class="card">

<div class="title2">
🏠 إنشاء غرفة
</div>

<div class="message">
أنشئ غرفة وشارك الكود مع أصدقائك.
</div>

<button onclick="createRoom()">
إنشاء غرفة
</button>

</div>


<div class="card">

<div class="title2">
🚪 الانضمام إلى غرفة
</div>

<input
id="roomCode"
maxlength="6"
inputmode="numeric"
placeholder="كود الغرفة"
/>

<button
class="green"
onclick="joinRoom()"
>
انضمام
</button>

</div>

</div>


<!-- ROOM -->

<div id="room" class="hidden">

<div class="card">

<div class="title2">
🎮 الغرفة
</div>

<div
class="code"
id="code"
>
------
</div>

<div
class="message"
id="roomMessage"
>
</div>

</div>


<div class="card">

<div class="title2">
👥 اللاعبين
</div>

<div id="roomPlayers">
</div>

</div>


<div class="card">

<button
class="green"
onclick="startGame()"
>
▶️ بدء اللعبة
</button>

<button
class="gray"
onclick="copyRoomCode()"
>
📋 نسخ كود الغرفة
</button>

<button
class="red"
onclick="leaveRoom()"
>
🚪 مغادرة الغرفة
</button>

</div>

</div>


<!-- GAME -->

<div id="game" class="hidden">

<div class="card">

<div
class="phase"
id="phase"
>
🌙 الليل
</div>

<div
class="message"
id="gameMessage"
>
</div>

</div>


<div class="card">

<div
class="role"
id="roleBox"
>
<div class="role-icon">
🎭
</div>

<div class="role-name">
دورك
</div>

<div class="message">
جاري تحميل الدور...
</div>

</div>

</div>


<div
id="actionBox"
class="card"
>

<div class="title2">
🎯 مهمتك
</div>

<div id="targets">
</div>

</div>


<div class="card">

<div class="title2">
👥 اللاعبون
</div>

<div id="gamePlayers">
</div>

</div>


<div
id="resultBox"
class="card hidden"
>

<div
class="result"
id="result"
>
</div>

<button
class="green"
onclick="backHome()"
>
🔄 العودة
</button>

</div>

</div>


<div class="small">
Mafia Game
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


if(tg){

tg.ready();
tg.expand();

}


let currentRoom = null;

let timer = null;


function user(){

if(
tg &&
tg.initDataUnsafe &&
tg.initDataUnsafe.user
){

return tg.initDataUnsafe.user;

}

return {

id:
"demo_" +
Math.floor(
Math.random()*999999
),

first_name:"لاعب"

};

}


function show(id){

[
"home",
"room",
"game"
].forEach(x=>{

document
.getElementById(x)
.classList.add("hidden");

});

document
.getElementById(id)
.classList.remove("hidden");

}


async function api(
url,
options={}
){

const res =
await fetch(
url,
{
headers:{
"Content-Type":
"application/json"
},
...options
}
);

return await res.json();

}


async function createRoom(){

const data =
await api(
"/api/game/create",
{
method:"POST",

body:JSON.stringify({
user:user()
})

}
);

if(!data.ok){

alert(data.message);
return;

}

currentRoom =
data.game.code;

renderRoom(
data.game
);

startPolling();

}


async function joinRoom(){

const code =
document
.getElementById(
"roomCode"
)
.value
.trim();

if(!/^[0-9]{6}$/.test(code)){

alert(
"أدخل كوداً من 6 أرقام."
);

return;

}

const data =
await api(
"/api/game/join",
{
method:"POST",

body:JSON.stringify({
code:code,
user:user()
})

}
);

if(!data.ok){

alert(data.message);
return;

}

currentRoom =
code;

renderRoom(
data.game
);

startPolling();

}


function renderRoom(game){

show("room");

document
.getElementById("code")
.textContent =
game.code;

document
.getElementById(
"roomMessage"
)
.textContent =
game.message;

const box =
document
.getElementById(
"roomPlayers"
);

box.innerHTML="";

game.players.forEach(
p=>{

const div =
document.createElement(
"div"
);

div.className="player";

div.innerHTML=`

<div>
${escapeHtml(p.name)}
</div>

<div class="${
p.alive
?
"alive"
:
"dead"
}">
${
p.alive
?
"🟢"
:
"⚫"
}
</div>

`;

box.appendChild(div);

}
);

}


async function startGame(){

const data =
await api(
"/api/game/start",
{
method:"POST",

body:JSON.stringify({

code:currentRoom,

user:user()

})

}
);

if(!data.ok){

alert(data.message);
return;

}

renderGame(
data.game
);

}


async function leaveRoom(){

if(!currentRoom)
return;

const data =
await api(
"/api/game/leave",
{
method:"POST",

body:JSON.stringify({

code:currentRoom,

user:user()

})

}
);

if(data.ok){

backHome();

}else{

alert(
data.message ||
"تعذر المغادرة"
);

}

}


async function copyRoomCode(){

if(!currentRoom)
return;

try{

await navigator
.clipboard
.writeText(
currentRoom
);

alert(
"تم نسخ الكود."
);

}catch{

alert(
"كود الغرفة: "+
currentRoom
);

}

}


function renderGame(game){

show("game");

let phase =
"🌙 الليل";

if(game.phase==="day")
phase="☀️ النهار";

if(game.phase==="finished")
phase="🏆 انتهت اللعبة";

document
.getElementById("phase")
.textContent =
phase;

document
.getElementById(
"gameMessage"
)
.textContent =
game.message || "";

renderPlayers(
game.players
);

renderRole(
game
);

renderTargets(
game
);

if(game.phase==="finished"){

document
.getElementById(
"resultBox"
)
.classList
.remove("hidden");

const result =
document
.getElementById(
"result"
);

if(game.winner==="mafia"){

result.textContent =
"🔪 المافيا فازت!";

}else{

result.textContent =
"🏆 المواطنون فازوا!";

}

}else{

document
.getElementById(
"resultBox"
)
.classList
.add("hidden");

}

}


function renderPlayers(players){

const box =
document
.getElementById(
"gamePlayers"
);

box.innerHTML="";

players.forEach(
p=>{

const div =
document.createElement(
"div"
);

div.className="player";

div.innerHTML=`

<div>
${escapeHtml(p.name)}
</div>

<div class="${
p.alive
?
"alive"
:
"dead"
}">
${
p.alive
?
"🟢 حي"
:
"⚫ خرج"
}
</div>

`;

box.appendChild(div);

}
);

}


function renderRole(game){

const box =
document
.getElementById(
"roleBox"
);

if(!game.my_role){

box.innerHTML=`

<div class="role-icon">
🎭
</div>

<div class="role-name">
الدور مخفي
</div>

`;

return;

}

const icons={

mafia:"🔪",
detective:"🕵️",
doctor:"🩺",
citizen:"👤"

};

box.innerHTML=`

<div class="role-icon">
${icons[game.my_role] || "🎭"}
</div>

<div class="role-name">
${escapeHtml(
game.role_name
)}
</div>

<div class="message">
${escapeHtml(
game.role_description
)}
</div>

`;

}


function renderTargets(game){

const box =
document
.getElementById(
"targets"
);

box.innerHTML="";


if(!game.alive){

box.innerHTML=`

<div class="message">
💀 أنت خارج اللعبة.
</div>

`;

return;

}


let targets=[];

let action="";


if(
game.phase==="night"
){

if(game.my_role==="mafia"){

targets =
game.mafia_targets ||
[];

action="kill";

}

else if(
game.my_role==="detective"
){

targets =
game.detective_targets ||
[];

action="check";

}

else if(
game.my_role==="doctor"
){

targets =
game.doctor_targets ||
[];

action="heal";

}

}


if(
game.phase==="day"
){

targets =
game.vote_targets ||
[];

action="vote";

}


if(
game.phase==="finished"
){

box.innerHTML=`

<div class="message">
🏁 انتهت اللعبة.
</div>

`;

return;

}


if(
targets.length===0
){

box.innerHTML=`

<div class="message">
⏳ لا توجد مهمة متاحة لك الآن.
</div>

`;

return;

}


targets.forEach(
target=>{

const div =
document.createElement(
"div"
);

div.className="target";

div.innerHTML=`

<div>
👤
${escapeHtml(
target.name
)}
</div>

<div>
➜
</div>

`;

div.onclick=()=>{

doAction(
action,
target.id
);

};

box.appendChild(div);

}
);

}


async function doAction(
action,
targetId
){

if(!currentRoom)
return;

const data =
await api(
"/api/game/action",
{
method:"POST",

body:JSON.stringify({

code:currentRoom,

user_id:user().id,

action:action,

target_id:targetId

})

}
);

if(!data.ok){

alert(
data.message ||
"تعذر تنفيذ العملية"
);

return;

}

renderGame(
data.game
);

}


async function loadGame(){

if(!currentRoom)
return;

const data =
await api(

"/api/game/" +
encodeURIComponent(
currentRoom
) +
"?user_id=" +
encodeURIComponent(
user().id
)

);

if(!data.ok)
return;

if(
data.game.phase==="waiting"
){

renderRoom(
data.game
);

}else{

renderGame(
data.game
);

}

}


function startPolling(){

if(timer)
clearInterval(timer);

timer =
setInterval(
loadGame,
1500
);

loadGame();

}


function backHome(){

currentRoom=null;

if(timer)
clearInterval(timer);

show("home");

}


function escapeHtml(value){

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


const params =
new URLSearchParams(
location.search
);

const room =
params.get("room");

if(room){

currentRoom=room;

startPolling();

}

</script>

</body>

</html>
"""


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app):

    global http_client

    logger.info(
        "🚀 Starting Mafia FastAPI application..."
    )

    load_games()

    http_client = httpx.AsyncClient()

    logger.info(
        "🌐 FastAPI started without Telegram polling."
    )

    yield

    logger.info(
        "🛑 Shutting down Mafia application..."
    )

    if http_client:

        await http_client.aclose()

    logger.info(
        "✅ Mafia application stopped."
    )


# ============================================================
# FASTAPI APP
# ============================================================

web = FastAPI(
    title="Mafia Game",
    version="3.0.0",
    lifespan=lifespan
)


# ============================================================
# BASIC ROUTES
# ============================================================

@web.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return HTML_PAGE


@web.get("/health")
async def health():

    return {
        "ok": True,
        "service": "mafia-game"
    }


@web.get("/api/status")
async def status():

    return {

        "ok": True,

        "games": len(games),

        "webapp_url": WEBAPP_URL

    }


# ============================================================
# CREATE GAME
# ============================================================

@web.post("/api/game/create")
async def api_create_game(
    request: Request
):

    try:

        body =
        await request.json()

    except Exception:

        return {
            "ok": False,
            "message": "بيانات غير صالحة."
        }

    user = body.get(
        "user"
    )

    if not user or "id" not in user:

        return {
            "ok": False,
            "message": "لم يتم التعرف على اللاعب."
        }

    game =
    create_game(user)

    return {

        "ok": True,

        "game":
        public_game(
            game,
            user["id"]
        )

    }


# ============================================================
# JOIN GAME
# ============================================================

@web.post("/api/game/join")
async def api_join_game(
    request: Request
):

    try:

        body =
        await request.json()

    except Exception:

        return {
            "ok": False,
            "message": "بيانات غير صالحة."
        }

    code =
    str(
        body.get(
            "code",
            ""
        )
    ).strip()

    user =
    body.get(
        "user"
    )

    game =
    games.get(code)

    if not game:

        return {
            "ok": False,
            "message": "الغرفة غير موجودة."
        }

    if not user:

        return {
            "ok": False,
            "message": "بيانات اللاعب غير موجودة."
        }

    if game["phase"] != "waiting":

        return {
            "ok": False,
            "message": "اللعبة بدأت بالفعل."
        }

    if len(
        game["players"]
    ) >= 20:

        return {
            "ok": False,
            "message": "الغرفة ممتلئة."
        }

    existing =
    find_player(
        game,
        user["id"]
    )

    if not existing:

        game["players"].append(
            new_player(user)
        )

        save_games()

    return {

        "ok": True,

        "game":
        public_game(
            game,
            user["id"]
        )

    }


# ============================================================
# LEAVE GAME
# ============================================================

@web.post("/api/game/leave")
async def api_leave_game(
    request: Request
):

    body =
    await request.json()

    code =
    str(
        body.get(
            "code",
            ""
        )
    )

    user =
    body.get(
        "user"
    )

    game =
    games.get(code)

    if not game:

        return {
            "ok": False,
            "message": "الغرفة غير موجودة."
        }

    if game["phase"] != "waiting":

        return {
            "ok": False,
            "message": "لا يمكنك المغادرة بعد بدء اللعبة."
        }

    user_id =
    int(
        user["id"]
    )

    game["players"] = [

        p
        for p in game["players"]

        if int(p["id"]) != user_id

    ]

    if not game["players"]:

        del games[code]

    elif (
        int(
            game["creator_id"]
        ) == user_id
    ):

        game["creator_id"] =
        int(
            game["players"][0]["id"]
        )

    save_games()

    return {
        "ok": True
    }


# ============================================================
# START GAME
# ============================================================

@web.post("/api/game/start")
async def api_start_game(
    request: Request
):

    body =
    await request.json()

    code =
    str(
        body.get(
            "code",
            ""
        )
    )

    user =
    body.get(
        "user"
    )

    game =
    games.get(code)

    if not game:

        return {
            "ok": False,
            "message": "الغرفة غير موجودة."
        }

    if not user:

        return {
            "ok": False,
            "message": "بيانات اللاعب غير موجودة."
        }

    if int(
        game["creator_id"]
    ) != int(
        user["id"]
    ):

        return {
            "ok": False,
            "message": "فقط منشئ الغرفة يستطيع بدء اللعبة."
        }

    if len(
        game["players"]
    ) < 3:

        return {
            "ok": False,
            "message": "تحتاج اللعبة إلى 3 لاعبين على الأقل."
        }

    if game["phase"] != "waiting":

        return {

            "ok": True,

            "game":
            public_game(
                game,
                user["id"]
            )

        }

    assign_roles(game)

    game["phase"] =
    "night"

    game["round"] =
    1

    game["message"] =
    "🌙 بدأت الليلة الأولى."

    save_games()

    return {

        "ok": True,

        "game":
        public_game(
            game,
            user["id"]
        )

    }


# ============================================================
# GAME ACTION
# ============================================================

@web.post("/api/game/action")
async def api_action(
    request: Request
):

    body =
    await request.json()

    code =
    str(
        body.get(
            "code",
            ""
        )
    )

    user_id =
    int(
        body.get(
            "user_id"
        )
    )

    action =
    body.get(
        "action"
    )

    target_id =
    int(
        body.get(
            "target_id"
        )
    )

    game =
    games.get(code)

    if not game:

        return {
            "ok": False,
            "message": "الغرفة غير موجودة."
        }

    player =
    find_player(
        game,
        user_id
    )

    if not player:

        return {
            "ok": False,
            "message": "أنت لست في هذه اللعبة."
        }

    if not player["alive"]:

        return {
            "ok": False,
            "message": "أنت خارج اللعبة."
        }

    target =
    find_player(
        game,
        target_id
    )

    if not target:

        return {
            "ok": False,
            "message": "الهدف غير موجود."
        }

    if not target["alive"]:

        return {
            "ok": False,
            "message": "هذا اللاعب خرج من اللعبة."
        }


    # -------------------------
    # NIGHT
    # -------------------------

    if game["phase"] == "night":

        if action == "kill":

            if player["role"] != "mafia":

                return {
                    "ok": False,
                    "message": "هذا الإجراء للمافيا فقط."
                }

            if target_id == user_id:

                return {
                    "ok": False,
                    "message": "لا يمكنك اختيار نفسك."
                }

            player["action"] =
            target_id

            game["message"] =
            "🔪 تم تسجيل اختيار المافيا."

        elif action == "check":

            if player["role"] != "detective":

                return {
                    "ok": False,
                    "message": "هذا الإجراء للمحقق فقط."
                }

            if target_id == user_id:

                return {
                    "ok": False,
                    "message": "لا يمكنك التحقيق بنفسك."
                }

            player["action"] =
            target_id

            if target["role"] == "mafia":

                game["message"] =
                "🕵️ نتيجة التحقيق: هذا اللاعب من المافيا."

            else:

                game["message"] =
                "🕵️ نتيجة التحقيق: هذا اللاعب ليس من المافيا."

        elif action == "heal":

            if player["role"] != "doctor":

                return {
                    "ok": False,
                    "message": "هذا الإجراء للطبيب فقط."
                }

            player["action"] =
            target_id

            game["message"] =
            "🩺 تم تسجيل الحماية."

        else:

            return {
                "ok": False,
                "message": "إجراء غير صالح."
            }


        if night_actions_complete(game):

            await resolve_night(game)


    # -------------------------
    # DAY
    # -------------------------

    elif game["phase"] == "day":

        if action != "vote":

            return {
                "ok": False,
                "message": "الآن وقت التصويت."
            }

        if target_id == user_id:

            return {
                "ok": False,
                "message": "لا يمكنك التصويت لنفسك."
            }

        player["vote"] =
        target_id

        game["message"] =
        "🗳️ تم تسجيل تصويتك."

        if votes_complete(game):

            await resolve_votes(game)


    else:

        return {
            "ok": False,
            "message": "لا يوجد إجراء متاح الآن."
        }


    save_games()

    return {

        "ok": True,

        "game":
        public_game(
            game,
            user_id
        )

    }


# ============================================================
# GET GAME
# ============================================================

@web.get(
    "/api/game/{code}"
)
async def api_get_game(
    code: str,
    user_id: Optional[str] = None
):

    game =
    games.get(code)

    if not game:

        return {
            "ok": False,
            "message": "الغرفة غير موجودة."
        }

    uid =
    int(user_id)
    if user_id
    else None

    return {

        "ok": True,

        "game":
        public_game(
            game,
            uid
        )

    }


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@web.post(
    WEBHOOK_PATH
)
async def telegram_webhook(
    request: Request
):

    try:

        update =
        await request.json()

        asyncio.create_task(
            process_update(
                update
            )
        )

        return {
            "ok": True
        }

    except Exception:

        logger.exception(
            "Webhook error"
        )

        return {
            "ok": False
        }


# ============================================================
# TELEGRAM TEST
# ============================================================

@web.get(
    "/telegram-test"
)
async def telegram_test():

    try:

        result =
        await telegram_api(
            "getMe"
        )

        return {

            "ok":
            result.get(
                "ok",
                False
            ),

            "telegram":
            result.get(
                "result"
            )

        }

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "ok": False,

                "error": str(e)

            }

        )


# ============================================================
# SET WEBHOOK
# ============================================================

@web.get(
    "/setup-webhook"
)
async def setup_webhook():

    try:

        result =
        await telegram_api(

            "setWebhook",

            {

                "url":
                WEBHOOK_URL,

                "allowed_updates": [

                    "message",

                    "callback_query"

                ],

                "drop_pending_updates":
                True

            }

        )

        return {

            "ok": True,

            "message":
            "تمت تهيئة Webhook بنجاح.",

            "webhook_url":
            WEBHOOK_URL,

            "telegram_result":
            result.get(
                "ok",
                False
            )

        }

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "ok": False,

                "error": str(e)

            }

        )


# ============================================================
# WEBHOOK INFO
# ============================================================

@web.get(
    "/webhook-info"
)
async def webhook_info():

    try:

        result =
        await telegram_api(
            "getWebhookInfo"
        )

        return {

            "ok": True,

            "telegram":
            result.get(
                "result"
            )

        }

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "ok": False,

                "error": str(e)

            }

        )


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
        ),

        reload=False

    )