# ============================================================
# ===   TUITION NOTES BOT  v2.0  (FULL HIERARCHY)          ===
# ===   Batch → Enroll → Subjects(+Teacher) → Chapters     ===
# ===   → Classes + Notes  |  ShrtFly Verify + 24h Access   ===
# ===   Search auto-detect + Year Filter + CoAdmin Locks   ===
# ============================================================

import os
import logging
import re
import secrets
import string
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson.objectid import ObjectId
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, Defaults
)
from telegram.error import BadRequest, Forbidden
import httpx

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- SECRETS --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
DEVELOPER_CONTACT = os.getenv("DEVELOPER_CONTACT", "")

if not all([BOT_TOKEN, MONGO_URI, ADMIN_ID]):
    logger.error("BOT_TOKEN, MONGO_URI, ADMIN_ID required")
    exit(1)

# -------------------- DB --------------------
client = MongoClient(MONGO_URI)
db = client.get_database()
config_col = db["config"]
users_col = db["users"]
tokens_col = db["verification_tokens"]
subs_col = db["subscriptions"]
batches_col = db["batches"]
subjects_col = db["subjects"]
chapters_col = db["chapters"]
contents_col = db["contents"]   # classes + notes
requests_col = db["requests"]

tokens_col.create_index([("token", ASCENDING)], unique=True)
tokens_col.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
subs_col.create_index([("user_id", ASCENDING)])
batches_col.create_index([("name", ASCENDING)])
batches_col.create_index([("year", ASCENDING)])
subjects_col.create_index([("batch_id", ASCENDING)])
chapters_col.create_index([("subject_id", ASCENDING)])
contents_col.create_index([("chapter_id", ASCENDING)])

logger.info("MongoDB connected")

# -------------------- FONT --------------------
FONT_MAPS = {
    "default": {},
    "default_bold": {
        'a':'𝐚','b':'𝐛','c':'𝐜','d':'𝐝','e':'𝐞','f':'𝐟','g':'𝐠','h':'𝐡','i':'𝐢',
        'j':'𝐣','k':'𝐤','l':'𝐥','m':'𝐦','n':'𝐧','o':'𝐨','p':'𝐩','q':'𝐪','r':'𝐫',
        's':'𝐬','t':'𝐭','u':'𝐮','v':'𝐯','w':'𝐰','x':'𝐱','y':'𝐲','z':'𝐳',
        'A':'𝐀','B':'𝐁','C':'𝐂','D':'𝐃','E':'𝐄','F':'𝐅','G':'𝐆','H':'𝐇','I':'𝐈',
        'J':'𝐉','K':'𝐊','L':'𝐋','M':'𝐌','N':'𝐍','O':'𝐎','P':'𝐏','Q':'𝐐','R':'𝐑',
        'S':'𝐒','T':'𝐓','U':'𝐔','V':'𝐕','W':'𝐖','X':'𝐗','Y':'𝐘','Z':'𝐙',
        '0':'𝟎','1':'𝟏','2':'𝟐','3':'𝟑','4':'𝟒','5':'𝟓','6':'𝟔','7':'𝟕','8':'𝟖','9':'𝟗'
    },
    "small_caps": {
        'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ꜰ','g':'ɢ','h':'ʜ','i':'ɪ',
        'j':'ᴊ','k':'ᴋ','l':'ʟ','m':'ᴍ','n':'ɴ','o':'ᴏ','p':'ᴘ','q':'Q','r':'ʀ',
        's':'ꜱ','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x','y':'ʏ','z':'ᴢ'
    },
}
FONT_MAPS["small_caps_bold"] = FONT_MAPS["small_caps"]

async def apply_font(raw: str, settings: dict) -> str:
    font = settings.get("font", "default")
    style = settings.get("style", "normal")
    if font == "default" and style == "normal":
        return raw.replace("<f>", "").replace("</f>", "")
    key = f"{font}_{style}" if style == "bold" else font
    fmap = FONT_MAPS.get(key) or FONT_MAPS.get("default_bold", {})
    def repl(m):
        content = m.group(1)
        parts = re.split(r'(<[^>]+>)', content)
        out = []
        for p in parts:
            if re.match(r'<[^>]+>', p):
                out.append(p)
            else:
                out.append("".join(fmap.get(c, c) for c in p))
        return "".join(out)
    try:
        return re.sub(r'<f>(.*?)</f>', repl, raw, flags=re.DOTALL)
    except:
        return raw.replace("<f>", "").replace("</f>", "")

