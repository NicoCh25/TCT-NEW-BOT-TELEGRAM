import os
import json
import logging
from datetime import time
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

UTC = pytz.utc
CONFIG_FILE = "config.json"
OWNER_ID = int(os.environ["OWNER_ID"])

ASSETS = {
    "GOLD": {"emoji": "🥇", "tp": 100,  "sl": 200},
    "US30": {"emoji": "📈", "tp": 200,  "sl": 200},
    "BTC":  {"emoji": "₿",  "tp": 400,  "sl": 800},
}

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"admins": [], "channels": []}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def can_use(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in load_config()["admins"]

def get_channels() -> list:
    channels = []
    for key, val in os.environ.items():
        if (key == "PRIMARY_CHANNEL" or key.startswith("CHANNEL_")) and val.strip():
            if val.strip() not in channels:
                channels.append(val.strip())
    for c in load_config().get("channels", []):
        if c not in channels:
            channels.append(c)
    return channels

# ── Broadcast ─────────────────────────────────────────────────────────────────
async def broadcast(bot, text: str):
    for chat_id in get_channels():
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"Error enviando a {chat_id}: {e}")

# ── Paso 1: botón COMPRA/VENTA (ventana 10 min) ───────────────────────────────
async def send_signal_prompt(bot, asset: str, job_queue=None):
    a = ASSETS[asset]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 COMPRA", callback_data=f"signal:{asset}:COMPRA"),
        InlineKeyboardButton("🔴 VENTA",  callback_data=f"signal:{asset}:VENTA"),
    ]])
    text = (
        f"📊 *SEÑAL — {a['emoji']} {asset}*\n\n"
        f"🎯 TP: +{a['tp']} pips\n"
        f"🛡 SL: −{a['sl']} pips\n\n"
        f"⏱ Ventana: 20 minutos\n\n"
        f"Elegí la dirección:"
    )
    recipients = [OWNER_ID] + load_config()["admins"]
    for uid in set(recipients):
        try:
            msg = await bot.send_message(chat_id=uid, text=text,
                                          reply_markup=keyboard, parse_mode="Markdown")
            if job_queue:
                job_queue.run_once(
                    expire_signal,
                    when=1200,  # 20 minutos
                    data={"chat_id": uid, "message_id": msg.message_id, "asset": asset},
                    name=f"expire_{msg.message_id}",
                )
        except Exception as e:
            logger.error(f"Error enviando prompt a {uid}: {e}")

