import os
import asyncio
import logging
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SB_URL = os.environ.get("SB_URL", "https://kaqfpzknyqcpdmalqhex.supabase.co")
SB_KEY = os.environ.get("SB_KEY")
TG_TOKEN = os.environ.get("TG_TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "-5218962193"))

db = create_client(SB_URL, SB_KEY)

# States
(AWAIT_COMPANY, AWAIT_PLAYER_NAME, AWAIT_PLAYER_PHONE,
 AWAIT_PLAYER_POSITION, AWAIT_PLAYER_EXTRA, CONFIRM_SUBMIT) = range(6)

# ─── HELPERS ─────────────────────────────────────────────────────────────────

async def get_hr(telegram_id: int):
    """Вернуть company_user + company если HR авторизован."""
    try:
        r = db.from_("company_users").select("*,companies(id,name)") \
            .eq("telegram_id", telegram_id).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

async def get_registration(company_id: str, tournament_id: str):
    try:
        r = db.from_("team_registrations").select("*") \
            .eq("company_id", company_id).eq("tournament_id", tournament_id).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

async def ensure_registration(company_id: str, tournament_id: str):
    reg = await get_registration(company_id, tournament_id)
    if not reg:
        r = db.from_("team_registrations").insert({
            "company_id": company_id,
            "tournament_id": tournament_id,
            "status": "draft"
        }).execute()
        return r.data[0]
    return reg

async def open_tournaments():
    r = db.from_("tournaments").select("*").eq("status", "open").execute()
    return r.data or []

async def send_admin(app, text):
    try:
        await app.bot.send_message(ADMIN_CHAT_ID, text)
    except Exception as e:
        logger.warning(f"Admin notify failed: {e}")

def main_menu():
    return ReplyKeyboardMarkup([
        ["📋 Мои заявки", "👥 Состав команды"],
        ["➕ Добавить игрока", "📤 Отправить заявку"],
        ["📊 Статус заявки"]
    ], resize_keyboard=True)

# ─── /start ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)

    if hr:
        ctx.user_data["company_id"] = hr["companies"]["id"]
        ctx.user_data["company_name"] = hr["companies"]["name"]
        await update.message.reply_text(
            f"👋 Привет! Ты представитель *{hr['companies']['name']}*.\n\nВыбери действие:",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        return ConversationHandler.END

    # Проверить pending
    p = db.from_("pending_hr").select("*").eq("telegram_id", tg_id).execute()
    if p.data:
        p.data = p.data[0]
    if p.data:
        status = p.data["status"]
        if status == "pending":
            await update.message.reply_text(
                "⏳ Твоя заявка на доступ уже отправлена и ожидает подтверждения администратора."
            )
        elif status == "rejected":
            await update.message.reply_text(
                "❌ Твоя заявка была отклонена. Обратись к администратору КЛЧ."
            )
        return ConversationHandler.END

    # Новый пользователь
    await update.message.reply_text(
        "👋 Добро пожаловать в *CoReg КЛЧ*!\n\n"
        "Это система регистрации команд на турниры Корпоративной Лиги Чемпионов.\n\n"
        "Напиши название своей компании:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return AWAIT_COMPANY

async def receive_company(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = update.effective_user
    company_name = update.message.text.strip()

    db.from_("pending_hr").insert({
        "telegram_id": tg_id,
        "username": user.username or "",
        "full_name": user.full_name or "",
        "company_name": company_name,
        "status": "pending"
    }).execute()

    await update.message.reply_text(
        f"✅ Заявка отправлена!\n\n"
        f"Компания: *{company_name}*\n\n"
        f"Администратор рассмотрит заявку и даст тебе доступ. "
        f"Ты получишь уведомление здесь.",
        parse_mode="Markdown"
    )

    await send_admin(
        ctx.application,
        f"🆕 Новая заявка на доступ HR!\n"
        f"👤 {user.full_name} (@{user.username})\n"
        f"🏢 Компания: {company_name}\n"
        f"🆔 Telegram ID: {tg_id}\n\n"
        f"Открой CoReg → Заявки на доступ → подтверди."
    )
    return ConversationHandler.END

# ─── МОИ ЗАЯВКИ ──────────────────────────────────────────────────────────────

async def my_registrations(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await update.message.reply_text("❌ Нет доступа. Напиши /start")
        return

    company_id = hr["companies"]["id"]
    r = db.from_("team_registrations").select("*,tournaments(name,sport_type)") \
        .eq("company_id", company_id).execute()
    regs = r.data or []

    if not regs:
        await update.message.reply_text(
            "У вас ещё нет заявок.\n\nОткрытые турниры:",
            reply_markup=main_menu()
        )
        tours = await open_tournaments()
        if tours:
            kb = [[InlineKeyboardButton(t["name"], callback_data=f"join_{t['id']}")] for t in tours]
            await update.message.reply_text(
                "Выбери турнир для регистрации:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        return

    sport_icon = {"chess":"♟️","esports_cs2":"🎮","bowling":"🎳","football":"⚽","table_tennis":"🏓"}
    status_text = {"draft":"📝 Черновик","submitted":"⏳ На проверке","approved":"✅ Принята","rejected":"❌ Отклонена"}

    text = f"📋 *Заявки команды {hr['companies']['name']}:*\n\n"
    for reg in regs:
        t = reg.get("tournaments", {})
        icon = sport_icon.get(t.get("sport_type",""), "🏆")
        st = status_text.get(reg["status"], reg["status"])
        text += f"{icon} {t.get('name','—')}\n{st}\n\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())

async def join_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tournament_id = query.data.replace("join_", "")

    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await query.message.reply_text("❌ Нет доступа.")
        return

    company_id = hr["companies"]["id"]
    reg = await ensure_registration(company_id, tournament_id)
    ctx.user_data["company_id"] = company_id
    ctx.user_data["tournament_id"] = tournament_id
    ctx.user_data["reg_id"] = reg["id"]

    await query.message.reply_text(
        f"✅ Заявка на турнир создана!\n\nТеперь добавь игроков через «➕ Добавить игрока».",
        reply_markup=main_menu()
    )

# ─── СОСТАВ КОМАНДЫ ──────────────────────────────────────────────────────────

async def show_roster(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await update.message.reply_text("❌ Нет доступа.")
        return

    company_id = hr["companies"]["id"]

    # Выбрать активный турнир
    regs = db.from_("team_registrations").select("*,tournaments(name)") \
        .eq("company_id", company_id).eq("status", "draft").execute()
    if not regs.data:
        await update.message.reply_text("Нет активных заявок в статусе Черновик.")
        return

    reg = regs.data[0]
    roster = db.from_("tournament_roster").select("*,players_pool(full_name,phone)") \
        .eq("registration_id", reg["id"]).order("order_no").execute()

    if not roster.data:
        await update.message.reply_text(
            f"👥 Состав на *{reg['tournaments']['name']}*: пусто\n\nДобавь игроков через «➕ Добавить игрока»",
            parse_mode="Markdown"
        )
        return

    text = f"👥 *Состав на {reg['tournaments']['name']}:*\n\n"
    for i, r in enumerate(roster.data, 1):
        p = r.get("players_pool", {})
        role = "👑 Капитан" if r["order_no"] == 1 else ("🔄 Запасной" if r["is_reserve"] else "")
        text += f"{i}. {p.get('full_name','—')} {role}\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())

# ─── ДОБАВИТЬ ИГРОКА ─────────────────────────────────────────────────────────

async def add_player_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await update.message.reply_text("❌ Нет доступа.")
        return ConversationHandler.END

    company_id = hr["companies"]["id"]
    ctx.user_data["company_id"] = company_id

    # Найти черновую заявку
    regs = db.from_("team_registrations").select("*,tournaments(name,roster_schema)") \
        .eq("company_id", company_id).eq("status", "draft").execute()
    if not regs.data:
        # Предложить турниры
        tours = await open_tournaments()
        if not tours:
            await update.message.reply_text("Нет открытых турниров.")
            return ConversationHandler.END
        kb = [[InlineKeyboardButton(t["name"], callback_data=f"join_{t['id']}")] for t in tours]
        await update.message.reply_text(
            "Сначала зарегистрируйся на турнир:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    reg = regs.data[0]
    ctx.user_data["reg_id"] = reg["id"]
    ctx.user_data["tournament_id"] = reg["tournament_id"]
    ctx.user_data["roster_schema"] = reg.get("tournaments", {}).get("roster_schema", {})
    ctx.user_data["new_player"] = {}

    # Проверить архив игроков
    pool = db.from_("players_pool").select("*").eq("company_id", company_id).eq("is_active", True).execute()
    pool_data = pool.data or []

    # Получить уже добавленных
    in_roster = db.from_("tournament_roster").select("player_id").eq("registration_id", reg["id"]).execute()
    in_ids = {r["player_id"] for r in (in_roster.data or [])}
    available = [p for p in pool_data if p["id"] not in in_ids]

    if available:
        kb = [[InlineKeyboardButton(p["full_name"], callback_data=f"pool_{p['id']}")] for p in available]
        kb.append([InlineKeyboardButton("➕ Новый игрок", callback_data="pool_new")])
        await update.message.reply_text(
            "Выбери игрока из архива или добавь нового:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await update.message.reply_text(
            "Введи ФИО нового игрока:",
            reply_markup=ReplyKeyboardRemove()
        )
        ctx.user_data["adding_new"] = True
        return AWAIT_PLAYER_NAME

    return AWAIT_PLAYER_NAME

async def pool_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pool_new":
        await query.message.reply_text("Введи ФИО нового игрока:")
        ctx.user_data["adding_new"] = True
        return AWAIT_PLAYER_NAME

    player_id = query.data.replace("pool_", "")
    pr = db.from_("players_pool").select("*").eq("id", player_id).execute()
    player = pr.data[0] if pr.data else {}

    # Добавить в состав
    roster = db.from_("tournament_roster").select("order_no") \
        .eq("registration_id", ctx.user_data["reg_id"]).order("order_no", desc=True).limit(1).execute()
    next_no = (roster.data[0]["order_no"] if roster.data else 0) + 1

    db.from_("tournament_roster").insert({
        "registration_id": ctx.user_data["reg_id"],
        "player_id": player_id,
        "order_no": next_no,
        "is_reserve": False
    }).execute()

    await query.message.reply_text(
        f"✅ *{player['full_name']}* добавлен в состав!",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

async def receive_player_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_player"]["name"] = update.message.text.strip()
    await update.message.reply_text("Телефон игрока (или напиши «-» если нет):")
    return AWAIT_PLAYER_PHONE

async def receive_player_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    ctx.user_data["new_player"]["phone"] = None if phone == "-" else phone
    await update.message.reply_text("Должность в компании:")
    return AWAIT_PLAYER_POSITION

async def receive_player_position(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_player"]["position"] = update.message.text.strip()

    # Проверить дополнительные поля по schema
    schema = ctx.user_data.get("roster_schema", {})
    extra_fields = [f for f in schema.get("fields", []) if f["key"] not in ("position",)]
    ctx.user_data["extra_fields"] = extra_fields
    ctx.user_data["extra_idx"] = 0
    ctx.user_data["extra_data"] = {}

    if extra_fields:
        f = extra_fields[0]
        await update.message.reply_text(
            f"{f['label']}{'  *обязательно*' if f.get('required') else ' (необязательно, напиши «-»)'}:",
            parse_mode="Markdown"
        )
        return AWAIT_PLAYER_EXTRA

    return await save_new_player(update, ctx)

async def receive_player_extra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    fields = ctx.user_data["extra_fields"]
    idx = ctx.user_data["extra_idx"]
    f = fields[idx]

    if val != "-":
        ctx.user_data["extra_data"][f["key"]] = val

    idx += 1
    ctx.user_data["extra_idx"] = idx

    if idx < len(fields):
        nf = fields[idx]
        await update.message.reply_text(
            f"{nf['label']}{'  *обязательно*' if nf.get('required') else ' (необязательно, напиши «-»)'}:",
            parse_mode="Markdown"
        )
        return AWAIT_PLAYER_EXTRA

    return await save_new_player(update, ctx)

async def save_new_player(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ctx.user_data["new_player"]
    company_id = ctx.user_data["company_id"]

    # Создать в players_pool
    new_p = db.from_("players_pool").insert({
        "company_id": company_id,
        "full_name": p["name"],
        "phone": p.get("phone"),
        "notes": p.get("position", "")
    }).execute()
    player_id = new_p.data[0]["id"]

    # Добавить в состав
    roster = db.from_("tournament_roster").select("order_no") \
        .eq("registration_id", ctx.user_data["reg_id"]).order("order_no", desc=True).limit(1).execute()
    next_no = (roster.data[0]["order_no"] if roster.data else 0) + 1

    db.from_("tournament_roster").insert({
        "registration_id": ctx.user_data["reg_id"],
        "player_id": player_id,
        "order_no": next_no,
        "is_reserve": False,
        "extra": ctx.user_data.get("extra_data", {})
    }).execute()

    await update.message.reply_text(
        f"✅ *{p['name']}* добавлен в состав и архив компании!",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# ─── ОТПРАВИТЬ ЗАЯВКУ ────────────────────────────────────────────────────────

async def submit_registration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await update.message.reply_text("❌ Нет доступа.")
        return

    company_id = hr["companies"]["id"]
    regs = db.from_("team_registrations").select("*,tournaments(name,roster_min)") \
        .eq("company_id", company_id).eq("status", "draft").execute()
    if not regs.data:
        await update.message.reply_text("Нет заявок в статусе черновик.")
        return

    reg = regs.data[0]
    roster = db.from_("tournament_roster").select("id").eq("registration_id", reg["id"]).execute()
    count = len(roster.data or [])
    min_players = reg.get("tournaments", {}).get("roster_min", 1)

    if count < min_players:
        await update.message.reply_text(
            f"⚠️ Минимальный состав: {min_players} игроков. Сейчас добавлено: {count}."
        )
        return

    db.from_("team_registrations").update({
        "status": "submitted",
        "submitted_at": "now()"
    }).eq("id", reg["id"]).execute()

    await update.message.reply_text(
        f"📤 Заявка *{hr['companies']['name']}* на *{reg['tournaments']['name']}* отправлена на проверку!\n\n"
        f"Ты получишь уведомление когда администратор её рассмотрит.",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

    await send_admin(
        ctx.application,
        f"📋 Новая заявка на проверку!\n"
        f"🏢 {hr['companies']['name']}\n"
        f"🏆 {reg['tournaments']['name']}\n"
        f"👥 Игроков: {count}"
    )

# ─── СТАТУС ЗАЯВКИ ───────────────────────────────────────────────────────────

async def check_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    hr = await get_hr(tg_id)
    if not hr:
        await update.message.reply_text("❌ Нет доступа.")
        return

    company_id = hr["companies"]["id"]
    regs = db.from_("team_registrations").select("*,tournaments(name)") \
        .eq("company_id", company_id).order("created_at", desc=True).execute()

    if not regs.data:
        await update.message.reply_text("У вас ещё нет заявок.")
        return

    status_text = {
        "draft": "📝 Черновик — заявка не отправлена",
        "submitted": "⏳ На проверке у администратора",
        "approved": "✅ Заявка принята!",
        "rejected": "❌ Заявка отклонена"
    }
    text = f"📊 *Статус заявок {hr['companies']['name']}:*\n\n"
    for reg in regs.data:
        t = reg.get("tournaments", {})
        st = status_text.get(reg["status"], reg["status"])
        text += f"🏆 {t.get('name','—')}\n{st}\n"
        if reg.get("admin_comment"):
            text += f"💬 Комментарий: {reg['admin_comment']}\n"
        text += "\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())

# ─── CANCEL ──────────────────────────────────────────────────────────────────

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=main_menu())
    return ConversationHandler.END

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TG_TOKEN).build()

    # Регистрация ConversationHandler
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAIT_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_company)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True
    )

    player_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить игрока$"), add_player_start)],
        states={
            AWAIT_PLAYER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_player_name),
                CallbackQueryHandler(pool_callback, pattern="^pool_"),
            ],
            AWAIT_PLAYER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_player_phone)],
            AWAIT_PLAYER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_player_position)],
            AWAIT_PLAYER_EXTRA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_player_extra)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True
    )

    app.add_handler(reg_conv)
    app.add_handler(player_conv)
    app.add_handler(CallbackQueryHandler(join_tournament, pattern="^join_"))
    app.add_handler(CallbackQueryHandler(pool_callback, pattern="^pool_"))
    app.add_handler(MessageHandler(filters.Regex("^📋 Мои заявки$"), my_registrations))
    app.add_handler(MessageHandler(filters.Regex("^👥 Состав команды$"), show_roster))
    app.add_handler(MessageHandler(filters.Regex("^📤 Отправить заявку$"), submit_registration))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статус заявки$"), check_status))

    logger.info("CoReg bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