# -------------------- CONFIG --------------------
async def get_config():
    cfg = config_col.find_one({"_id": "bot_config"})
    if not cfg:
        cfg = {
            "_id": "bot_config",
            "shrtfly_api_key": "",
            "access_hours": 24,
            "token_expiry_minutes": 20,
            "co_admins": [],
            "locked_features": ["delete_content", "admin_settings"],
            "developer_contact": DEVELOPER_CONTACT,
            "appearance": {"font": "default", "style": "normal"},
            "messages": {
                "welcome": "📚 <b><f>Welcome to Tuition Notes Bot!</f></b>\n\n"
                           "<f>Pehle verification complete karo.</f>\n"
                           "<f>Access: {access_hours} hours</f>",
                "verification_link": "🔗 <b><f>Verification Link</f></b>\n\n"
                                     "{shortlink}\n\n"
                                     "<f>Link complete karo. {token_expiry} min valid.</f>",
                "subscription_activated": "✅ <b><f>Subscription Activated!</f></b>\n\n"
                                          "<f>Valid till:</f> <code>{valid_till}</code>",
                "already_active": "✅ <b><f>Access Active</f></b>\n\n"
                                  "<f>Valid till:</f> <code>{valid_till}</code>",
                "token_used": "❌ <b>This link has already been used.</b>",
                "token_expired": "❌ <b>Link expired. Get Verified again.</b>",
                "token_wrong_user": "❌ <b>This link is not for you.</b>",
                "no_access": "🔒 <b><f>Access Required</f></b>\n\n<f>Pehle Get Verified karo.</f>",
                "access_expired": "⏰ <b>Access expired. Get Verified again.</b>",
            },
            "btn": {
                "get_verified": "✅ Get Verified",
                "refresh": "🔄 Refresh",
                "search": "🔍 Search Batch",
                "year": "📅 Year Filter",
                "request": "📩 Request",
                "my_sub": "📋 My Subscription",
                "help": "ℹ️ Help",
                "contact": "📞 Contact Dev",
                "enroll": "✅ Enroll into Batch",
            }
        }
        config_col.insert_one(cfg)
    return cfg

async def save_config(data: dict):
    config_col.update_one({"_id": "bot_config"}, {"$set": data}, upsert=True)

async def fmt(key: str, vars: dict = None) -> str:
    cfg = await get_config()
    text = cfg.get("messages", {}).get(key, key)
    if vars:
        for k, v in vars.items():
            text = text.replace("{" + k + "}", str(v))
    return await apply_font(text, cfg.get("appearance", {}))

# -------------------- HELPERS --------------------
async def is_main_admin(uid: int) -> bool:
    return uid == ADMIN_ID