async def expire_signal(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    try:
        await ctx.bot.edit_message_text(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            text=f"⏱️ *{data['asset']}* — Ventana cerrada. No se envió señal.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error expirando señal: {e}")

# ── Paso 2: botón TP/SL (aparece después de elegir dirección) ─────────────────
async def send_result_prompt(bot, asset: str, direction: str, uid: int):
    a = ASSETS[asset]
    direction_emoji = "🟢" if direction == "COMPRA" else "🔴"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ TAKE PROFIT", callback_data=f"result:{asset}:TP"),
        InlineKeyboardButton("❌ STOP LOSS",   callback_data=f"result:{asset}:SL"),
    ]])
    text = (
        f"{a['emoji']} *{asset} — {direction} {direction_emoji} activa*\n\n"
        f"¿Qué pasó con esta operación?"
    )
    try:
        await bot.send_message(chat_id=uid, text=text,
                                reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error enviando resultado prompt a {uid}: {e}")

# ── Callback: eligió COMPRA o VENTA ──────────────────────────────────────────
async def handle_signal_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not can_use(uid):
        await query.answer("⛔ Sin acceso.", show_alert=True)
        return
    await query.answer()

    _, asset, direction = query.data.split(":")
    a = ASSETS[asset]

    # Cancelar expiración
    for job in ctx.job_queue.get_jobs_by_name(f"expire_{query.message.message_id}"):
        job.schedule_removal()

    direction_emoji = "🟢" if direction == "COMPRA" else "🔴"
    signal_text = (
        f"📊 SEÑAL\n"
        f"{a['emoji']} {asset} — {direction} {direction_emoji}\n"
        f"🎯 TP: +{a['tp']} pips\n"
        f"🛡 SL: −{a['sl']} pips"
    )

    # Confirmar en el mensaje original
    await query.edit_message_text(
        f"{direction_emoji} *{asset} {direction}* enviado a todos los canales.",
        parse_mode="Markdown",
    )

    # Publicar en canales
    await broadcast(ctx.bot, signal_text)

    # Mandar botón de resultado TP/SL
    await send_result_prompt(ctx.bot, asset, direction, uid)

    logger.info(f"Señal enviada: {asset} {direction}")

# ── Callback: eligió TP o SL ─────────────────────────────────────────────────
async def handle_result_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not can_use(uid):
        await query.answer("⛔ Sin acceso.", show_alert=True)
        return
    await query.answer()

    _, asset, result = query.data.split(":")
    a = ASSETS[asset]

    if result == "TP":
        result_text = (
            f"✅ {a['emoji']} {asset} — TAKE PROFIT ✅\n"
            f"+{a['tp']} pips 🎯"
        )
        confirm = f"✅ *{asset} TAKE PROFIT* anunciado."
    else:
        result_text = (
            f"❌ {a['emoji']} {asset} — STOP LOSS\n"
            f"−{a['sl']} pips"
        )
        confirm = f"❌ *{asset} STOP LOSS* anunciado."

    await query.edit_message_text(confirm, parse_mode="Markdown")
    await broadcast(ctx.bot, result_text)
    logger.info(f"Resultado enviado: {asset} {result}")

# ── Scheduler jobs ────────────────────────────────────────────────────────────

async def gold_night_15min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⏰ En 15 min — GOLD. Prepárense para operar.")

async def gold_night_5min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⚡ En 5 min — GOLD. Estén listos.")

async def gold_night_signal(ctx: ContextTypes.DEFAULT_TYPE):
    await send_signal_prompt(ctx.bot, "GOLD", ctx.job_queue)

async def gold_us30_15min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⏰ En 15 min — GOLD. Prepárense para operar.")
    await broadcast(ctx.bot, "⏰ En 15 min — US30. Prepárense para operar.")

async def gold_us30_5min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⚡ En 5 min — GOLD. Estén listos.")
    await broadcast(ctx.bot, "⚡ En 5 min — US30. Estén listos.")

async def gold_morning_signal(ctx: ContextTypes.DEFAULT_TYPE):
    await send_signal_prompt(ctx.bot, "GOLD", ctx.job_queue)

async def us30_morning_signal(ctx: ContextTypes.DEFAULT_TYPE):
    await send_signal_prompt(ctx.bot, "US30", ctx.job_queue)

async def us30_15min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⏰ En 15 min — US30. Prepárense para operar.")

async def us30_5min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⚡ En 5 min — US30. Estén listos.")

async def us30_signal(ctx: ContextTypes.DEFAULT_TYPE):
    await send_signal_prompt(ctx.bot, "US30", ctx.job_queue)

async def btc_15min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⏰ En 15 min — BTC. Prepárense para operar.")

async def btc_5min(ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.bot, "⚡ En 5 min — BTC. Estén listos.")

async def btc_signal(ctx: ContextTypes.DEFAULT_TYPE):
    await send_signal_prompt(ctx.bot, "BTC", ctx.job_queue)

# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ No tenés acceso.")
        return
    await update.message.reply_text(
        "📡 *Signal Bot activo*\n\n"
        "*Scheduler (hora Argentina):*\n"
        "🥇 GOLD → 10:35 / 10:45 / 10:50 PM\n"
        "🥇+📈 GOLD+US30 → 3:35 / 3:45 / 3:50 AM\n"
        "📈 US30 → 7:35 / 7:45 / 7:50 AM\n"
        "₿ BTC → 9:45 / 9:55 / 10:00 AM\n\n"
        "*Comandos:*\n"
        "/test GOLD — Probar señal\n"
        "/test US30 — Probar señal\n"
        "/test BTC — Probar señal\n"
        "/addchannel `<id>` — Agregar canal\n"
        "/removechannel `<id>` — Quitar canal\n"
        "/listchannels — Ver canales\n"
        "/addadmin `<id>` — Agregar admin",
        parse_mode="Markdown",
    )

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    asset = ctx.args[0].upper() if ctx.args else "BTC"
    if asset not in ASSETS:
        await update.message.reply_text("⚠️ Usá: /testgold | /testus30 | /testbtc")
        return
    await update.message.reply_text(f"🧪 Probando señal de {asset}...")
    await send_signal_prompt(ctx.bot, asset, ctx.job_queue)

async def cmd_testgold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    await update.message.reply_text("🧪 Probando señal de GOLD...")
    await send_signal_prompt(ctx.bot, "GOLD", ctx.job_queue)

async def cmd_testus30(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    await update.message.reply_text("🧪 Probando señal de US30...")
    await send_signal_prompt(ctx.bot, "US30", ctx.job_queue)

async def cmd_testbtc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    await update.message.reply_text("🧪 Probando señal de BTC...")
    await send_signal_prompt(ctx.bot, "BTC", ctx.job_queue)

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /addchannel `<chat_id>`", parse_mode="Markdown")
        return
    chat_id = ctx.args[0]
    cfg = load_config()
    if chat_id in cfg["channels"]:
        await update.message.reply_text("⚠️ Ya está en la lista.")
        return
    cfg["channels"].append(chat_id)
    save_config(cfg)
    await update.message.reply_text(f"✅ Canal `{chat_id}` agregado.", parse_mode="Markdown")

async def cmd_removechannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /removechannel `<chat_id>`", parse_mode="Markdown")
        return
    chat_id = ctx.args[0]
    cfg = load_config()
    if chat_id not in cfg["channels"]:
        await update.message.reply_text("⚠️ No estaba en la lista.")
        return
    cfg["channels"].remove(chat_id)
    save_config(cfg)
    await update.message.reply_text("🗑️ Canal eliminado.")

async def cmd_listchannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        await update.message.reply_text("⛔ Sin acceso.")
        return
    channels = get_channels()
    if not channels:
        await update.message.reply_text("📭 No hay canales configurados.")
        return
    lines = "\n".join(f"• `{c}`" for c in channels)
    await update.message.reply_text(f"📋 *Canales:*\n{lines}", parse_mode="Markdown")

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Solo el owner.")
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /addadmin `<user_id>`", parse_mode="Markdown")
        return
    try:
        new_admin = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ El user_id debe ser un número.")
        return
    cfg = load_config()
    if new_admin in cfg["admins"]:
        await update.message.reply_text("⚠️ Ya es admin.")
        return
    cfg["admins"].append(new_admin)
    save_config(cfg)
    await update.message.reply_text(f"✅ Admin `{new_admin}` agregado.", parse_mode="Markdown")

async def broadcast_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    channels = get_channels()
    if not channels:
        await update.message.reply_text("⚠️ No hay canales configurados.")
        return
    text = update.message.text
    sent, failed = 0, []
    for chat_id in channels:
        try:
            await ctx.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:
            logger.error(f"Error enviando a {chat_id}: {e}")
            failed.append(chat_id)
    summary = f"✅ Enviado a {sent}/{len(channels)} destinos."
    if failed:
        summary += f"\n❌ Falló en: {', '.join(f'`{c}`' for c in failed)}"
    await update.message.reply_text(summary, parse_mode="Markdown")

async def broadcast_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not can_use(update.effective_user.id):
        return
    channels = get_channels()
    if not channels:
        await update.message.reply_text("⚠️ No hay canales configurados.")
        return
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    sent, failed = 0, []
    for chat_id in channels:
        try:
            await ctx.bot.send_photo(chat_id=chat_id, photo=photo.file_id, caption=caption)
            sent += 1
        except Exception as e:
            logger.error(f"Error enviando foto a {chat_id}: {e}")
            failed.append(chat_id)
    summary = f"✅ Foto enviada a {sent}/{len(channels)} destinos."
    if failed:
        summary += f"\n❌ Falló en: {', '.join(f'`{c}`' for c in failed)}"
    await update.message.reply_text(summary, parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("testgold", cmd_testgold))
    app.add_handler(CommandHandler("testus30", cmd_testus30))
    app.add_handler(CommandHandler("testbtc", cmd_testbtc))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CallbackQueryHandler(handle_signal_callback, pattern="^signal:"))
    app.add_handler(CallbackQueryHandler(handle_result_callback, pattern="^result:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_text))
    app.add_handler(MessageHandler(filters.PHOTO, broadcast_photo))

    jq = app.job_queue
    EVERYDAY = (0, 1, 2, 3, 4, 5, 6)
    WEEKDAYS = (0, 1, 2, 3, 4)
    GOLD_DAYS = (0, 1, 2, 3, 4)  # UTC: lun mar mie jue vie = AR: dom lun mar mie jue noche

    # 🥇 GOLD 10:50 PM AR
    jq.run_daily(gold_night_15min,   time(1, 35, tzinfo=UTC), days=GOLD_DAYS)
    jq.run_daily(gold_night_5min,    time(1, 45, tzinfo=UTC), days=GOLD_DAYS)
    jq.run_daily(gold_night_signal,  time(1, 50, tzinfo=UTC), days=GOLD_DAYS)

    # 🥇+📈 GOLD+US30 03:50 AM AR
    jq.run_daily(gold_us30_15min,     time(6, 35, tzinfo=UTC), days=WEEKDAYS)
    jq.run_daily(gold_us30_5min,      time(6, 45, tzinfo=UTC), days=WEEKDAYS)
    jq.run_daily(gold_morning_signal, time(6, 50, tzinfo=UTC), days=WEEKDAYS)
    jq.run_daily(us30_morning_signal, time(6, 50, tzinfo=UTC), days=WEEKDAYS)

    # 📈 US30 07:50 AM AR
    jq.run_daily(us30_15min,  time(10, 35, tzinfo=UTC), days=WEEKDAYS)
    jq.run_daily(us30_5min,   time(10, 45, tzinfo=UTC), days=WEEKDAYS)
    jq.run_daily(us30_signal, time(10, 50, tzinfo=UTC), days=WEEKDAYS)

    # ₿ BTC 10:00 AM AR
    jq.run_daily(btc_15min,  time(12, 45, tzinfo=UTC), days=EVERYDAY)
    jq.run_daily(btc_5min,   time(12, 55, tzinfo=UTC), days=EVERYDAY)
    jq.run_daily(btc_signal, time(13,  0, tzinfo=UTC), days=EVERYDAY)

    logger.info("Signal Bot iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()