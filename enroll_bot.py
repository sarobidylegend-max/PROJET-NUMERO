"""
Bot d'enrôlement — Gestion des accès au bot principal
Les agents font /start ici pour demander l'accès.
L'admin principal ET les sous-admins peuvent approuver/refuser.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import user_manager as um
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def b(t):    return f"<b>{t}</b>"
def code(t): return f"<code>{t}</code>"
def i(t):    return f"<i>{t}</i>"
def esc(t):  return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def is_main_admin(uid): return uid == Config.ADMIN_ID
def is_admin(uid):      return is_main_admin(uid) or um.is_sub_admin(uid)

def build_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Demandes en attente",     callback_data="adm_pending")],
        [InlineKeyboardButton("✅ Agents autorisés",         callback_data="adm_authorized")],
        [InlineKeyboardButton("🚫 Utilisateurs bannis",     callback_data="adm_banned")],
    ])

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid        = update.effective_user.id
    username   = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""

    if is_admin(uid):
        role = " [ADMIN PRINCIPAL]" if is_main_admin(uid) else " [SOUS-ADMIN]"
        await update.message.reply_text(
            f"🔐 {b('Panel Gestion des accès')}{role}\n\nGérez les demandes d'accès au bot principal.",
            parse_mode="HTML", reply_markup=build_admin_menu())
        return

    if um.is_banned(uid):
        await update.message.reply_text("🚫 Vous avez été banni. Contactez l'administrateur.")
        return

    if um.is_authorized(uid) or um.is_sub_admin(uid):
        await update.message.reply_text(
            f"✅ {b('Vous avez déjà accès au bot principal !')}\n\n🇫🇷 Rendez-vous sur le bot principal.",
            parse_mode="HTML")
        return

    if um.is_pending(uid):
        await update.message.reply_text(
            f"⏳ {b('Votre demande est en cours.')}\nPatientez, un administrateur va examiner votre demande.",
            parse_mode="HTML")
        return

    # Nouvelle demande
    added = um.add_pending(uid, username, first_name)
    if not added:
        await update.message.reply_text("⚠️ Vous êtes déjà enregistré dans le système.")
        return

    name  = esc(first_name)
    uname = f"@{esc(username)}" if username else i("(pas de username)")

    notif_text = (
        f"📥 {b('Nouvelle demande d\'accès')}\n\n"
        f"👤 Nom : {b(name)}\n"
        f"🔗 Username : {uname}\n"
        f"🆔 ID Telegram : {code(str(uid))}\n\n"
        f"Accorder l'accès au bot principal ?"
    )
    kb_notif = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approuver", callback_data=f"approve_{uid}"),
        InlineKeyboardButton("❌ Refuser",   callback_data=f"reject_{uid}"),
        InlineKeyboardButton("🚫 Bannir",    callback_data=f"ban_{uid}"),
    ]])

    # Notifier l'admin principal
    try:
        await context.bot.send_message(chat_id=Config.ADMIN_ID, text=notif_text,
            parse_mode="HTML", reply_markup=kb_notif)
    except Exception as e:
        logger.warning(f"Impossible de notifier l'admin: {e}")

    # Notifier aussi les sous-admins
    for sid, sub in um.get_admins().items():
        try:
            await context.bot.send_message(chat_id=int(sid), text=notif_text,
                parse_mode="HTML", reply_markup=kb_notif)
        except Exception as e:
            logger.warning(f"Impossible de notifier sous-admin {sid}: {e}")

    await update.message.reply_text(
        f"📨 {b('Demande envoyée !')}\n\nL'administrateur va examiner votre demande.\nVous serez notifié dès qu'une décision sera prise. ⏳",
        parse_mode="HTML")

# ─── Boutons ──────────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if not is_admin(uid):
        await query.edit_message_text("⛔ Accès non autorisé.")
        return

    if data.startswith("approve_"):
        target = int(data.split("_")[1])
        user   = um.approve_user(target)
        if user:
            name = esc(user.get("first_name","Inconnu"))
            await query.edit_message_text(
                f"✅ {b(name)} ({code(str(target))}) a été {b('approuvé')} !",
                parse_mode="HTML")
            try:
                await context.bot.send_message(chat_id=target,
                    text=f"🎉 {b('Accès accordé !')}\n\n✅ Votre demande a été approuvée.\nVous pouvez utiliser le bot principal 🇫🇷",
                    parse_mode="HTML")
            except: pass
        else:
            await query.edit_message_text("⚠️ Demande introuvable (déjà traitée ?).")

    elif data.startswith("reject_"):
        target = int(data.split("_")[1])
        user   = um.reject_user(target)
        if user:
            name = esc(user.get("first_name","Inconnu"))
            await query.edit_message_text(
                f"❌ Demande de {b(name)} refusée.", parse_mode="HTML")
            try:
                await context.bot.send_message(chat_id=target,
                    text=f"❌ {b('Demande refusée')}\n\nContactez l'administrateur si nécessaire.",
                    parse_mode="HTML")
            except: pass
        else:
            await query.edit_message_text("⚠️ Demande introuvable.")

    elif data.startswith("ban_") and is_main_admin(uid):
        target = int(data.split("_")[1])
        user   = um.ban_user(target)
        name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
        await query.edit_message_text(f"🚫 {b(name)} a été banni.", parse_mode="HTML")

    elif data == "adm_pending":
        await show_pending(query)
    elif data == "adm_authorized":
        await show_authorized(query, uid)
    elif data == "adm_banned" and is_main_admin(uid):
        await show_banned(query)
    elif data.startswith("rm_") and is_admin(uid):
        target = int(data.split("_")[1])
        user   = um.remove_user(target)
        name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
        await query.edit_message_text(
            f"🔚 Accès de {b(name)} retiré.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="adm_authorized")]]))
        if user:
            try:
                await context.bot.send_message(chat_id=target,
                    text=f"🔚 {b('Accès retiré')}\n\nVotre accès au bot a été révoqué.", parse_mode="HTML")
            except: pass
    elif data.startswith("unban_") and is_main_admin(uid):
        target = int(data.split("_")[1])
        user   = um.unban_user(target)
        name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
        await query.edit_message_text(
            f"✅ {b(name)} débanni. Il devra refaire une demande d'accès.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="adm_banned")]]))
    elif data == "adm_menu":
        await query.edit_message_text(
            f"🔐 {b('Panel Gestion des accès')}",
            parse_mode="HTML", reply_markup=build_admin_menu())

# ─── Vues admin ───────────────────────────────────────────────────────────────
async def show_pending(query):
    users   = um.get_all_users()["pending"]
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")]])
    if not users:
        await query.edit_message_text("⏳ Aucune demande en attente.", reply_markup=kb_back)
        return
    text    = f"⏳ {b('Demandes en attente')} ({len(users)})\n\n"
    buttons = []
    for sid, u in users.items():
        name  = esc(u.get("first_name","Inconnu"))
        uname = f"@{esc(u['username'])}" if u.get("username") else "(pas de username)"
        text += f"👤 {b(name)} — {uname}\n🆔 {code(sid)} | 🕐 {u.get('requested_at','')}\n\n"
        buttons.append([
            InlineKeyboardButton(f"✅ {u.get('first_name','')}", callback_data=f"approve_{sid}"),
            InlineKeyboardButton("❌ Refuser",                   callback_data=f"reject_{sid}"),
            InlineKeyboardButton("🚫 Bannir",                    callback_data=f"ban_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def show_authorized(query, uid):
    users   = um.get_all_users()["agents"]
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")]])
    if not users:
        await query.edit_message_text("✅ Aucun agent autorisé.", reply_markup=kb_back)
        return
    text    = f"✅ {b('Agents autorisés')} ({len(users)})\n\n"
    buttons = []
    for sid, u in users.items():
        name   = esc(u.get("first_name","Inconnu"))
        uname  = f"@{esc(u['username'])}" if u.get("username") else "(pas de username)"
        achats = u.get("total_achats",0)
        restr  = " ⚠️" if u.get("restricted") else ""
        text  += f"👤 {b(name)}{restr} — {uname}\n📦 {achats} achat(s) | 🆔 {code(sid)}\n\n"
        buttons.append([
            InlineKeyboardButton(f"🔚 Retirer {u.get('first_name','')}", callback_data=f"rm_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def show_banned(query):
    users   = um.get_all_users()["banned"]
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")]])
    if not users:
        await query.edit_message_text("🚫 Aucun utilisateur banni.", reply_markup=kb_back)
        return
    text    = f"🚫 {b('Bannis')} ({len(users)})\n\n"
    buttons = []
    for sid, u in users.items():
        name  = esc(u.get("first_name","Inconnu"))
        text += f"👤 {b(name)} — 🆔 {code(sid)} | 🕐 {u.get('banned_at','')}\n\n"
        buttons.append([
            InlineKeyboardButton(f"✅ Débannir {u.get('first_name','')}", callback_data=f"unban_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="adm_menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return
    users = um.get_all_users()
    await update.message.reply_text(
        f"📊 {b('Résumé des accès')}\n\n"
        f"✅ Agents : {b(str(len(users['agents'])))}\n"
        f"🛡️ Sous-admins : {b(str(len(users['sub_admins'])))}\n"
        f"⏳ En attente : {b(str(len(users['pending'])))}\n"
        f"🚫 Bannis : {b(str(len(users['banned'])))}",
        parse_mode="HTML", reply_markup=build_admin_menu())

def main():
    app = Application.builder().token(Config.ENROLL_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("liste", cmd_liste))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("🔐 Bot d'enrôlement démarré...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