async def is_co_admin(uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    cfg = await get_config()
    return uid in cfg.get("co_admins", [])

async def is_locked(uid: int, feature: str) -> bool:
    if await is_main_admin(uid):
        return False
    cfg = await get_config()
    return feature in cfg.get("locked_features", [])

async def get_sub(uid: int):
    return subs_col.find_one({"user_id": uid, "expires_at": {"$gt": datetime.utcnow()}})

async def has_access(uid: int) -> bool:
    return await get_sub(uid) is not None

def gen_token() -> str:
    return "VFY-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))

def ist(dt: datetime) -> str:
    return (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p")

async def create_shrtfly(dest: str, api_key: str) -> str | None:
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://shrtfly.com/api", params={"api": api_key, "url": dest, "format": "json"})
            data = r.json()
            if data.get("status") == "success":
                return data.get("shortenedUrl") or data.get("shortened_url") or data.get("result")
            if isinstance(data, str) and data.startswith("http"):
                return data
    except Exception as e:
        logger.error(f"ShrtFly error: {e}")
    return None

# -------------------- STATES --------------------
(
    SET_SHRTFLY, SET_HOURS, SET_TOKEN_EXP,
    ADD_BATCH_NAME, ADD_BATCH_YEAR, ADD_BATCH_THUMB, ADD_BATCH_DESC,
    ADD_SUB_NAME, ADD_SUB_TEACHER,
    ADD_CHAP_NAME,
    ADD_CONTENT_TITLE, ADD_CONTENT_TYPE, ADD_CONTENT_FILE,
    COADMIN_ADD, COADMIN_REMOVE,
    SEARCH_WAIT, REQUEST_WAIT
) = range(17)

# ============================================================
# ====================== USER SIDE ===========================
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []

    if args and args[0].startswith("verify_"):
        await do_verify(update, context, args[0][7:])
        return

    users_col.update_one(
        {"user_id": user.id},
        {"$set": {"username": user.username, "full_name": user.full_name, "last_seen": datetime.utcnow()},
         "$setOnInsert": {"joined_at": datetime.utcnow()}},
        upsert=True
    )

    cfg = await get_config()
    sub = await get_sub(user.id)

    if sub:
        text = await fmt("already_active", {"valid_till": ist(sub["expires_at"])})
        kb = [
            [InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")],
            [InlineKeyboardButton(cfg["btn"]["search"], callback_data="search")],
            [InlineKeyboardButton(cfg["btn"]["year"], callback_data="year_menu")],
            [InlineKeyboardButton(cfg["btn"]["request"], callback_data="request")],
            [InlineKeyboardButton(cfg["btn"]["my_sub"], callback_data="mysub")],
        ]
        if await is_co_admin(user.id):
            kb.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin")])
    else:
        text = await fmt("welcome", {"access_hours": cfg.get("access_hours", 24)})
        kb = [
            [InlineKeyboardButton(cfg["btn"]["get_verified"], callback_data="get_verified")],
            [InlineKeyboardButton(cfg["btn"]["help"], callback_data="help")],
            [InlineKeyboardButton(cfg["btn"]["contact"], callback_data="contact")],
        ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def do_verify(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    user = update.effective_user
    doc = tokens_col.find_one({"token": token})
    if not doc:
        await update.message.reply_text(await fmt("token_expired"), parse_mode=ParseMode.HTML)
        return
    if doc.get("used"):
        await update.message.reply_text(await fmt("token_used"), parse_mode=ParseMode.HTML)
        return
    if doc["user_id"] != user.id:
        await update.message.reply_text(await fmt("token_wrong_user"), parse_mode=ParseMode.HTML)
        return
    if doc["expires_at"] < datetime.utcnow():
        await update.message.reply_text(await fmt("token_expired"), parse_mode=ParseMode.HTML)
        return

    cfg = await get_config()
    hours = cfg.get("access_hours", 24)
    exp = datetime.utcnow() + timedelta(hours=hours)
    tokens_col.update_one({"token": token}, {"$set": {"used": True, "used_at": datetime.utcnow()}})
    subs_col.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "activated_at": datetime.utcnow(), "expires_at": exp, "token": token}},
        upsert=True
    )
    text = await fmt("subscription_activated", {"valid_till": ist(exp)})
    kb = [
        [InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")],
        [InlineKeyboardButton(cfg["btn"]["search"], callback_data="search")],
        [InlineKeyboardButton(cfg["btn"]["year"], callback_data="year_menu")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def cb_get_verified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    cfg = await get_config()
    if await get_sub(user.id):
        sub = await get_sub(user.id)
        text = await fmt("already_active", {"valid_till": ist(sub["expires_at"])})
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")]]), parse_mode=ParseMode.HTML)
        return

    api = cfg.get("shrtfly_api_key")
    if not api:
        await q.edit_message_text("❌ ShrtFly API Key set nahi hai. Admin se bolo.")
        return

    token = gen_token()
    exp_min = cfg.get("token_expiry_minutes", 20)
    tokens_col.insert_one({
        "token": token, "user_id": user.id,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=exp_min),
        "used": False
    })
    bot_uname = (await context.bot.get_me()).username
    dest = f"https://t.me/{bot_uname}?start=verify_{token}"
    short = await create_shrtfly(dest, api) or dest

    text = await fmt("verification_link", {"shortlink": short, "token_expiry": exp_min})
    kb = [
        [InlineKeyboardButton("🔗 Open Link", url=short)],
        [InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Refreshing...")
    user = q.from_user
    cfg = await get_config()
    sub = await get_sub(user.id)
    if sub:
        text = await fmt("already_active", {"valid_till": ist(sub["expires_at"])})
        kb = [
            [InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")],
            [InlineKeyboardButton(cfg["btn"]["search"], callback_data="search")],
            [InlineKeyboardButton(cfg["btn"]["year"], callback_data="year_menu")],
        ]
    else:
        text = await fmt("access_expired")
        kb = [[InlineKeyboardButton(cfg["btn"]["get_verified"], callback_data="get_verified")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def cb_mysub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cb_refresh(update, context)


async def require_access(q, user):
    if not await has_access(user.id):
        cfg = await get_config()
        text = await fmt("no_access")
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(cfg["btn"]["get_verified"], callback_data="get_verified")]]), parse_mode=ParseMode.HTML)
        return False
    return True


# -------------------- SEARCH (auto detect) --------------------
async def cb_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(q, q.from_user):
        return
    context.user_data["mode"] = "search"
    text = "🔍 <b><f>Search Batch</f></b>\n\n<f>Type karo (even 1 letter). Matching batches buttons mein aa jayenge.</f>"
    cfg = await get_config()
    text = await apply_font(text, cfg.get("appearance", {}))
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]), parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    user = update.effective_user
    text = update.message.text.strip()

    if mode == "search":
        if not await has_access(user.id):
            await update.message.reply_text(await fmt("no_access"), parse_mode=ParseMode.HTML)
            return
        # Auto detect – even 1 letter
        regex = {"$regex": f"^{re.escape(text)}", "$options": "i"} if len(text) == 1 else {"$regex": re.escape(text), "$options": "i"}
        batches = list(batches_col.find({"name": regex}).sort("name", ASCENDING).limit(25))
        if not batches:
            await update.message.reply_text(
                f"📭 No batch found for <b>{text}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search Again", callback_data="search")]])
            )
            return
        buttons = []
        for b in batches:
            label = f"{b['name']} ({b.get('year', '')})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"batch_{b['_id']}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await update.message.reply_text(
            f"🔍 Results for <b>{text}</b>:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )
        return

    if mode == "request":
        context.user_data["mode"] = None
        requests_col.insert_one({
            "user_id": user.id, "username": user.username,
            "text": text, "created_at": datetime.utcnow(), "status": "pending"
        })
        cfg = await get_config()
        for aid in set([ADMIN_ID] + cfg.get("co_admins", [])):
            try:
                await context.bot.send_message(aid,
                    f"📩 <b>New Request</b>\nUser: {user.full_name} (@{user.username})\nID: <code>{user.id}</code>\n\n{text}",
                    parse_mode=ParseMode.HTML)
            except: pass
        await update.message.reply_text("✅ Request bhej diya.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return


# -------------------- YEAR FILTER --------------------
async def cb_year_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(q, q.from_user):
        return
    years = sorted(batches_col.distinct("year"), reverse=True) or [2026, 2025, 2024, 2023, 2022]
    buttons = []
    row = []
    for y in years:
        row.append(InlineKeyboardButton(str(y), callback_data=f"yr_{y}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    await q.edit_message_text("📅 <b>Select Year</b>", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def cb_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    year = int(q.data[3:])
    batches = list(batches_col.find({"year": year}).sort("name", ASCENDING).limit(30))
    if not batches:
        await q.edit_message_text(f"📭 No batches for {year}", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="year_menu")]]), parse_mode=ParseMode.HTML)
        return
    buttons = [[InlineKeyboardButton(f"{b['name']}", callback_data=f"batch_{b['_id']}")] for b in batches]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="year_menu")])
    await q.edit_message_text(f"📚 Batches of <b>{year}</b>", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


# -------------------- BATCH → ENROLL → SUBJECTS → CHAPTERS → CONTENT --------------------
async def cb_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(q, q.from_user):
        return
    bid = q.data[6:]
    batch = batches_col.find_one({"_id": ObjectId(bid)})
    if not batch:
        await q.answer("Batch not found", show_alert=True)
        return

    cfg = await get_config()
    text = f"📚 <b>{batch['name']}</b>\n📅 Year: <code>{batch.get('year')}</code>\n\n{batch.get('description', '')}"
    kb = [
        [InlineKeyboardButton(cfg["btn"]["enroll"], callback_data=f"enroll_{bid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ]
    if batch.get("thumbnail"):
        try:
            await q.message.delete()
            await context.bot.send_photo(q.from_user.id, batch["thumbnail"], caption=text,
                                         reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
            return
        except: pass
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def cb_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bid = q.data[7:]
    subjects = list(subjects_col.find({"batch_id": ObjectId(bid)}).sort("name", ASCENDING))
    if not subjects:
        await q.edit_message_text("📭 Is batch mein abhi koi subject nahi hai.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"batch_{bid}")]]), parse_mode=ParseMode.HTML)
        return

    buttons = []
    for s in subjects:
        label = f"{s['name']} — {s.get('teacher', 'Teacher')}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sub_{s['_id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"batch_{bid}")])
    await q.edit_message_text("📖 <b>Subjects</b>\n\nSelect a subject:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def cb_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data[4:]
    chapters = list(chapters_col.find({"subject_id": ObjectId(sid)}).sort("name", ASCENDING))
    if not chapters:
        await q.edit_message_text("📭 No chapters yet.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]), parse_mode=ParseMode.HTML)
        return
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"chap_{c['_id']}")] for c in chapters]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    sub = subjects_col.find_one({"_id": ObjectId(sid)})
    title = f"📘 {sub['name']} — {sub.get('teacher', '')}" if sub else "Chapters"
    await q.edit_message_text(f"<b>{title}</b>\n\nSelect Chapter:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def cb_chapter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.data[5:]
    contents = list(contents_col.find({"chapter_id": ObjectId(cid)}).sort("order", ASCENDING))
    if not contents:
        await q.edit_message_text("📭 No classes/notes yet.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]), parse_mode=ParseMode.HTML)
        return

    buttons = []
    for c in contents:
        icon = "🎬" if c.get("type") == "video" else "📄"
        buttons.append([InlineKeyboardButton(f"{icon} {c['title']}", callback_data=f"cnt_{c['_id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    chap = chapters_col.find_one({"_id": ObjectId(cid)})
    await q.edit_message_text(f"📚 <b>{chap['name'] if chap else 'Content'}</b>\n\nSelect:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def cb_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(q, q.from_user):
        return
    cid = q.data[4:]
    content = contents_col.find_one({"_id": ObjectId(cid)})
    if not content:
        await q.answer("Not found", show_alert=True)
        return

    # Delivery like movie bot – forward file or send link
    try:
        if content.get("file_id"):
            if content.get("type") == "video":
                await context.bot.send_video(q.from_user.id, content["file_id"], caption=f"🎬 {content['title']}")
            else:
                await context.bot.send_document(q.from_user.id, content["file_id"], caption=f"📄 {content['title']}")
            await q.answer("✅ Sent!")
        elif content.get("link"):
            await q.edit_message_text(
                f"{'🎬' if content.get('type')=='video' else '📄'} <b>{content['title']}</b>\n\n{content['link']}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await q.answer("No file/link", show_alert=True)
    except Exception as e:
        logger.error(e)
        await q.answer("Error sending", show_alert=True)


# -------------------- REQUEST / HELP / CONTACT / BACK --------------------
async def cb_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(q, q.from_user):
        return
    context.user_data["mode"] = "request"
    await q.edit_message_text("📩 <b>Request</b>\n\nJo batch/subject chahiye uska naam type karo:",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
                              parse_mode=ParseMode.HTML)


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = ("ℹ️ <b>How to use</b>\n\n"
            "1. Get Verified → shortlink complete karo\n"
            "2. Search mein type karo (1 letter bhi chalega)\n"
            "3. Batch → Enroll → Subject → Chapter → Class/Notes\n"
            "4. Access 24 hours (admin change kar sakta hai)")
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]), parse_mode=ParseMode.HTML)


async def cb_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = await get_config()
    contact = cfg.get("developer_contact") or DEVELOPER_CONTACT
    kb = []
    if contact:
        url = contact if contact.startswith("http") else f"https://t.me/{contact.lstrip('@')}"
        kb.append([InlineKeyboardButton("💬 Message", url=url)])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    await q.edit_message_text("📞 Contact Developer", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    # notify
    for aid in set([ADMIN_ID] + cfg.get("co_admins", [])):
        if aid != q.from_user.id:
            try:
                await context.bot.send_message(aid, f"📞 Contact request from {q.from_user.full_name} (@{q.from_user.username}) ID:{q.from_user.id}")
            except: pass


async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["mode"] = None
    user = q.from_user
    cfg = await get_config()
    sub = await get_sub(user.id)
    if sub:
        text = await fmt("already_active", {"valid_till": ist(sub["expires_at"])})
        kb = [
            [InlineKeyboardButton(cfg["btn"]["refresh"], callback_data="refresh")],
            [InlineKeyboardButton(cfg["btn"]["search"], callback_data="search")],
            [InlineKeyboardButton(cfg["btn"]["year"], callback_data="year_menu")],
            [InlineKeyboardButton(cfg["btn"]["request"], callback_data="request")],
            [InlineKeyboardButton(cfg["btn"]["my_sub"], callback_data="mysub")],
        ]
        if await is_co_admin(user.id):
            kb.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin")])
    else:
        text = await fmt("welcome", {"access_hours": cfg.get("access_hours", 24)})
        kb = [
            [InlineKeyboardButton(cfg["btn"]["get_verified"], callback_data="get_verified")],
            [InlineKeyboardButton(cfg["btn"]["help"], callback_data="help")],
            [InlineKeyboardButton(cfg["btn"]["contact"], callback_data="contact")],
        ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


# ============================================================
# ====================== ADMIN SIDE ==========================
# ============================================================

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await is_co_admin(q.from_user.id):
        await q.answer("Access denied", show_alert=True)
        return
    is_main = await is_main_admin(q.from_user.id)
    kb = [
        [InlineKeyboardButton("📦 Add Batch", callback_data="a_add_batch")],
        [InlineKeyboardButton("📖 Manage Subjects/Chapters", callback_data="a_manage")],
        [InlineKeyboardButton("📋 List Batches", callback_data="a_list_batches")],
        [InlineKeyboardButton("📩 Requests", callback_data="a_requests")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="a_settings")],
    ]
    if is_main:
        kb.append([InlineKeyboardButton("👥 Co-Admins", callback_data="a_coadmins")])
        kb.append([InlineKeyboardButton("🔒 Locks", callback_data="a_locks")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    await q.edit_message_text("🛠️ <b>Admin Panel</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def cb_a_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await is_locked(q.from_user.id, "admin_settings"):
        await q.answer("🔒 Locked", show_alert=True)
        return
    cfg = await get_config()
    text = (f"⚙️ <b>Settings</b>\n\n"
            f"ShrtFly: {'✅ Set' if cfg.get('shrtfly_api_key') else '❌ Not Set'}\n"
            f"Access Hours: {cfg.get('access_hours', 24)}\n"
            f"Token Expiry: {cfg.get('token_expiry_minutes', 20)} min")
    kb = [
        [InlineKeyboardButton("🔑 ShrtFly API Key", callback_data="set_shrtfly")],
        [InlineKeyboardButton("⏱ Access Hours", callback_data="set_hours")],
        [InlineKeyboardButton("⏳ Token Expiry", callback_data="set_token")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


# Settings conversations
async def set_shrtfly_start(u, c):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text("🔑 ShrtFly API Key bhejo:")
    return SET_SHRTFLY

async def set_shrtfly_save(u, c):
    await save_config({"shrtfly_api_key": u.message.text.strip()})
    await u.message.reply_text("✅ Saved!")
    return ConversationHandler.END

async def set_hours_start(u, c):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text("⏱ Access hours (e.g. 24):")
    return SET_HOURS

async def set_hours_save(u, c):
    try:
        h = int(u.message.text.strip())
        await save_config({"access_hours": h})
        await u.message.reply_text(f"✅ Access hours = {h}")
    except:
        await u.message.reply_text("Invalid")
        return SET_HOURS
    return ConversationHandler.END

async def set_token_start(u, c):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text("⏳ Token expiry minutes (15-60):")
    return SET_TOKEN_EXP

async def set_token_save(u, c):
    try:
        m = int(u.message.text.strip())
        await save_config({"token_expiry_minutes": m})
        await u.message.reply_text(f"✅ Token expiry = {m} min")
    except:
        await u.message.reply_text("Invalid")
        return SET_TOKEN_EXP
    return ConversationHandler.END


# -------------------- ADD BATCH --------------------
async def a_add_batch_start(u, c):
    q = u.callback_query
    await q.answer()
    c.user_data["new_batch"] = {}
    await q.edit_message_text("📦 Batch Name bhejo:")
    return ADD_BATCH_NAME

async def a_batch_name(u, c):
    c.user_data["new_batch"]["name"] = u.message.text.strip()
    await u.message.reply_text("📅 Year (e.g. 2025):")
    return ADD_BATCH_YEAR

async def a_batch_year(u, c):
    try:
        c.user_data["new_batch"]["year"] = int(u.message.text.strip())
    except:
        await u.message.reply_text("Valid year bhejo")
        return ADD_BATCH_YEAR
    await u.message.reply_text("🖼 Thumbnail photo bhejo ya /skip")
    return ADD_BATCH_THUMB

async def a_batch_thumb(u, c):
    if u.message.photo:
        c.user_data["new_batch"]["thumbnail"] = u.message.photo[-1].file_id
    await u.message.reply_text("📝 Description ya /skip")
    return ADD_BATCH_DESC

async def a_batch_thumb_skip(u, c):
    await u.message.reply_text("📝 Description ya /skip")
    return ADD_BATCH_DESC

async def a_batch_desc(u, c):
    if u.message.text and u.message.text != "/skip":
        c.user_data["new_batch"]["description"] = u.message.text.strip()
    batch = c.user_data["new_batch"]
    batch["created_at"] = datetime.utcnow()
    batch["created_by"] = u.effective_user.id
    res = batches_col.insert_one(batch)
    await u.message.reply_text(
        f"✅ Batch <b>{batch['name']}</b> added!\nID: <code>{res.inserted_id}</code>\n\n"
        f"Ab Subjects add karne ke liye Admin → Manage Subjects use karo.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠️ Admin", callback_data="admin")]])
    )
    c.user_data.pop("new_batch", None)
    return ConversationHandler.END

async def a_batch_desc_skip(u, c):
    return await a_batch_desc(u, c)


# -------------------- MANAGE SUBJECTS / CHAPTERS / CONTENT --------------------
async def a_manage(u, c):
    q = u.callback_query
    await q.answer()
    batches = list(batches_col.find().sort("name", ASCENDING).limit(20))
    if not batches:
        await q.edit_message_text("Pehle Batch add karo.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="admin")]]))
        return
    buttons = [[InlineKeyboardButton(b["name"], callback_data=f"m_batch_{b['_id']}")] for b in batches]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
    await q.edit_message_text("📖 Select Batch to manage:", reply_markup=InlineKeyboardMarkup(buttons))


async def m_batch(u, c):
    q = u.callback_query
    await q.answer()
    bid = q.data[8:]
    c.user_data["manage_batch"] = bid
    kb = [
        [InlineKeyboardButton("➕ Add Subject", callback_data=f"add_sub_{bid}")],
        [InlineKeyboardButton("📋 List Subjects", callback_data=f"list_sub_{bid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="a_manage")],
    ]
    batch = batches_col.find_one({"_id": ObjectId(bid)})
    await q.edit_message_text(f"Managing: <b>{batch['name'] if batch else bid}</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def add_sub_start(u, c):
    q = u.callback_query
    await q.answer()
    bid = q.data[8:]
    c.user_data["new_sub"] = {"batch_id": ObjectId(bid)}
    await q.edit_message_text("Subject Name bhejo:")
    return ADD_SUB_NAME

async def add_sub_name(u, c):
    c.user_data["new_sub"]["name"] = u.message.text.strip()
    await u.message.reply_text("Teacher Name bhejo:")
    return ADD_SUB_TEACHER

async def add_sub_teacher(u, c):
    c.user_data["new_sub"]["teacher"] = u.message.text.strip()
    c.user_data["new_sub"]["created_at"] = datetime.utcnow()
    res = subjects_col.insert_one(c.user_data["new_sub"])
    await u.message.reply_text(
        f"✅ Subject added!\nAb Chapter add karne ke liye Manage → List Subjects → select subject.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠️ Admin", callback_data="admin")]])
    )
    c.user_data.pop("new_sub", None)
    return ConversationHandler.END


async def list_sub(u, c):
    q = u.callback_query
    await q.answer()
    bid = q.data[9:]
    subs = list(subjects_col.find({"batch_id": ObjectId(bid)}).sort("name", ASCENDING))
    if not subs:
        await q.edit_message_text("No subjects.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"m_batch_{bid}")]]))
        return
    buttons = [[InlineKeyboardButton(f"{s['name']} — {s.get('teacher')}", callback_data=f"m_sub_{s['_id']}")] for s in subs]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"m_batch_{bid}")])
    await q.edit_message_text("Subjects:", reply_markup=InlineKeyboardMarkup(buttons))


async def m_sub(u, c):
    q = u.callback_query
    await q.answer()
    sid = q.data[6:]
    c.user_data["manage_sub"] = sid
    kb = [
        [InlineKeyboardButton("➕ Add Chapter", callback_data=f"add_chap_{sid}")],
        [InlineKeyboardButton("📋 List Chapters", callback_data=f"list_chap_{sid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="a_manage")],
    ]
    sub = subjects_col.find_one({"_id": ObjectId(sid)})
    await q.edit_message_text(f"Subject: <b>{sub['name'] if sub else sid}</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def add_chap_start(u, c):
    q = u.callback_query
    await q.answer()
    sid = q.data[9:]
    c.user_data["new_chap"] = {"subject_id": ObjectId(sid)}
    await q.edit_message_text("Chapter Name bhejo:")
    return ADD_CHAP_NAME

async def add_chap_name(u, c):
    c.user_data["new_chap"]["name"] = u.message.text.strip()
    c.user_data["new_chap"]["created_at"] = datetime.utcnow()
    res = chapters_col.insert_one(c.user_data["new_chap"])
    await u.message.reply_text(
        f"✅ Chapter added!\nAb Content (Class/Notes) add karne ke liye List Chapters → select.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠️ Admin", callback_data="admin")]])
    )
    c.user_data.pop("new_chap", None)
    return ConversationHandler.END


async def list_chap(u, c):
    q = u.callback_query
    await q.answer()
    sid = q.data[10:]
    chaps = list(chapters_col.find({"subject_id": ObjectId(sid)}).sort("name", ASCENDING))
    if not chaps:
        await q.edit_message_text("No chapters.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"m_sub_{sid}")]]))
        return
    buttons = [[InlineKeyboardButton(ch["name"], callback_data=f"m_chap_{ch['_id']}")] for ch in chaps]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"m_sub_{sid}")])
    await q.edit_message_text("Chapters:", reply_markup=InlineKeyboardMarkup(buttons))


async def m_chap(u, c):
    q = u.callback_query
    await q.answer()
    cid = q.data[7:]
    c.user_data["manage_chap"] = cid
    kb = [
        [InlineKeyboardButton("➕ Add Class/Notes", callback_data=f"add_cnt_{cid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="a_manage")],
    ]
    chap = chapters_col.find_one({"_id": ObjectId(cid)})
    await q.edit_message_text(f"Chapter: <b>{chap['name'] if chap else cid}</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def add_cnt_start(u, c):
    q = u.callback_query
    await q.answer()
    cid = q.data[8:]
    c.user_data["new_cnt"] = {"chapter_id": ObjectId(cid), "order": 0}
    await q.edit_message_text("Title bhejo (e.g. Class 1 - Introduction):")
    return ADD_CONTENT_TITLE

async def add_cnt_title(u, c):
    c.user_data["new_cnt"]["title"] = u.message.text.strip()
    await u.message.reply_text("Type bhejo:\n1 = Video\n2 = Notes\n3 = Test Paper")
    return ADD_CONTENT_TYPE

async def add_cnt_type(u, c):
    t = u.message.text.strip()
    mapping = {"1": "video", "2": "notes", "3": "test"}
    c.user_data["new_cnt"]["type"] = mapping.get(t, "notes")
    await u.message.reply_text("Ab Video/Document file bhejo (Telegram pe forward/send) ya link bhejo:")
    return ADD_CONTENT_FILE

async def add_cnt_file(u, c):
    cnt = c.user_data["new_cnt"]
    if u.message.video:
        cnt["file_id"] = u.message.video.file_id
        cnt["type"] = "video"
    elif u.message.document:
        cnt["file_id"] = u.message.document.file_id
    elif u.message.text and u.message.text.startswith("http"):
        cnt["link"] = u.message.text.strip()
    else:
        await u.message.reply_text("Video, Document ya Link bhejo.")
        return ADD_CONTENT_FILE
    cnt["created_at"] = datetime.utcnow()
    contents_col.insert_one(cnt)
    await u.message.reply_text("✅ Content added!", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛠️ Admin", callback_data="admin")]]))
    c.user_data.pop("new_cnt", None)
    return ConversationHandler.END


# -------------------- LIST / REQUESTS / COADMIN / LOCKS --------------------
async def a_list_batches(u, c):
    q = u.callback_query
    await q.answer()
    batches = list(batches_col.find().sort([("year", DESCENDING), ("name", ASCENDING)]).limit(40))
    text = "📋 <b>Batches</b>\n\n" + "\n".join(f"• {b['name']} ({b.get('year')})" for b in batches) or "None"
    await q.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="admin")]]), parse_mode=ParseMode.HTML)


async def a_requests(u, c):
    q = u.callback_query
    await q.answer()
    reqs = list(requests_col.find({"status": "pending"}).sort("created_at", DESCENDING).limit(15))
    text = "📩 <b>Pending Requests</b>\n\n"
    for r in reqs:
        text += f"• {r.get('text')} — @{r.get('username') or r['user_id']}\n"
    if not reqs:
        text += "None"
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="admin")]]), parse_mode=ParseMode.HTML)


