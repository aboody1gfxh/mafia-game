import os, json, random, string, asyncio, logging, sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

BOT_TOKEN=os.getenv('BOT_TOKEN','').strip()
WEBAPP_URL=os.getenv('WEBAPP_URL','https://mafia-game.fastapicloud.dev').rstrip('/')
WEBHOOK_PATH='/telegram/webhook'; WEBHOOK_URL=WEBAPP_URL+WEBHOOK_PATH
DATABASE_FILE='mafia.db'; MIN_PLAYERS=3; MAX_PLAYERS=20
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger=logging.getLogger('mafia-game')
db_lock=asyncio.Lock()

def db_connect():
    c=sqlite3.connect(DATABASE_FILE,check_same_thread=False); c.row_factory=sqlite3.Row; return c

def now(): return datetime.now(timezone.utc).isoformat()

def db_execute(q,p=(),fetch=False,many=False):
    c=db_connect(); cur=c.cursor(); cur.executemany(q,p) if many else cur.execute(q,p)
    r=cur.fetchall() if fetch else None; c.commit(); c.close(); return r

def db_one(q,p=()):
    c=db_connect(); cur=c.cursor(); cur.execute(q,p); r=cur.fetchone(); c.close(); return r

def make_public_id():
    while True:
        x='MAF-'+''.join(random.choice(string.digits) for _ in range(6))
        if not db_one('SELECT user_id FROM profiles WHERE public_id=?',(x,)): return x