async def a_coadmins(u, c):
    q = u.callback_query
    await q.answer()
    if not await is_main_admin(q.from_user.id):
        await q.answer("Only main admin", show_alert=True)
        return
    cfg = await get_config()
    co = cfg.get("co_admins", [])
    text = "👥 <b>Co-Admins</b>\n\n" + ("\n".join(f"• <code>{x}</code>" for x in co) or "None")
    kb = [
        [InlineKeyboardButton("➕ Add", callback_data="co_add")],
        [InlineKeyboardButton("➖ Remove", callback_data="co_rem")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def co_add_start(u, c):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text("User ID bhejo:")
    return COADMIN_ADD

async def co_add_save(u, c):
    try:
        uid = int(u.message.text.strip())
        cfg = await get_config()
        co = cfg.get("co_admins", [])
        if uid not in co:
            co.append(uid)
            await save_config({"co_admins": co})
        await u.message.reply_text(f"✅ Added {uid}")
    except:
        await u.message.reply_text("Invalid")
        return COADMIN_ADD
    return ConversationHandler.END

async def co_rem_start(u, c):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text("Remove User ID:")
    return COADMIN_REMOVE

async def co_rem_save(u, c):
    try:
        uid = int(u.message.text.strip())
        cfg = await get_config()
        co = cfg.get("co_admins", [])
        if uid in co:
            co.remove(uid)
            await save_config({"co_admins": co})
        await u.message.reply_text(f"✅ Removed {uid}")
    except:
        await u.message.reply_text("Invalid")
        return COADMIN_REMOVE
    return ConversationHandler.END


async def a_locks(u, c):
    q = u.callback_query
    await q.answer()
    if not await is_main_admin(q.from_user.id):
        await q.answer("Only main admin", show_alert=True)
        return
    cfg = await get_config()
    locked = cfg.get("locked_features", [])
    features = ["delete_content", "admin_settings", "add_batch", "manage_content"]
    kb = []
    for f in features:
        status = "🔒" if f in locked else "🔓"
        kb.append([InlineKeyboardButton(f"{status} {f}", callback_data=f"tlock_{f}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
    await q.edit_message_text("🔒 Feature Locks for Co-Admins", reply_markup=InlineKeyboardMarkup(kb))


async def tlock(u, c):
    q = u.callback_query
    await q.answer()
    f = q.data[6:]
    cfg = await get_config()
    locked = cfg.get("locked_features", [])
    if f in locked:
        locked.remove(f)
    else:
        locked.append(f)
    await save_config({"locked_features": locked})
    await a_locks(u, c)


async def cancel(u, c):
    await u.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ============================================================
# ====================== MAIN ================================
# ============================================================
def main():
    app = Application.builder().token(BOT_TOKEN).defaults(Defaults(parse_mode=ParseMode.HTML)).build()

    # Settings conv
    settings_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(set_shrtfly_start, pattern="^set_shrtfly$"),
            CallbackQueryHandler(set_hours_start, pattern="^set_hours$"),
            CallbackQueryHandler(set_token_start, pattern="^set_token$"),
        ],
        states={
            SET_SHRTFLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_shrtfly_save)],
            SET_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_hours_save)],
            SET_TOKEN_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_token_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    batch_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(a_add_batch_start, pattern="^a_add_batch$")],
        states={
            ADD_BATCH_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_batch_name)],
            ADD_BATCH_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_batch_year)],
            ADD_BATCH_THUMB: [MessageHandler(filters.PHOTO, a_batch_thumb), CommandHandler("skip", a_batch_thumb_skip)],
            ADD_BATCH_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_batch_desc), CommandHandler("skip", a_batch_desc_skip)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_sub_start, pattern="^add_sub_")],
        states={
            ADD_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_name)],
            ADD_SUB_TEACHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_teacher)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    chap_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_chap_start, pattern="^add_chap_")],
        states={
            ADD_CHAP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chap_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    cnt_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_cnt_start, pattern="^add_cnt_")],
        states={
            ADD_CONTENT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cnt_title)],
            ADD_CONTENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cnt_type)],
            ADD_CONTENT_FILE: [
                MessageHandler(filters.VIDEO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), add_cnt_file)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    co_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(co_add_start, pattern="^co_add$"),
            CallbackQueryHandler(co_rem_start, pattern="^co_rem$"),
        ],
        states={
            COADMIN_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, co_add_save)],
            COADMIN_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, co_rem_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cb_admin))
    app.add_handler(settings_conv)
    app.add_handler(batch_conv)
    app.add_handler(sub_conv)
    app.add_handler(chap_conv)
    app.add_handler(cnt_conv)
    app.add_handler(co_conv)

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_get_verified, pattern="^get_verified$"))
    app.add_handler(CallbackQueryHandler(cb_refresh, pattern="^refresh$"))
    app.add_handler(CallbackQueryHandler(cb_mysub, pattern="^mysub$"))
    app.add_handler(CallbackQueryHandler(cb_search, pattern="^search$"))
    app.add_handler(CallbackQueryHandler(cb_year_menu, pattern="^year_menu$"))
    app.add_handler(CallbackQueryHandler(cb_year, pattern="^yr_"))
    app.add_handler(CallbackQueryHandler(cb_batch, pattern="^batch_"))
    app.add_handler(CallbackQueryHandler(cb_enroll, pattern="^enroll_"))
    app.add_handler(CallbackQueryHandler(cb_subject, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(cb_chapter, pattern="^chap_"))
    app.add_handler(CallbackQueryHandler(cb_content, pattern="^cnt_"))
    app.add_handler(CallbackQueryHandler(cb_request, pattern="^request$"))
    app.add_handler(CallbackQueryHandler(cb_help, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(cb_contact, pattern="^contact$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern="^back$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(cb_a_settings, pattern="^a_settings$"))
    app.add_handler(CallbackQueryHandler(a_manage, pattern="^a_manage$"))
    app.add_handler(CallbackQueryHandler(m_batch, pattern="^m_batch_"))
    app.add_handler(CallbackQueryHandler(list_sub, pattern="^list_sub_"))
    app.add_handler(CallbackQueryHandler(m_sub, pattern="^m_sub_"))
    app.add_handler(CallbackQueryHandler(list_chap, pattern="^list_chap_"))
    app.add_handler(CallbackQueryHandler(m_chap, pattern="^m_chap_"))
    app.add_handler(CallbackQueryHandler(a_list_batches, pattern="^a_list_batches$"))
    app.add_handler(CallbackQueryHandler(a_requests, pattern="^a_requests$"))
    app.add_handler(CallbackQueryHandler(a_coadmins, pattern="^a_coadmins$"))
    app.add_handler(CallbackQueryHandler(a_locks, pattern="^a_locks$"))
    app.add_handler(CallbackQueryHandler(tlock, pattern="^tlock_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Tuition Notes Bot v2.0 starting...")
    if WEBHOOK_URL:
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 8080)),
                        url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