def init_database():
    c=db_connect(); cur=c.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY,name TEXT NOT NULL,host_id TEXT NOT NULL,max_players INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'waiting',phase TEXT NOT NULL DEFAULT 'waiting',created_at TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY AUTOINCREMENT,room_code TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,role TEXT,alive INTEGER NOT NULL DEFAULT 1,is_ai INTEGER NOT NULL DEFAULT 0,night_action TEXT,vote TEXT,private_message TEXT DEFAULT '',UNIQUE(room_code,user_id))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,room_code TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS profiles(user_id TEXT PRIMARY KEY,public_id TEXT UNIQUE NOT NULL,name TEXT NOT NULL,xp INTEGER NOT NULL DEFAULT 0,coins INTEGER NOT NULL DEFAULT 0,skin TEXT NOT NULL DEFAULT 'classic',created_at TEXT NOT NULL,last_seen TEXT NOT NULL,online INTEGER NOT NULL DEFAULT 1)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS friend_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,sender_id TEXT NOT NULL,receiver_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,UNIQUE(sender_id,receiver_id))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS friendships(user_a TEXT NOT NULL,user_b TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_a,user_b))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS private_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,sender_id TEXT NOT NULL,receiver_id TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL,seen INTEGER NOT NULL DEFAULT 0)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS match_results(id INTEGER PRIMARY KEY AUTOINCREMENT,room_code TEXT NOT NULL,winner TEXT NOT NULL,finished_at TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS daily_tasks(user_id TEXT PRIMARY KEY,task_key TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,target INTEGER NOT NULL DEFAULT 1,reward_xp INTEGER NOT NULL DEFAULT 50,reward_coins INTEGER NOT NULL DEFAULT 10,day TEXT NOT NULL,claimed INTEGER NOT NULL DEFAULT 0)''')
    c.commit(); c.close(); logger.info('SQLite database initialized.')

def ensure_profile(user_id,name='لاعب'):
    uid=str(user_id); name=clean_name(name); p=db_one('SELECT * FROM profiles WHERE user_id=?',(uid,))
    if p:
        db_execute('UPDATE profiles SET name=?,last_seen=?,online=1 WHERE user_id=?',(name,now(),uid)); return db_one('SELECT * FROM profiles WHERE user_id=?',(uid,))
    pid=make_public_id(); db_execute('INSERT INTO profiles(user_id,public_id,name,created_at,last_seen,online) VALUES(?,?,?,?,?,1)',(uid,pid,name,now(),now())); reset_daily_task(uid); return db_one('SELECT * FROM profiles WHERE user_id=?',(uid,))

def touch_presence(uid,name=None,online=1):
    p=ensure_profile(uid,name or 'لاعب'); db_execute('UPDATE profiles SET last_seen=?,online=? WHERE user_id=?',(now(),int(online),str(uid))); return p

def clean_name(x): return str(x or '').strip()[:30] or 'لاعب'
def clean_text(x): return str(x or '').strip()[:500]
def role_name(r): return {'mafia':'🔴 المافيا','detective':'🔎 المحقق','doctor':'💉 الطبيب','citizen':'👤 المواطن'}.get(r,'👤 لاعب')
def phase_name(p): return {'waiting':'🏠 انتظار اللاعبين','night':'🌙 الليل','day':'☀️ النهار','vote':'🗳️ التصويت','finished':'🏆 انتهت اللعبة'}.get(p,p)

def reset_daily_task(uid):
    day=datetime.now(timezone.utc).date().isoformat(); p=db_one('SELECT day FROM daily_tasks WHERE user_id=?',(uid,))
    if not p or p['day']!=day:
        db_execute('INSERT OR REPLACE INTO daily_tasks(user_id,task_key,progress,target,reward_xp,reward_coins,day,claimed) VALUES(?,?,?,?,?,?,?,0)',(uid,'play_match',0,1,50,10,day))

def task_progress(uid):
    reset_daily_task(uid); return db_one('SELECT * FROM daily_tasks WHERE user_id=?',(uid,))

def add_reward(uid,xp=0,coins=0):
    db_execute('UPDATE profiles SET xp=xp+?,coins=coins+? WHERE user_id=?',(xp,coins,str(uid)))

def public_profile(uid):
    p=db_one('SELECT * FROM profiles WHERE user_id=?',(str(uid),));
    if not p:return None
    rp=db_one("SELECT r.code,r.name,r.status FROM rooms r JOIN players pl ON pl.room_code=r.code WHERE pl.user_id=? AND r.status IN ('waiting','playing','finished') LIMIT 1",(str(uid),))
    return {'user_id':str(p['user_id']),'public_id':p['public_id'],'name':p['name'],'xp':p['xp'],'coins':p['coins'],'skin':p['skin'],'online':bool(p['online']),'last_seen':p['last_seen'],'room_code':rp['code'] if rp else None,'room_name':rp['name'] if rp else None,'room_status':rp['status'] if rp else None}

def profile_by_public_id(pid): return db_one('SELECT * FROM profiles WHERE public_id=?',(str(pid).upper().strip(),))

def friend_pair(a,b): return tuple(sorted((str(a),str(b))))
def are_friends(a,b):
    a,b=friend_pair(a,b); return db_one('SELECT 1 FROM friendships WHERE user_a=? AND user_b=?',(a,b)) is not None

def friend_list(uid):
    rows=db_execute('''SELECT p.* FROM profiles p JOIN friendships f ON (f.user_a=p.user_id OR f.user_b=p.user_id) WHERE (f.user_a=? OR f.user_b=?) AND p.user_id!=? ORDER BY p.name''',(uid,uid,uid),fetch=True)
    return [public_profile(r['user_id']) for r in rows]

def pending_requests(uid):
    rows=db_execute('''SELECT f.id,p.public_id,p.name,p.user_id FROM friend_requests f JOIN profiles p ON p.user_id=f.sender_id WHERE f.receiver_id=? AND f.status='pending' ORDER BY f.id DESC''',(uid,),fetch=True)
    return [{'id':r['id'],'user_id':r['user_id'],'public_id':r['public_id'],'name':r['name']} for r in rows]

def send_friend_request(sender,public_id):
    target=profile_by_public_id(public_id)
    if not target:return False,'الـID غير موجود.'
    receiver=target['user_id']; sender=str(sender)
    if receiver==sender:return False,'لا يمكنك إضافة نفسك.'
    if are_friends(sender,receiver):return False,'أنتم أصدقاء بالفعل.'
    old=db_one('SELECT * FROM friend_requests WHERE sender_id=? AND receiver_id=? AND status=?',(sender,receiver,'pending'))
    if old:return False,'طلب الصداقة موجود بالفعل.'
    reverse=db_one('SELECT * FROM friend_requests WHERE sender_id=? AND receiver_id=? AND status=?',(receiver,sender,'pending'))
    if reverse:
        accept_friend(reverse['id'],receiver); return True,'تم قبول الطلب الموجود.'
    db_execute('INSERT INTO friend_requests(sender_id,receiver_id,status,created_at) VALUES(?,?,?,?)',(sender,receiver,'pending',now())); return True,'تم إرسال طلب الصداقة.'

def accept_friend(req_id,uid):
    r=db_one('SELECT * FROM friend_requests WHERE id=? AND receiver_id=? AND status=?',(req_id,uid,'pending'))
    if not r:return False,'الطلب غير موجود.'
    a,b=friend_pair(r['sender_id'],r['receiver_id']); db_execute('INSERT OR IGNORE INTO friendships(user_a,user_b,created_at) VALUES(?,?,?)',(a,b,now())); db_execute('UPDATE friend_requests SET status=? WHERE id=?',('accepted',req_id)); return True,'تمت إضافة الصديق.'

def remove_friend(uid,target):
    a,b=friend_pair(uid,target); db_execute('DELETE FROM friendships WHERE user_a=? AND user_b=?',(a,b)); return True

def private_send(sender,receiver,text):
    text=clean_text(text)
    if not are_friends(sender,receiver): return False,'المراسلة الخاصة متاحة للأصدقاء فقط.'
    if not text:return False,'الرسالة فارغة.'
    db_execute('INSERT INTO private_messages(sender_id,receiver_id,text,created_at) VALUES(?,?,?,?)',(str(sender),str(receiver),text,now())); return True,'تم الإرسال.'

def private_conversation(a,b):
    rows=db_execute('''SELECT * FROM private_messages WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?) ORDER BY id DESC LIMIT 100''',(a,b,b,a),fetch=True)
    return [{'id':r['id'],'sender_id':str(r['sender_id']),'receiver_id':str(r['receiver_id']),'text':r['text'],'created_at':r['created_at']} for r in reversed(rows)]

def create_room_code():
    chars=string.ascii_uppercase+string.digits
    while True:
        x=''.join(random.choice(chars) for _ in range(5))
        if not db_one('SELECT code FROM rooms WHERE code=?',(x,)):return x

def get_room(code):return db_one('SELECT * FROM rooms WHERE code=?',(code,))
def get_players(code):return db_execute('SELECT * FROM players WHERE room_code=? ORDER BY id',(code,),fetch=True)
def get_player(code,uid):return db_one('SELECT * FROM players WHERE room_code=? AND user_id=?',(code,str(uid)))
def get_alive_players(code):return db_execute('SELECT * FROM players WHERE room_code=? AND alive=1 ORDER BY id',(code,),fetch=True)

def public_rooms():
    rows=db_execute('''SELECT r.*,COUNT(p.id) player_count FROM rooms r LEFT JOIN players p ON p.room_code=r.code WHERE r.status='waiting' GROUP BY r.code ORDER BY r.created_at DESC''',fetch=True)
    return [{'code':r['code'],'name':r['name'],'host_id':str(r['host_id']),'players':r['player_count'],'max_players':r['max_players'],'status':r['status'],'phase':r['phase']} for r in rows if r['player_count']<r['max_players']]

def distribute_roles(n):
    mafia=max(1,round(n/4)); detective=1 if n>=4 else 0; doctor=1 if n>=5 else 0; citizen=n-mafia-detective-doctor
    while citizen<1:
        if mafia>1:mafia-=1
        elif doctor:doctor=0
        elif detective:detective=0
        else:break
        citizen=n-mafia-detective-doctor
    roles=['mafia']*mafia+['detective']*detective+['doctor']*doctor+['citizen']*max(1,citizen); roles=roles[:n]
    while len(roles)<n:roles.append('citizen')
    random.shuffle(roles); return roles

async def create_room(uid,name,room_name,max_players):
    ensure_profile(uid,name); max_players=max(MIN_PLAYERS,min(MAX_PLAYERS,int(max_players))); code=create_room_code()
    db_execute('INSERT INTO rooms(code,name,host_id,max_players,status,phase,created_at) VALUES(?,?,?,?,?,?,?)',(code,clean_name(room_name),str(uid),max_players,'waiting','waiting',now()))
    db_execute('INSERT INTO players(room_code,user_id,name,alive,is_ai) VALUES(?,?,?,?,0)',(code,str(uid),clean_name(name),1)); return code

async def join_room(code,uid,name):
    code=code.upper().strip(); room=get_room(code)
    if not room:return False,'الغرفة غير موجودة.'
    ensure_profile(uid,name)
    if get_player(code,uid):return True,'أنت داخل الغرفة بالفعل.'
    if room['status']!='waiting':return False,'اللعبة بدأت بالفعل.'
    if len(get_players(code))>=room['max_players']:return False,'الغرفة ممتلئة.'
    db_execute('INSERT INTO players(room_code,user_id,name,alive,is_ai) VALUES(?,?,?,?,0)',(code,str(uid),clean_name(name),1)); await manager.broadcast(code,{'type':'room_update'}); return True,'تم الانضمام.'

async def leave_room(code,uid):
    room=get_room(code)
    if not room:return False,'الغرفة غير موجودة.'
    p=get_player(code,uid)
    if not p:return False,'أنت لست داخل الغرفة.'
    if room['status']=='playing':return False,'لا يمكنك مغادرة الغرفة أثناء المباراة.'
    db_execute('DELETE FROM players WHERE room_code=? AND user_id=?',(code,str(uid)))
    remaining=get_players(code)
    if not remaining:
        db_execute('DELETE FROM rooms WHERE code=?',(code,)); db_execute('DELETE FROM messages WHERE room_code=?',(code,)); return True,'تم حذف الغرفة.'
    if str(room['host_id'])==str(uid):db_execute('UPDATE rooms SET host_id=? WHERE code=?',(remaining[0]['user_id'],code))
    await manager.broadcast(code,{'type':'room_update'}); return True,'تمت المغادرة.'

async def start_online_game(code,uid):
    room=get_room(code)
    if not room:return False,'الغرفة غير موجودة.'
    if str(room['host_id'])!=str(uid):return False,'فقط صاحب الغرفة يستطيع بدء اللعبة.'
    if room['status']=='finished': return await rematch(code,uid)
    players=get_players(code)
    if len(players)<MIN_PLAYERS:return False,'تحتاج إلى 3 لاعبين على الأقل.'
    roles=distribute_roles(len(players))
    for p,r in zip(players,roles):db_execute('UPDATE players SET role=?,alive=1,night_action=NULL,vote=NULL,private_message=? WHERE id=?',(r,'',p['id']))
    db_execute("UPDATE rooms SET status='playing',phase='night' WHERE code=?",(code,)); await manager.broadcast(code,{'type':'game_started'}); return True,'بدأت اللعبة.'

async def rematch(code,uid):
    room=get_room(code)
    if not room:return False,'الغرفة غير موجودة.'
    if room['status']!='finished':return False,'المباراة لم تنتهِ بعد.'
    if str(room['host_id'])!=str(uid):return False,'فقط صاحب الغرفة يستطيع بدء مباراة جديدة.'
    players=get_players(code)
    if len(players)<MIN_PLAYERS:return False,'تحتاج إلى 3 لاعبين.'
    roles=distribute_roles(len(players))
    for p,r in zip(players,roles):db_execute('UPDATE players SET role=?,alive=1,night_action=NULL,vote=NULL,private_message=? WHERE id=?',(r,'',p['id']))
    db_execute("UPDATE rooms SET status='playing',phase='night' WHERE code=?",(code,)); await manager.broadcast(code,{'type':'game_started'}); return True,'بدأت مباراة جديدة.'

async def create_single_player_game(uid,name,count):
    ensure_profile(uid,name); count=max(3,min(20,int(count))); code=create_room_code()
    db_execute('INSERT INTO rooms(code,name,host_id,max_players,status,phase,created_at) VALUES(?,?,?,?,?,?,?)',(code,'🤖 لعب فردي',str(uid),count,'playing','night',now()))
    db_execute('INSERT INTO players(room_code,user_id,name,alive,is_ai) VALUES(?,?,?,?,0)',(code,str(uid),clean_name(name),1))
    names=[clean_name(name)]; base=['سالم','كرار','علي','محمد','حسين','زهراء','نور','مصطفى','عباس','سجاد','مريم','ياسين','حيدر','فاطمة','مرتضى','آدم','ليان','عمر','زينب','رؤى']
    for i in range(count-1):
        avail=[n for n in base if n not in names]; n=random.choice(avail) if avail else f'AI-{random.randint(100,999)}'; names.append(n); db_execute('INSERT INTO players(room_code,user_id,name,alive,is_ai) VALUES(?,?,?,?,1)',(code,f'ai_{code}_{i}',n,1))
    ps=get_players(code); roles=distribute_roles(len(ps))
    for p,r in zip(ps,roles):db_execute('UPDATE players SET role=? WHERE id=?',(r,p['id']))
    return code

def ai_choose_target(code,p):
    candidates=[x for x in get_alive_players(code) if x['id']!=p['id']]
    if p['role']=='mafia':
        nm=[x for x in candidates if x['role']!='mafia']; candidates=nm or candidates
    return random.choice(candidates) if candidates else None

def ai_night_actions(code):
    for p in get_alive_players(code):
        if not p['is_ai']:continue
        t=ai_choose_target(code,p)
        if not t:continue
        if p['role'] in ('mafia','doctor'):db_execute('UPDATE players SET night_action=? WHERE id=?',(t['user_id'],p['id']))
        elif p['role']=='detective':
            result=f"🔎 نتيجة التحقيق: {t['name']} {'هو مافيا 🔴' if t['role']=='mafia' else 'ليس مافيا 🟢'}"; db_execute('UPDATE players SET night_action=?,private_message=? WHERE id=?',(t['user_id'],result,p['id']))

async def resolve_night(code):
    ps=get_alive_players(code); mafia=[p for p in ps if p['role']=='mafia']; doctors=[p for p in ps if p['role']=='doctor']; votes=[p['night_action'] for p in mafia if p['night_action']]; target=max(set(votes),key=votes.count) if votes else None; healed={p['night_action'] for p in doctors if p['night_action']}; msg='☀️ انتهى الليل.'
    if target:
        t=get_player(code,target)
        if t:
            if target in healed:msg=f"💉 الطبيب أنقذ {t['name']} هذه الليلة!"
            else:db_execute('UPDATE players SET alive=0 WHERE id=?',(t['id'],)); msg=f"💀 في الصباح، اكتشف الجميع أن {t['name']} خرج من اللعبة."
    db_execute('UPDATE players SET night_action=NULL WHERE room_code=?',(code,)); result=check_winner(code)
    if result:return await finish_game(code,result)
    db_execute("UPDATE rooms SET phase='vote' WHERE code=?",(code,)); await add_system_message(code,msg); await manager.broadcast(code,{'type':'phase_changed'})

def check_winner(code):
    ps=get_alive_players(code); m=sum(p['role']=='mafia' for p in ps); o=len(ps)-m
    return 'citizens' if m==0 else ('mafia' if m>=o else None)

async def finish_game(code,winner):
    room=get_room(code)
    if not room:return
    db_execute("UPDATE rooms SET status='finished',phase='finished' WHERE code=?",(code,)); db_execute('INSERT INTO match_results(room_code,winner,finished_at) VALUES(?,?,?)',(code,winner,now()))
    text='🩸 المافيا انتصرت! تمكنت المافيا من السيطرة على المدينة.' if winner=='mafia' else '👥 المواطنون انتصروا! تم كشف جميع أفراد المافيا.'
    await add_system_message(code,text)
    for p in get_players(code):
        if not p['is_ai']:
            add_reward(p['user_id'],100,20); t=task_progress(p['user_id']);
            if t and t['progress']<t['target']:db_execute('UPDATE daily_tasks SET progress=? WHERE user_id=?',(t['target'],p['user_id']))
    await manager.broadcast(code,{'type':'game_finished','winner':winner})

def finished_results(code):
    room=get_room(code); ps=get_players(code); winner=None
    if room and room['status']=='finished':
        winner=db_one('SELECT winner FROM match_results WHERE room_code=? ORDER BY id DESC LIMIT 1',(code,))
        winner=winner['winner'] if winner else check_winner(code)
    return {'winner':winner,'players':[{'id':str(p['user_id']),'name':p['name'],'role':p['role'],'alive':bool(p['alive']),'is_ai':bool(p['is_ai'])} for p in ps]}

async def submit_vote(code,uid,target):
    room=get_room(code); p=get_player(code,uid); t=get_player(code,target)
    if not room:return False,'الغرفة غير موجودة.'
    if room['phase']!='vote':return False,'التصويت غير مفتوح.'
    if not p or not p['alive']:return False,'أنت خارج اللعبة.'
    if not t or not t['alive']:return False,'اللاعب غير موجود.'
    db_execute('UPDATE players SET vote=? WHERE id=?',(target,p['id'])); ps=get_alive_players(code)
    if all(p['vote'] for p in ps):await resolve_votes(code)
    else:await manager.broadcast(code,{'type':'vote_update'})
    return True,'تم تسجيل التصويت.'

async def resolve_votes(code):
    ps=get_alive_players(code); counts={}
    for p in ps:
        if p['vote']: counts[p['vote']]=counts.get(p['vote'],0)+1
    msg='⚖️ حصل تعادل، ولم يتم إخراج أي لاعب.'
    if counts:
        hi=max(counts.values()); winners=[k for k,v in counts.items() if v==hi]
        if len(winners)==1:
            e=get_player(code,winners[0]);
            if e:db_execute('UPDATE players SET alive=0 WHERE id=?',(e['id'],)); msg=f"🗳️ تم إخراج {e['name']} بالتصويت."
    db_execute('UPDATE players SET vote=NULL WHERE room_code=?',(code,)); await add_system_message(code,msg); result=check_winner(code)
    if result:return await finish_game(code,result)
    db_execute("UPDATE rooms SET phase='night' WHERE code=?",(code,)); await manager.broadcast(code,{'type':'phase_changed'})

async def add_chat_message(code,uid,text):
    p=get_player(code,uid); text=clean_text(text)
    if not p:return False,'أنت لست داخل الغرفة.'
    if not text:return False,'الرسالة فارغة.'
    if not p['alive'] and get_room(code)['status']=='playing':return False,'اللاعب الميت لا يستطيع الكتابة في الشات العام.'
    db_execute('INSERT INTO messages(room_code,user_id,name,text,created_at) VALUES(?,?,?,?,?)',(code,str(uid),p['name'],text,now())); await manager.broadcast(code,{'type':'chat','user_id':str(uid),'name':p['name'],'text':text}); return True,'تم الإرسال.'
async def add_system_message(code,text):
    db_execute("INSERT INTO messages(room_code,user_id,name,text,created_at) VALUES(?,?,?, ?,?)",(code,'system','النظام',text,now())); await manager.broadcast(code,{'type':'system','text':text})
def get_messages(code):
    rows=db_execute('SELECT user_id,name,text,created_at FROM messages WHERE room_code=? ORDER BY id DESC LIMIT 100',(code,),fetch=True); return [{'user_id':str(r['user_id']),'name':r['name'],'text':r['text'],'created_at':r['created_at']} for r in reversed(rows)]

def game_state(code,uid):
    room=get_room(code)
    if not room:return None
    p=get_player(code,uid); ps=get_players(code); out=[]
    for x in ps:
        item={'id':str(x['user_id']),'name':x['name'],'alive':bool(x['alive']),'is_ai':bool(x['is_ai'])}
        if room['phase']=='finished' or (p and str(x['user_id'])==str(uid)):item['role']=x['role']
        out.append(item)
    targets=[]
    if p and p['alive'] and room['phase'] in ('night','vote'):
        for t in ps:
            if not t['alive'] or t['id']==p['id']:continue
            if room['phase']=='vote' or p['role']!='citizen':
                if p['role']!='mafia' or t['role']!='mafia':targets.append({'id':str(t['user_id']),'name':t['name']})
    winner=finished_results(code)['winner'] if room['status']=='finished' else None
    return {'code':room['code'],'name':room['name'],'host_id':str(room['host_id']),'max_players':room['max_players'],'phase':room['phase'],'phase_name':phase_name(room['phase']),'status':room['status'],'players':out,'targets':targets,'my_role':p['role'] if p else None,'my_alive':bool(p['alive']) if p else False,'private_message':p['private_message'] if p else '','messages':get_messages(code),'winner':winner,'results':finished_results(code) if room['status']=='finished' else None}

async def process_ai(code):
    room=get_room(code)
    if not room or room['status']!='playing':return
    if room['phase']=='night':
        ai_night_actions(code); await asyncio.sleep(.2); req=[p for p in get_alive_players(code) if p['role'] in ('mafia','detective','doctor')]
        if req and all(p['night_action'] for p in req):await resolve_night(code)
    elif room['phase']=='vote':
        ps=get_alive_players(code)
        for p in ps:
            if p['is_ai']:
                candidates=[x for x in ps if x['id']!=p['id']];
                if candidates:db_execute('UPDATE players SET vote=? WHERE id=?',(random.choice(candidates)['user_id'],p['id']))
        ps=get_alive_players(code)
        if ps and all(p['vote'] for p in ps):await resolve_votes(code)

class ConnectionManager:
    def __init__(self):self.rooms={}
    async def connect(self,code,ws):self.rooms.setdefault(code,set()).add(ws); await ws.accept()
    def disconnect(self,code,ws):
        if code in self.rooms:
            self.rooms[code].discard(ws)
            if not self.rooms[code]:del self.rooms[code]
    async def broadcast(self,code,msg):
        dead=[]
        for ws in list(self.rooms.get(code,set())):
            try:await ws.send_json(msg)
            except Exception:dead.append(ws)
        for ws in dead:self.disconnect(code,ws)
manager=ConnectionManager()

async def telegram_call(method,payload=None):
    if not BOT_TOKEN:return {'ok':False,'description':'BOT_TOKEN is not configured'}
    try:
        async with httpx.AsyncClient(timeout=20) as client:r=await client.post(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',json=payload or {})
        try:return r.json()
        except:return {'ok':False,'description':r.text}
    except Exception as e:return {'ok':False,'description':str(e)}
async def send_message(chat_id,text,keyboard=None):
    p={'chat_id':chat_id,'text':text}
    if keyboard:p['reply_markup']=keyboard
    return await telegram_call('sendMessage',p)
async def answer_callback(cid,text=''):return await telegram_call('answerCallbackQuery',{'callback_query_id':cid,'text':text})
def telegram_keyboard():return {'inline_keyboard':[[{'text':'🎭 فتح لعبة المافيا','web_app':{'url':WEBAPP_URL}}],[{'text':'ℹ️ طريقة اللعب','callback_data':'help'}]]}

@asynccontextmanager
async def lifespan(app):
    init_database(); yield
app=FastAPI(title='Mafia Game',lifespan=lifespan); web=app

@app.get('/',response_class=HTMLResponse)
async def home():return HTMLResponse(GAME_HTML)
@app.get('/health')
async def health():return {'ok':True,'service':'mafia-game','database':DATABASE_FILE,'telegram_configured':bool(BOT_TOKEN),'webapp_url':WEBAPP_URL}
@app.get('/api/status')
async def status():return {'ok':True,'rooms':len(public_rooms()),'telegram_configured':bool(BOT_TOKEN)}
@app.get('/api/rooms')
async def rooms():return {'ok':True,'rooms':public_rooms()}

@app.get('/api/profile')
async def api_profile(user_id:str='',name:str=''):
    if not user_id:return JSONResponse({'ok':False,'error':'معرف اللاعب مفقود.'},400)
    touch_presence(user_id,name or 'لاعب'); t=task_progress(user_id); return {'ok':True,'profile':public_profile(user_id),'friends':friend_list(user_id),'requests':pending_requests(user_id),'task':dict(t) if t else None}
@app.post('/api/profile/skin')
async def api_skin(data:dict):
    uid=str(data.get('user_id','')); skin=str(data.get('skin','classic')); costs={'classic':0,'red':50,'gold':100,'shadow':150}
    if skin not in costs:return JSONResponse({'ok':False,'error':'السكن غير موجود.'},400)
    p=db_one('SELECT * FROM profiles WHERE user_id=?',(uid,))
    if not p:return JSONResponse({'ok':False,'error':'الحساب غير موجود.'},404)
    cost=costs[skin]
    if p['coins']<cost and p['skin']!=skin:return JSONResponse({'ok':False,'error':'عملاتك غير كافية.'},400)
    if p['skin']!=skin: db_execute('UPDATE profiles SET coins=coins-?,skin=? WHERE user_id=?',(cost,skin,uid))
    return {'ok':True,'profile':public_profile(uid)}

@app.post('/api/profile/online')
async def api_online(data:dict):
    uid=str(data.get('user_id','')).strip(); name=clean_name(data.get('name','لاعب')); online=1 if data.get('online',True) else 0
    if not uid:return {'ok':False}
    touch_presence(uid,name,online); return {'ok':True}
@app.post('/api/friends/request')
async def api_friend_request(data:dict):
    uid=str(data.get('user_id','')); ok,msg=send_friend_request(uid,data.get('public_id','')); return {'ok':ok,'message':msg}
@app.post('/api/friends/respond')
async def api_friend_respond(data:dict):
    uid=str(data.get('user_id','')); req=int(data.get('request_id',0)); action=data.get('action');
    if action=='accept':ok,msg=accept_friend(req,uid)
    else: db_execute("UPDATE friend_requests SET status='rejected' WHERE id=? AND receiver_id=?",(req,uid)); ok,msg=True,'تم رفض الطلب.'
    return {'ok':ok,'message':msg}
@app.post('/api/friends/remove')
async def api_friend_remove(data:dict):remove_friend(str(data.get('user_id','')),str(data.get('friend_id','')));return {'ok':True}
@app.get('/api/friends/search')
async def api_friend_search(public_id:str=''):
    p=profile_by_public_id(public_id); return {'ok':bool(p),'profile':public_profile(p['user_id']) if p else None}
@app.get('/api/private/{friend_id}')
async def api_private(friend_id:str,user_id:str=''):
    if not are_friends(user_id,friend_id):return JSONResponse({'ok':False,'error':'ليس صديقك.'},403)
    return {'ok':True,'messages':private_conversation(user_id,friend_id)}
@app.post('/api/private/send')
async def api_private_send(data:dict):
    ok,msg=private_send(str(data.get('user_id','')),str(data.get('friend_id','')),data.get('text','')); return {'ok':ok,'message':msg}
@app.post('/api/friends/invite')
async def api_invite(data:dict):
    code=str(data.get('code','')).upper().strip(); uid=str(data.get('user_id','')); fid=str(data.get('friend_id',''))
    if not are_friends(uid,fid):return JSONResponse({'ok':False,'error':'ليس صديقك.'},403)
    await add_system_message(code,f"📨 تمت دعوة صديق إلى الغرفة بواسطة {get_player(code,uid)['name'] if get_player(code,uid) else 'لاعب'}.")
    return {'ok':True,'code':code}

@app.post('/api/rooms/create')
async def api_create_room(data:dict):
    uid=str(data.get('user_id','')).strip(); name=clean_name(data.get('player_name','لاعب')); room=clean_name(data.get('room_name','غرفة مافيا')); mx=int(data.get('max_players',8))
    if not uid:return JSONResponse({'ok':False,'error':'معرف اللاعب مفقود.'},400)
    return {'ok':True,'code':await create_room(uid,name,room,mx)}
@app.post('/api/rooms/join')
async def api_join_room(data:dict):
    ok,msg=await join_room(str(data.get('code','')),str(data.get('user_id','')),data.get('player_name','لاعب')); return {'ok':ok,'message':msg} if ok else JSONResponse({'ok':False,'error':msg},400)
@app.post('/api/rooms/leave')
async def api_leave_room(data:dict):
    ok,msg=await leave_room(str(data.get('code','')),str(data.get('user_id',''))); return {'ok':ok,'message':msg}
@app.post('/api/rooms/start')
async def api_start_room(data:dict):
    ok,msg=await start_online_game(str(data.get('code','')),str(data.get('user_id',''))); return {'ok':ok,'message':msg} if ok else JSONResponse({'ok':False,'error':msg},400)
@app.post('/api/rooms/rematch')
async def api_rematch(data:dict):
    ok,msg=await rematch(str(data.get('code','')),str(data.get('user_id',''))); return {'ok':ok,'message':msg} if ok else JSONResponse({'ok':False,'error':msg},400)
@app.get('/api/room/{code}')
async def api_room(code:str,user_id:str=''):
    state=game_state(code.upper().strip(),user_id)
    if not state:return JSONResponse({'ok':False,'error':'الغرفة غير موجودة.'},404)
    touch_presence(user_id,None,1) if user_id else None
    return {'ok':True,'game':state}
@app.post('/api/chat')
async def api_chat(data:dict):
    ok,msg=await add_chat_message(str(data.get('code','')).upper(),str(data.get('user_id','')),data.get('text','')); return {'ok':ok} if ok else JSONResponse({'ok':False,'error':msg},400)
@app.post('/api/action')
async def api_action(data:dict):
    code=str(data.get('code','')).upper(); uid=str(data.get('user_id','')); action=str(data.get('action','')); target=str(data.get('target_id','')); room=get_room(code); p=get_player(code,uid); t=get_player(code,target)
    if not room:return JSONResponse({'ok':False,'error':'الغرفة غير موجودة.'},404)
    if action=='vote':
        ok,msg=await submit_vote(code,uid,target); return {'ok':ok} if ok else JSONResponse({'ok':False,'error':msg},400)
    if not p:return JSONResponse({'ok':False,'error':'أنت لست داخل اللعبة.'},400)
    if not p['alive']:return JSONResponse({'ok':False,'error':'أنت خارج اللعبة.'},400)
    if not t:return JSONResponse({'ok':False,'error':'الهدف غير موجود.'},400)
    if room['phase']!='night':return JSONResponse({'ok':False,'error':'ليس وقت تنفيذ هذا الدور.'},400)
    if action=='kill' and (p['role']!='mafia' or t['role']=='mafia'):return JSONResponse({'ok':False,'error':'إجراء غير مسموح.'},400)
    if action=='check' and p['role']!='detective':return JSONResponse({'ok':False,'error':'هذا الإجراء للمحقق فقط.'},400)
    if action=='heal' and p['role']!='doctor':return JSONResponse({'ok':False,'error':'هذا الإجراء للطبيب فقط.'},400)
    if action=='check':db_execute('UPDATE players SET private_message=? WHERE id=?',(f"🔎 نتيجة التحقيق: {t['name']} {'هو مافيا 🔴' if t['role']=='mafia' else 'ليس مافيا 🟢'}",p['id']))
    db_execute('UPDATE players SET night_action=? WHERE id=?',(target,p['id'])); await process_ai(code)
    req=[x for x in get_alive_players(code) if x['role'] in ('mafia','detective','doctor')]
    if req and all(x['night_action'] for x in req):await resolve_night(code)
    return {'ok':True}
@app.post('/api/single/create')
async def api_single_create(data:dict):
    uid=str(data.get('user_id','')).strip(); name=clean_name(data.get('player_name','لاعب')); count=int(data.get('count',8))
    if not uid:return JSONResponse({'ok':False,'error':'معرف اللاعب مفقود.'},400)
    return {'ok':True,'code':await create_single_player_game(uid,name,count)}

@app.websocket('/ws/{code}/{user_id}')
async def websocket_endpoint(ws:WebSocket,code:str,user_id:str):
    code=code.upper().strip(); room=get_room(code); p=get_player(code,user_id)
    if not room:await ws.close(code=4004);return
    if not p:await ws.close(code=4003);return
    touch_presence(user_id,p['name'],1); await manager.connect(code,ws)
    try:
        await ws.send_json({'type':'state','game':game_state(code,user_id)})
        while True:
            d=await ws.receive_json(); a=d.get('action')
            if a=='chat':await add_chat_message(code,user_id,d.get('text',''))
            elif a=='refresh':await ws.send_json({'type':'state','game':game_state(code,user_id)})
    except WebSocketDisconnect:pass
    except Exception as e:logger.error('WebSocket error: %s',e)
    finally:manager.disconnect(code,ws); touch_presence(user_id,p['name'],0)

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request:Request):
    try:update=await request.json()
    except:return {'ok':False}
    try:
        if 'message' in update:
            m=update['message']; chat=m.get('chat',{}); cid=chat.get('id'); text=m.get('text','')
            if text=='/start':await send_message(cid,'🎭 أهلاً بك في لعبة المافيا!\n\nادخل إلى اللعبة وأنشئ غرفة أو انضم إلى اللاعبين الموجودين.',telegram_keyboard())
            elif text=='/help':await send_message(cid,'🎭 لعبة المافيا\n\n🌐 لعب أونلاين\n🤖 لعب فردي\n👥 أصدقاء ورسائل خاصة\n💬 شات مباشر\n🌙 أدوار ليلية\n☀️ نقاش وتصويت',telegram_keyboard())
        if 'callback_query' in update:
            cb=update['callback_query']; await answer_callback(cb.get('id',''))
            if cb.get('data')=='help':await send_message(cb.get('message',{}).get('chat',{}).get('id'),'🎭 اجمع اللاعبين داخل غرفة ثم تبدأ الأدوار، وفي النهار النقاش والتصويت وفي الليل تتحرك الأدوار الخاصة.',telegram_keyboard())
    except Exception:logger.exception('Webhook error')
    return {'ok':True}
@app.get('/telegram-test')
async def telegram_test():r=await telegram_call('getMe');return {'ok':bool(r.get('ok')),'telegram':r}
@app.get('/setup-webhook')
async def setup_webhook():r=await telegram_call('setWebhook',{'url':WEBHOOK_URL,'drop_pending_updates':True});return {'ok':bool(r.get('ok')),'message':'تمت تهيئة Webhook بنجاح.' if r.get('ok') else 'فشل إعداد Webhook.','webhook_url':WEBHOOK_URL,'telegram_result':r}
@app.get('/webhook-info')
async def webhook_info():return await telegram_call('getWebhookInfo')

GAME_HTML=r'''<!doctype html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,maximum-scale=1,user-scalable=no"><title>مافيا أونلاين</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}body{margin:0;background:radial-gradient(circle at top,#202536,#080a10 65%);color:#fff;font-family:Arial,Tahoma,sans-serif}.app{max-width:620px;margin:auto;padding:14px;min-height:100vh}.header{text-align:center;padding:14px 0}.logo{font-size:48px}.title{font-size:28px;font-weight:900}.sub{color:#858ca0}.card{background:rgba(18,22,34,.96);border:1px solid #292f40;border-radius:20px;padding:14px;margin-bottom:12px}.section{font-size:18px;font-weight:900;margin-bottom:10px}.btn{width:100%;padding:13px;border-radius:13px;color:#fff;background:#292f40;font-weight:900;margin-top:8px}.primary{background:#8e273b}.green{background:#17694c}.blue{background:#245d91}.gold{background:#80601e}.red{background:#741e2c}.hidden{display:none!important}input,select{width:100%;padding:13px;border-radius:12px;border:1px solid #30384c;background:#090c13;color:#fff;outline:none;margin-bottom:8px}.tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.tabs button{padding:10px;border-radius:11px;background:#161b28;color:#fff;font-weight:900}.rooms,.players,.targets,.friends{display:flex;flex-direction:column;gap:8px}.room,.player,.friend,.request{background:#090c13;border:1px solid #282f42;border-radius:13px;padding:11px}.room-top,.player,.friend,.request{display:flex;justify-content:space-between;align-items:center;gap:8px}.room-name{font-weight:900}.muted,.room-count{color:#8e96aa;font-size:12px}.chat{height:300px;overflow-y:auto;background:#080b12;border-radius:14px;padding:9px}.msg{margin-bottom:8px;background:#151a27;border-radius:11px;padding:8px}.msg-name{font-size:12px;color:#d8ad43;font-weight:900}.system{text-align:center;color:#aab1c1}.chat-input{display:flex;gap:7px;margin-top:7px}.chat-input input{margin:0}.chat-input button{width:80px;border:0;border-radius:11px;background:#8e273b;color:#fff;font-weight:900}.code{text-align:center;font-size:34px;color:#e5b848;letter-spacing:6px;font-weight:900}.phase{text-align:center;font-size:22px;font-weight:900}.role{text-align:center;background:#090c13;border-radius:14px;padding:13px}.role-icon{font-size:38px}.role-name{font-size:21px;font-weight:900}.private{background:#392d12;border:1px solid #71591e;padding:10px;border-radius:11px;margin-top:8px}.target{display:flex;justify-content:space-between;align-items:center;background:#090c13;padding:9px;border-radius:11px}.target button{width:auto;margin:0}.dead{opacity:.4}.notice{text-align:center;color:#8e96aa;line-height:1.6}.idbox{background:#090c13;border:1px dashed #71591e;border-radius:12px;padding:10px;text-align:center}.publicid{font-size:20px;color:#e5b848;font-weight:900;letter-spacing:2px}.modal{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;padding:18px;z-index:50}.modalbox{width:min(560px,100%);background:#111625;border:1px solid #3a4258;border-radius:24px;padding:18px;max-height:90vh;overflow:auto}.winner{text-align:center;font-size:30px;font-weight:900;margin:8px}.winner.mafia{color:#e56b7d}.winner.citizens{color:#69d9a7}.resultrow{display:flex;justify-content:space-between;padding:9px;background:#090c13;border-radius:10px;margin-top:6px}.skin{display:flex;justify-content:space-between;align-items:center;background:#090c13;padding:10px;border-radius:11px;margin-top:7px}.online{color:#50d58d}.offline{color:#777f91}
</style></head><body><div class="app"><div class="header"><div class="logo">🎭</div><div class="title">مافيا أونلاين</div><div class="sub">Mafia Online</div></div>
<div id="home"><div class="card"><div class="section">👤 حسابي</div><div class="idbox"><div id="myName">لاعب</div><div class="publicid" id="myPublicId">MAF-------</div><div class="muted">هذا الـID ثابت لحسابك</div></div><div class="tabs"><button onclick="showPanel('friendsPanel')">👥 أصدقائي</button><button onclick="showPanel('messagesPanel')">💬 الرسائل</button><button onclick="showPanel('profilePanel')">🎁 حسابي</button></div></div>
<div class="card"><div class="section">🌐 الرومات العامة</div><div id="rooms" class="rooms"><div class="notice">جاري التحميل...</div></div><button class="btn blue" onclick="loadRooms()">🔄 تحديث</button></div>
<div class="card"><div class="section">➕ إنشاء غرفة</div><input id="roomName" placeholder="اسم الغرفة" maxlength="30"><select id="maxPlayers"><option>3</option><option>4</option><option>5</option><option>6</option><option value="8" selected>8</option><option>10</option><option>12</option><option>15</option><option>20</option></select><button class="btn primary" onclick="createRoom()">🎭 إنشاء غرفة</button></div>
<div class="card"><div class="section">🤖 لعب فردي</div><div class="notice">العب ضد الذكاء الاصطناعي.</div><select id="singleCount"><option>5</option><option value="8" selected>8</option><option>10</option><option>12</option></select><button class="btn gold" onclick="createSingle()">🤖 ابدأ اللعب الفردي</button></div>
<div id="friendsPanel" class="card hidden"><div class="section">👥 أصدقائي</div><input id="friendId" placeholder="MAF-123456"><button class="btn green" onclick="addFriend()">➕ إرسال طلب صداقة</button><div id="requests" class="friends"></div><div id="friends" class="friends"></div></div>
<div id="messagesPanel" class="card hidden"><div class="section">💬 الرسائل الخاصة</div><div id="conversationList" class="friends"></div><div id="privateBox" class="hidden"><div id="privateTitle" class="section"></div><div id="privateMessages" class="chat"></div><div class="chat-input"><input id="privateInput" placeholder="اكتب رسالة..."><button onclick="sendPrivate()">إرسال</button></div></div></div>
<div id="profilePanel" class="card hidden"><div class="section">🎁 المكافآت والسكنات</div><div id="stats" class="notice"></div><div id="task" class="notice"></div><div id="skins"></div></div></div>
<div id="lobby" class="hidden"><div class="card"><div class="section">🏠 <span id="lobbyName"></span></div><div id="roomCode" class="code">-----</div><div id="lobbyCount" class="notice"></div></div><div class="card"><div class="section">👥 اللاعبين</div><div id="lobbyPlayers" class="players"></div></div><div class="card"><div class="section">💬 شات الغرفة</div><div id="lobbyChat" class="chat"></div><div class="chat-input"><input id="lobbyChatInput" placeholder="اكتب رسالتك..." onkeydown="if(event.key==='Enter')sendLobbyChat()"><button onclick="sendLobbyChat()">إرسال</button></div></div><div class="card"><button id="startBtn" class="btn primary" onclick="startGame()">🚀 بدء المباراة</button><button class="btn red" onclick="leaveRoom()">🚪 مغادرة</button></div></div>
<div id="game" class="hidden"><div class="card"><div id="phase" class="phase"></div><div id="role" class="role"></div><div id="private" class="private hidden"></div></div><div class="card"><div class="section">💬 الشات</div><div id="chat" class="chat"></div><div class="chat-input"><input id="chatInput" placeholder="اكتب رسالتك..." onkeydown="if(event.key==='Enter')sendChat()"><button onclick="sendChat()">إرسال</button></div></div><div class="card"><div class="section">🎯 الإجراءات</div><div id="targets" class="targets"></div></div><div class="card"><div class="section">👥 اللاعبون</div><div id="gamePlayers" class="players"></div></div><button class="btn red" onclick="leaveRoom()">🚪 الخروج</button></div></div>
<div id="resultModal" class="modal hidden"><div class="modalbox"><div id="winnerText" class="winner"></div><div class="notice">نتائج المباراة</div><div id="resultPlayers"></div><button class="btn primary" onclick="returnToRoom()">🔄 العودة إلى الغرفة</button><button id="rematchBtn" class="btn green" onclick="rematch()">🔥 مباراة جديدة</button><button class="btn red" onclick="leaveRoom()">🚪 مغادرة الغرفة</button></div></div>
<script>
const tg=window.Telegram&&window.Telegram.WebApp?window.Telegram.WebApp:null;if(tg){tg.ready();tg.expand()}
let userId='',playerName='',currentCode='',currentGame=null,socket=null,selectedFriend=null,profile=null;
function initUser(){if(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user){let u=tg.initDataUnsafe.user;userId=String(u.id);playerName=u.first_name||u.username||'لاعب'}else{userId=localStorage.getItem('mafia_user_id')||('demo_'+Math.random().toString(36).slice(2,10));localStorage.setItem('mafia_user_id',userId);playerName=localStorage.getItem('mafia_player_name')||'لاعب تجريبي'}localStorage.setItem('mafia_player_name',playerName)}
initUser();
function notify(t){if(tg&&tg.showAlert)tg.showAlert(t);else alert(t)}
async function api(url,opt={}){let r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});let d=await r.json();if(!r.ok)throw Error(d.error||d.message||'حدث خطأ.');return d}
function show(id){['home','lobby','game'].forEach(x=>document.getElementById(x).classList.add('hidden'));document.getElementById(id).classList.remove('hidden')}
function showPanel(id){['friendsPanel','messagesPanel','profilePanel'].forEach(x=>document.getElementById(x).classList.add('hidden'));document.getElementById(id).classList.remove('hidden');if(id==='friendsPanel')loadSocial();if(id==='messagesPanel')loadSocial();if(id==='profilePanel')loadProfile()}
async function loadProfile(){let d=await api('/api/profile?user_id='+encodeURIComponent(userId)+'&name='+encodeURIComponent(playerName));profile=d.profile;document.getElementById('myName').textContent=profile.name;document.getElementById('myPublicId').textContent=profile.public_id;document.getElementById('stats').innerHTML=`⭐ XP: <b>${profile.xp}</b> &nbsp; 🪙 العملات: <b>${profile.coins}</b>`;document.getElementById('task').innerHTML=d.task?`🎯 مهمة اليوم: إكمال مباراة — ${d.task.progress}/${d.task.target} | +${d.task.reward_xp} XP +${d.task.reward_coins} 🪙`:'';renderSkins(profile.skin)}
function renderSkins(active){let skins=[['classic','🎭 كلاسيكي',0],['red','🩸 أحمر',50],['gold','👑 ذهبي',100],['shadow','🌑 ظل',150]];document.getElementById('skins').innerHTML=skins.map(s=>`<div class="skin"><span>${s[1]} ${s[2]?`(${s[2]} 🪙)`:''}</span><button class="btn ${active===s[0]?'green':'blue'}" style="width:auto;margin:0" onclick="chooseSkin('${s[0]}',${s[2]})">${active===s[0]?'مفعّل':'اختيار'}</button></div>`).join('')}
async function chooseSkin(s,cost){try{let d=await api('/api/profile/skin',{method:'POST',body:JSON.stringify({user_id:userId,skin:s})});profile=d.profile;notify('تم تفعيل السكن.');renderSkins(profile.skin);document.getElementById('stats').innerHTML=`⭐ XP: <b>${profile.xp}</b> &nbsp; 🪙 العملات: <b>${profile.coins}</b>`}catch(e){notify(e.message)}}
async function loadSocial(){let d=await api('/api/profile?user_id='+encodeURIComponent(userId)+'&name='+encodeURIComponent(playerName));profile=d.profile;document.getElementById('myName').textContent=profile.name;document.getElementById('myPublicId').textContent=profile.public_id;document.getElementById('requests').innerHTML=(d.requests||[]).map(r=>`<div class="request"><span>👤 ${esc(r.name)}<br><small>${r.public_id}</small></span><span><button class="btn green" style="width:auto;margin:0" onclick="respond(${r.id},'accept')">قبول</button> <button class="btn red" style="width:auto;margin:0" onclick="respond(${r.id},'reject')">رفض</button></span></div>`).join('');document.getElementById('friends').innerHTML=(d.friends||[]).map(f=>`<div class="friend"><span>${f.online?'🟢':'🔴'} ${esc(f.name)}<br><small>${f.public_id} · ${f.online?'متصل':'غير متصل'}</small></span><span><button class="btn blue" style="width:auto;margin:0" onclick="openPrivate('${f.user_id}','${esc(f.name)}')">💬</button><button class="btn green" style="width:auto;margin:4px 0 0" onclick="inviteFriend('${f.user_id}')">📨</button></span></div>`).join('')||'<div class="notice">لا يوجد أصدقاء بعد.</div>';document.getElementById('conversationList').innerHTML=(d.friends||[]).map(f=>`<button class="btn" onclick="openPrivate('${f.user_id}','${esc(f.name)}')">${f.online?'🟢':'🔴'} ${esc(f.name)}</button>`).join('')}
async function addFriend(){let id=document.getElementById('friendId').value.trim();if(!id)return;let d=await api('/api/friends/request',{method:'POST',body:JSON.stringify({user_id:userId,public_id:id})});notify(d.message);document.getElementById('friendId').value='';loadSocial()}
async function respond(id,a){let d=await api('/api/friends/respond',{method:'POST',body:JSON.stringify({user_id:userId,request_id:id,action:a})});notify(d.message);loadSocial()}
async function openPrivate(id,name){selectedFriend=id;document.getElementById('privateBox').classList.remove('hidden');document.getElementById('privateTitle').textContent='💬 '+name;let d=await api('/api/private/'+encodeURIComponent(id)+'?user_id='+encodeURIComponent(userId));document.getElementById('privateMessages').innerHTML=d.messages.map(m=>`<div class="msg"><div class="msg-name">${m.sender_id===userId?'أنت':esc(name)}</div><div>${esc(m.text)}</div></div>`).join('')}
async function sendPrivate(){if(!selectedFriend)return;let i=document.getElementById('privateInput'),t=i.value.trim();if(!t)return;let d=await api('/api/private/send',{method:'POST',body:JSON.stringify({user_id:userId,friend_id:selectedFriend,text:t})});if(!d.ok)notify(d.message);i.value='';openPrivate(selectedFriend,document.getElementById('privateTitle').textContent.replace('💬 ',''))}
async function inviteFriend(fid){if(!currentCode){notify('ادخل غرفة أولاً.');return}let d=await api('/api/friends/invite',{method:'POST',body:JSON.stringify({user_id:userId,friend_id:fid,code:currentCode})});notify(d.message||'تمت الدعوة.')}
async function loadRooms(){try{let d=await api('/api/rooms');let box=document.getElementById('rooms');box.innerHTML=d.rooms.length?d.rooms.map(r=>`<div class="room"><div><b>🎭 ${esc(r.name)}</b><div class="room-count">👥 ${r.players}/${r.max_players}</div></div><button class="btn green" style="width:auto;margin:0" onclick="joinRoom('${r.code}')">دخول</button></div>`).join(''):'<div class="notice">لا توجد رومات حالياً.</div>'}catch(e){notify(e.message)}}
async function createRoom(){try{let d=await api('/api/rooms/create',{method:'POST',body:JSON.stringify({user_id:userId,player_name:playerName,room_name:document.getElementById('roomName').value.trim()||'غرفة مافيا',max_players:Number(document.getElementById('maxPlayers').value)})});openRoom(d.code)}catch(e){notify(e.message)}}
async function joinRoom(c){try{await api('/api/rooms/join',{method:'POST',body:JSON.stringify({code:c,user_id:userId,player_name:playerName}));openRoom(c)}catch(e){notify(e.message)}}
async function openRoom(c){currentCode=c;connectSocket();await refresh();show(currentGame&&currentGame.status==='waiting'?'lobby':'game');}
function connectSocket(){if(socket)try{socket.close()}catch{};let pr=location.protocol==='https:'?'wss':'ws';socket=new WebSocket(pr+'://'+location.host+'/ws/'+encodeURIComponent(currentCode)+'/'+encodeURIComponent(userId));socket.onmessage=e=>{let d=JSON.parse(e.data);refresh()};socket.onclose=()=>setTimeout(()=>{if(currentCode)connectSocket()},2500)}
async function refresh(){if(!currentCode)return;try{let d=await api('/api/room/'+encodeURIComponent(currentCode)+'?user_id='+encodeURIComponent(userId));currentGame=d.game;renderCurrent()}catch(e){console.log(e.message)}}
function renderCurrent(){if(!currentGame)return;if(currentGame.status==='waiting'){renderLobby();show('lobby')}else if(currentGame.status==='finished'){renderGame();show('game');showResults()}else{renderGame();show('game')}}
function renderLobby(){document.getElementById('lobbyName').textContent=currentGame.name;document.getElementById('roomCode').textContent=currentGame.code;document.getElementById('lobbyCount').textContent=`👥 ${currentGame.players.length} / ${currentGame.max_players}`;document.getElementById('lobbyPlayers').innerHTML=currentGame.players.map(p=>`<div class="player"><span>${esc(p.name)}</span><span>${String(p.id)===String(currentGame.host_id)?'👑':'👤'}</span></div>`).join('');renderLobbyChat();let b=document.getElementById('startBtn');b.classList.toggle('hidden',String(currentGame.host_id)!==String(userId));b.disabled=currentGame.players.length<3;b.textContent=currentGame.status==='finished'?'🔥 مباراة جديدة':'🚀 بدء المباراة'}
function renderLobbyChat(){document.getElementById('lobbyChat').innerHTML=(currentGame.messages||[]).map(m=>`<div class="msg ${m.name==='النظام'?'system':''}">${m.name==='النظام'?esc(m.text):`<div class="msg-name">${esc(m.name)}</div><div>${esc(m.text)}</div>`}</div>`).join('')}
async function startGame(){try{await api('/api/rooms/start',{method:'POST',body:JSON.stringify({code:currentCode,user_id:userId})});refresh()}catch(e){notify(e.message)}}
function renderGame(){document.getElementById('phase').textContent=currentGame.phase_name;let roles={mafia:['🔴','المافيا'],detective:['🔎','المحقق'],doctor:['💉','الطبيب'],citizen:['👤','المواطن']},r=roles[currentGame.my_role];document.getElementById('role').innerHTML=r?`<div class="role-icon">${r[0]}</div><div class="role-name">${r[1]}</div>`:'جاري التحميل...';let pr=document.getElementById('private');pr.classList.toggle('hidden',!currentGame.private_message);pr.textContent=currentGame.private_message||'';renderChat();renderTargets();document.getElementById('gamePlayers').innerHTML=currentGame.players.map(p=>`<div class="player ${p.alive?'':'dead'}"><span><b>${esc(p.name)}</b><br><small>${p.alive?'🟢 على قيد الحياة':'💀 خارج اللعبة'}</small></span><span>${p.role?roles[p.role][0]:'👤'}</span></div>`).join('')}
function renderChat(){document.getElementById('chat').innerHTML=(currentGame.messages||[]).map(m=>`<div class="msg ${m.name==='النظام'?'system':''}">${m.name==='النظام'?esc(m.text):`<div class="msg-name">${esc(m.name)}</div><div>${esc(m.text)}</div>`}</div>`).join('');let b=document.getElementById('chat');b.scrollTop=b.scrollHeight}
function renderTargets(){let b=document.getElementById('targets');b.innerHTML='';if(!currentGame.my_alive){b.innerHTML='<div class="notice">💀 أنت خارج اللعبة.</div>';return}let ts=currentGame.targets||[];if(!ts.length){b.innerHTML='<div class="notice">لا يوجد إجراء متاح الآن.</div>';return}ts.forEach(t=>{let row=document.createElement('div');row.className='target';row.innerHTML=`<span>${esc(t.name)}</span>`;let btn=document.createElement('button');btn.className='btn primary';btn.textContent=currentGame.phase==='vote'?'🗳️ تصويت':'اختيار';btn.onclick=()=>sendAction(currentGame.phase==='vote'?'vote':({mafia:'kill',detective:'check',doctor:'heal'}[currentGame.my_role]||''),t.id);row.appendChild(btn);b.appendChild(row)})}
async function sendAction(action,target){if(!action)return;try{await api('/api/action',{method:'POST',body:JSON.stringify({code:currentCode,user_id:userId,action,target_id:target})});refresh()}catch(e){notify(e.message)}}
function sendChat(){let i=document.getElementById('chatInput'),t=i.value.trim();if(!t)return;if(socket&&socket.readyState===1)socket.send(JSON.stringify({action:'chat',text:t}));else api('/api/chat',{method:'POST',body:JSON.stringify({code:currentCode,user_id:userId,text:t})});i.value=''}
function sendLobbyChat(){let i=document.getElementById('lobbyChatInput'),t=i.value.trim();if(!t)return;if(socket&&socket.readyState===1)socket.send(JSON.stringify({action:'chat',text:t}));i.value=''}
async function createSingle(){try{let d=await api('/api/single/create',{method:'POST',body:JSON.stringify({user_id:userId,player_name:playerName,count:Number(document.getElementById('singleCount').value)})});openRoom(d.code)}catch(e){notify(e.message)}}
function showResults(){let r=currentGame.results;if(!r)return;let win=r.winner==='mafia';document.getElementById('winnerText').className='winner '+(win?'mafia':'citizens');document.getElementById('winnerText').textContent=win?'🩸 المافيا انتصرت':'👥 المواطنون انتصروا';document.getElementById('resultPlayers').innerHTML=r.players.map(p=>`<div class="resultrow"><span>${esc(p.name)} ${p.alive?'🟢':'💀'}</span><span>${roleText(p.role)}</span></div>`).join('');document.getElementById('rematchBtn').classList.toggle('hidden',String(currentGame.host_id)!==String(userId));document.getElementById('resultModal').classList.remove('hidden')}
function hideResults(){document.getElementById('resultModal').classList.add('hidden')}
async function returnToRoom(){hideResults();try{let d=await api('/api/room/'+encodeURIComponent(currentCode)+'?user_id='+encodeURIComponent(userId));currentGame=d.game;if(currentGame.status==='finished'){notify('انتظر صاحب الغرفة ليبدأ مباراة جديدة.')}else{renderLobby();show('lobby')}}catch(e){notify(e.message)}}
async function rematch(){try{let d=await api('/api/rooms/rematch',{method:'POST',body:JSON.stringify({code:currentCode,user_id:userId})});hideResults();refresh()}catch(e){notify(e.message)}}
async function leaveRoom(){if(!currentCode)return;try{await api('/api/rooms/leave',{method:'POST',body:JSON.stringify({code:currentCode,user_id:userId})})}catch{};hideResults();if(socket)try{socket.close()}catch{};currentCode='';currentGame=null;show('home');loadRooms();loadProfile()}
function roleText(r){return {mafia:'🔴 مافيا',detective:'🔎 محقق',doctor:'💉 طبيب',citizen:'👤 مواطن'}[r]||r}
function esc(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;')}
async function boot(){await loadProfile();await loadRooms();setInterval(()=>{if(!currentCode)loadRooms();else refresh()},5000)}
window.addEventListener('beforeunload',()=>{if(userId)fetch('/api/profile/online',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:userId,name:playerName,online:false})})});show('home');boot();
</script></body></html>'''

if __name__=='__main__':
    import uvicorn
    uvicorn.run('main:web',host='0.0.0.0',port=int(os.getenv('PORT','8080')))
