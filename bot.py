"""
Bot Telegram — Numéros virtuels HeroSMS
🇫🇷 France & 🇺🇸 USA | Multi-admin | Prix en temps réel
"""

import logging
import asyncio
import os
import sys
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from herosms_api import HeroSMSClient
from config import Config
import user_manager as um
import webhook_server as wh

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

sms_client = HeroSMSClient(Config.HEROSMS_API_KEY)

# ─── Codes pays HeroSMS ───────────────────────────────────────────────────────
COUNTRY_FR   = "78"    # France
COUNTRY_US   = "187"   # USA
COUNTRY_INFO = {
    COUNTRY_FR: {"flag": "🇫🇷", "label": "France"},
    COUNTRY_US: {"flag": "🇺🇸", "label": "USA"},
}

# ─── Timers ───────────────────────────────────────────────────────────────────
NUMBER_LIFETIME_SEC  = 3600   # Durée de vie d'un numéro : 1 heure
RELEASE_LOCKOUT_SEC  = 120    # Délai minimum avant de pouvoir libérer : 2 minutes

bot_state = {
    "veille":          False,
    "pending_numbers": {},   # {uid: {number, rental_id, user_name, price, operator, country, requested_at}}
    "active_numbers":  {},   # {uid: {number, rental_id, user_name, price, operator, country, accepted_at}}
    "history":         [],
    "price_cache":     {COUNTRY_FR: [], COUNTRY_US: []},   # Cache prix par pays
    "price_last_update": {COUNTRY_FR: None, COUNTRY_US: None},
    "user_country":    {},   # {uid: "78" | "187"}
    # Timers : {uid: datetime} — moment exact où le numéro a été attribué
    "number_start_time": {},   # pour le compte à rebours 1h
    "timer_tasks":       {},   # {uid: asyncio.Task} — tâche de fond par agent
    "refresh_tasks":     {},   # {uid: asyncio.Task} — tâche de rafraîchissement auto du panneau
    "panel_message_ids": {},   # {uid: message_id} — message du panneau actif pour édition auto
    "panel_chat_ids":    {},   # {uid: chat_id}
}

# ─── Helpers HTML ─────────────────────────────────────────────────────────────
def b(t):    return f"<b>{t}</b>"
def code(t): return f"<code>{t}</code>"
def i(t):    return f"<i>{t}</i>"
def esc(t):  return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def now():   return datetime.now().strftime("%d/%m/%Y %H:%M")

def country_flag(country):  return COUNTRY_INFO.get(country, {}).get("flag", "🌍")
def country_label(country): return COUNTRY_INFO.get(country, {}).get("label", country)
def country_display(country): return f"{country_flag(country)} {country_label(country)}"

# Préfixes internationaux par code pays HeroSMS
COUNTRY_DIALCODE = {
    COUNTRY_FR: "33",
    COUNTRY_US: "1",
}

def format_number(number: str, country: str) -> str:
    """
    Formate un numéro brut HeroSMS en format international +XX XXXX...
    Ex: '33751495320' → '+33 751 495 320'
         '15752512995' → '+1 575 251 2995'
    """
    num = str(number).strip()
    dialcode = COUNTRY_DIALCODE.get(country, "")

    # Retirer le préfixe si déjà présent (ex: '33...' pour France, '1...' pour US)
    if dialcode and num.startswith(dialcode):
        local = num[len(dialcode):]
    else:
        local = num

    if not dialcode:
        return f"+{num}"

    # Formatage par blocs selon le pays
    if country == COUNTRY_FR:
        # +33 6 12 34 56 78  (groupes de 2 après le premier chiffre)
        if len(local) == 9:
            formatted = f"{local[0]} {local[1:3]} {local[3:5]} {local[5:7]} {local[7:9]}"
        else:
            formatted = local
        return f"+{dialcode} {formatted}"
    elif country == COUNTRY_US:
        # +1 (575) 251-2995
        if len(local) == 10:
            formatted = f"({local[0:3]}) {local[3:6]}-{local[6:10]}"
        else:
            formatted = local
        return f"+{dialcode} {formatted}"
    else:
        return f"+{dialcode} {local}"

# ─── Helpers Timers ───────────────────────────────────────────────────────────
def get_elapsed_sec(uid) -> int:
    """Secondes écoulées depuis l'attribution du numéro."""
    start = bot_state["number_start_time"].get(uid)
    if not start:
        return 0
    return int((datetime.now() - start).total_seconds())

def get_remaining_sec(uid) -> int:
    """Secondes restantes avant expiration automatique (1h)."""
    return max(0, NUMBER_LIFETIME_SEC - get_elapsed_sec(uid))

def get_lockout_remaining(uid) -> int:
    """Secondes restantes avant de pouvoir libérer (120s lock)."""
    return max(0, RELEASE_LOCKOUT_SEC - get_elapsed_sec(uid))

def format_countdown(seconds: int) -> str:
    """Formate un nombre de secondes en MM:SS ou HH:MM:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def start_number_timer(uid):
    """Enregistre l'heure de départ du compteur pour un agent."""
    bot_state["number_start_time"][uid] = datetime.now()

def clear_number_timer(uid):
    """Supprime le timer d'un agent (numéro libéré)."""
    bot_state["number_start_time"].pop(uid, None)
    task = bot_state["timer_tasks"].pop(uid, None)
    if task and not task.done():
        task.cancel()
    refresh_task = bot_state["refresh_tasks"].pop(uid, None)
    if refresh_task and not refresh_task.done():
        refresh_task.cancel()
    bot_state["panel_message_ids"].pop(uid, None)
    bot_state["panel_chat_ids"].pop(uid, None)

def get_user_country(uid):
    """Retourne le pays choisi par l'utilisateur, France par défaut."""
    return bot_state["user_country"].get(uid, COUNTRY_FR)

def set_user_country(uid, country):
    bot_state["user_country"][uid] = country

# ─── Accès ────────────────────────────────────────────────────────────────────
def is_main_admin(uid):   return uid == Config.ADMIN_ID
def is_sub_admin(uid):    return um.is_sub_admin(uid)
def is_admin(uid):        return is_main_admin(uid) or is_sub_admin(uid)
def is_authorized(uid):   return um.is_authorized(uid) or um.is_sub_admin(uid) or is_admin(uid)
def can_request(uid):     return is_authorized(uid) and not um.is_restricted(uid)
def veille_active(uid):   return bot_state["veille"] and not is_admin(uid)

# ─── Historique ───────────────────────────────────────────────────────────────
def add_history(uid, user_name, number, rental_id, price, operator, country=COUNTRY_FR, status="en_attente"):
    for e in bot_state["history"]:
        if e["rental_id"] == rental_id:
            e["status"] = status
            if status == "accepte":              e["accepted_at"] = now()
            elif status in ("libere","decline"): e["ended_at"]    = now()
            return
    bot_state["history"].append({
        "uid": uid, "user_name": user_name, "number": number,
        "rental_id": rental_id, "price": price, "operator": operator,
        "country": country,
        "status": status, "requested_at": now(), "accepted_at": None, "ended_at": None,
    })

def slabel(s):
    return {"en_attente":"⏳ En attente","accepte":"✅ Actif",
            "libere":"🔚 Libéré","decline":"❌ Décliné"}.get(s, s)

# ─── Suivi des prix (polling) ─────────────────────────────────────────────────
async def price_watcher(app):
    """
    Tâche de fond : actualise les prix FR et USA toutes les PRICE_POLL_MINUTES minutes.
    Notifie l'admin principal si le meilleur prix d'un pays a changé.
    """
    last_best = {COUNTRY_FR: None, COUNTRY_US: None}
    while True:
        for country in (COUNTRY_FR, COUNTRY_US):
            flag = country_flag(country)
            label = country_label(country)
            try:
                prices = await sms_client.get_prices(Config.SERVICE, country)
                if prices:
                    bot_state["price_cache"][country]       = prices
                    bot_state["price_last_update"][country] = now()
                    best = prices[0]["price"]
                    if last_best[country] is not None and best != last_best[country]:
                        direction = "📉 baissé" if best < last_best[country] else "📈 monté"
                        await app.bot.send_message(
                            chat_id=Config.ADMIN_ID,
                            text=(
                                f"🔔 {b('Changement de prix détecté !')}\n\n"
                                f"{flag} Pays : {b(label)}\n"
                                f"Service : {b(Config.SERVICE.upper())}\n"
                                f"Le prix a {direction} :\n"
                                f"  Avant : {b(f'{last_best[country]:.4f}')} $\n"
                                f"  Maintenant : {b(f'{best:.4f}')} $\n\n"
                                f"🕐 {now()}"
                            ),
                            parse_mode="HTML"
                        )
                    last_best[country] = best
            except Exception as e:
                logger.warning(f"Erreur price_watcher [{label}]: {e}")
        await asyncio.sleep(Config.PRICE_POLL_MINUTES * 60)

# ─── Tâche de fond : expiration automatique du numéro après 1h ───────────────
async def number_expiry_task(uid: int, app):
    """
    Lance un compte à rebours de NUMBER_LIFETIME_SEC (1h).
    - Envoie une alerte à 10 min restantes.
    - Libère automatiquement le numéro à l'expiration et notifie l'agent + admins.
    """
    try:
        # Alerte à 10 minutes restantes
        alert_at = NUMBER_LIFETIME_SEC - 600
        if alert_at > 0:
            await asyncio.sleep(alert_at)
            if uid in bot_state["active_numbers"]:
                d    = bot_state["active_numbers"][uid]
                flag = country_flag(d.get("country", COUNTRY_FR))
                try:
                    await app.bot.send_message(
                        chat_id=uid,
                        text=(
                            f"⚠️ {b('Attention — 10 minutes restantes !')}\n\n"
                            f"{flag} Votre numéro {code(format_number(d['number'], d.get('country', COUNTRY_FR)))} expire dans {b('10:00')}.\n"
                            f"Il sera libéré automatiquement à l'expiration."
                        ),
                        parse_mode="HTML")
                except: pass
            await asyncio.sleep(600)
        else:
            await asyncio.sleep(NUMBER_LIFETIME_SEC)

        # Expiration : libérer le numéro
        if uid not in bot_state["active_numbers"]:
            return  # Déjà libéré manuellement

        d       = bot_state["active_numbers"].pop(uid)
        country = d.get("country", COUNTRY_FR)
        flag    = country_flag(country)
        clear_number_timer(uid)
        add_history(uid, d["user_name"], d["number"], d["rental_id"],
                    d["price"], d["operator"], country, "libere")
        wh.unregister_sms_callback(d["rental_id"])  # expiry
        try: await sms_client.cancel_number(d["rental_id"])
        except Exception as e: logger.warning(f"Annulation HeroSMS (expiry): {e}")

        # Notifier l'agent
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=(
                    f"🔚 {b('Numéro expiré automatiquement')}\n\n"
                    f"{flag} {code(format_number(d['number'], country))} — durée maximale (1h) atteinte.\n"
                    f"Vous pouvez demander un nouveau numéro."
                ),
                parse_mode="HTML")
        except: pass

        # Notifier admin principal
        try:
            await app.bot.send_message(
                chat_id=Config.ADMIN_ID,
                text=(
                    f"🕐 {b('Expiration automatique')}\n\n"
                    f"👤 {b(esc(d['user_name']))}\n"
                    f"{flag} {code(format_number(d['number'], country))} — libéré après 1h.\n"
                    f"🕐 {now()}"
                ),
                parse_mode="HTML")
        except: pass

    except asyncio.CancelledError:
        pass  # Tâche annulée proprement (libération manuelle)

# ─── Tâche de fond : rafraîchissement automatique du panneau toutes les 30s ──
async def panel_refresh_task(uid: int, app):
    """
    Rafraîchit automatiquement le panneau OTP de l'agent toutes les 30 secondes
    pour mettre à jour le compte à rebours en temps réel.
    S'arrête si le numéro est libéré.
    """
    try:
        await asyncio.sleep(30)  # Premier refresh après 30s
        while uid in bot_state["active_numbers"]:
            msg_id  = bot_state["panel_message_ids"].get(uid)
            chat_id = bot_state["panel_chat_ids"].get(uid)
            if msg_id and chat_id:
                try:
                    d         = bot_state["active_numbers"][uid]
                    number    = d["number"]
                    rental_id = d["rental_id"]
                    price     = d.get("price", 0)
                    accepted  = d.get("accepted_at", now())
                    country   = d.get("country", COUNTRY_FR)
                    flag      = country_flag(country)

                    remaining = get_remaining_sec(uid)
                    lockout   = get_lockout_remaining(uid)
                    countdown = format_countdown(remaining)

                    if remaining <= 600:   timer_icon = "🔴"
                    elif remaining <= 1800: timer_icon = "🟡"
                    else:                   timer_icon = "🟢"

                    otp_text  = ""
                    has_code  = False
                    try:
                        sms = await sms_client.get_sms(rental_id)
                        if sms:
                            has_code = True
                            otp_text = (
                                f"\n━━━━━━━━━━━━━━━━━━\n"
                                f"🔑 {b('Code OTP reçu !')}\n"
                                f"   Code : {code(esc(sms['code']))}\n"
                                f"   📨 {i(esc(sms['message']))}"
                            )
                        else:
                            otp_text = f"\n━━━━━━━━━━━━━━━━━━\n⏳ {i('En attente du SMS...')} Rafraîchissez dans quelques secondes."
                    except Exception:
                        otp_text = f"\n━━━━━━━━━━━━━━━━━━\n⏳ {i('En attente du SMS...')}"

                    text = (
                        f"🟢 {b('Votre numéro actif')}\n\n"
                        f"{flag} Numéro : {code(format_number(number, country))}\n"
                        f"💲 Prix : {b(f'{price:.4f}')} $\n"
                        f"🕐 Actif depuis : {accepted}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{timer_icon} {b('Temps restant :')} {code(countdown)}\n"
                    )
                    if lockout > 0:
                        text += (
                            f"⏳ {b(f'Disponible dans {format_countdown(lockout)}')}\n"
                            f"{i('HeroSMS nécessite 2 min avant de libérer ou changer un numéro.')}\n"
                        )
                    text += otp_text

                    can_act = lockout == 0
                    rows = []
                    if can_act:
                        if has_code:
                            rows.append([
                                InlineKeyboardButton("🔄 Rafraîchir",    callback_data="voir_otp"),
                                InlineKeyboardButton("📩 Nouveau code",  callback_data="nouveau_code"),
                            ])
                            rows.append([InlineKeyboardButton("🔁 Changer numéro", callback_data="renouveler_numero")])
                        else:
                            rows.append([InlineKeyboardButton("🔄 Rafraîchir OTP", callback_data="voir_otp")])
                            rows.append([InlineKeyboardButton("🔁 Changer numéro", callback_data="renouveler_numero")])
                        rows.append([InlineKeyboardButton("🔚 Libérer le numéro", callback_data="liberer_numero")])
                    else:
                        rows.append([InlineKeyboardButton("🔄 Rafraîchir OTP", callback_data="voir_otp")])
                    rows.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])

                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(rows),
                    )
                except Exception as e:
                    logger.debug(f"panel_refresh_task [{uid}]: {e}")
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass  # Annulée proprement lors de la libération

# ─── Récupérer les prix (avec cache) ──────────────────────────────────────────
async def get_prices_cached(country=COUNTRY_FR, force=False) -> list[dict]:
    """Retourne les prix depuis le cache ou les rafraîchit pour le pays donné."""
    if force or not bot_state["price_cache"][country]:
        try:
            prices = await sms_client.get_prices(Config.SERVICE, country)
            if prices:
                bot_state["price_cache"][country]       = prices
                bot_state["price_last_update"][country] = now()
        except Exception as e:
            logger.warning(f"Erreur récupération prix [{country_label(country)}]: {e}")
    return bot_state["price_cache"][country]

# ─── Menu ─────────────────────────────────────────────────────────────────────
def build_main_keyboard(uid):
    user_country = get_user_country(uid)
    flag         = country_flag(user_country)
    label        = country_label(user_country)

    # Bouton capacité : libellé différent selon le rôle
    if is_main_admin(uid):
        cap_label = "💰 Solde & Capacité"
    else:
        cap_label = "📦 Numéros disponibles"

    # Bouton sélection pays
    other_country = COUNTRY_US if user_country == COUNTRY_FR else COUNTRY_FR
    other_flag    = country_flag(other_country)
    other_label   = country_label(other_country)

    buttons = [
        # Sélection du pays — toujours visible en premier
        [
            InlineKeyboardButton(f"{'✅ ' if user_country == COUNTRY_FR else ''}{country_flag(COUNTRY_FR)} France",
                                 callback_data=f"set_country_{COUNTRY_FR}"),
            InlineKeyboardButton(f"{'✅ ' if user_country == COUNTRY_US else ''}{country_flag(COUNTRY_US)} USA",
                                 callback_data=f"set_country_{COUNTRY_US}"),
        ],
        [InlineKeyboardButton(cap_label, callback_data="capacite")],
        [InlineKeyboardButton(f"{flag} Choisir un numéro {label}", callback_data="choisir_prix")],
    ]

    # Si l'agent a un numéro actif, afficher le panneau directement
    if uid in bot_state["active_numbers"]:
        num         = bot_state["active_numbers"][uid]["number"]
        num_country = bot_state["active_numbers"][uid].get("country", COUNTRY_FR)
        num_flag    = country_flag(num_country)
        buttons.insert(3, [InlineKeyboardButton(f"🟢 Mon numéro : {num_flag} {format_number(num, num_country)}", callback_data="voir_otp")])

    buttons.append([InlineKeyboardButton("📜 Mon historique", callback_data="mon_historique")])

    if um.is_sub_admin(uid):
        buttons.append([
            InlineKeyboardButton("📋 Demandes numéros",      callback_data="admin_pending"),
            InlineKeyboardButton("📊 Historique global",     callback_data="admin_historique"),
        ])
        buttons.append([InlineKeyboardButton("👥 Gérer accès agents", callback_data="sub_acces")])
        buttons.append([InlineKeyboardButton("⚙️ Système & Outils",   callback_data="admin_systeme")])

    if is_main_admin(uid):
        label_veille = "☀️ Désactiver veille" if bot_state["veille"] else "🌙 Activer veille"
        buttons.append([InlineKeyboardButton(label_veille, callback_data="toggle_veille")])
        buttons.append([
            InlineKeyboardButton("📋 Demandes numéros",      callback_data="admin_pending"),
            InlineKeyboardButton("📊 Historique global",     callback_data="admin_historique"),
        ])
        buttons.append([
            InlineKeyboardButton("👥 Agents & Stats",        callback_data="admin_agents"),
            InlineKeyboardButton("💲 Suivi des prix",        callback_data="admin_prix"),
        ])
        buttons.append([
            InlineKeyboardButton("🛡️ Gérer les admins",     callback_data="admin_admins"),
            InlineKeyboardButton("💳 Recharger le compte",   callback_data="admin_recharge"),
        ])
        buttons.append([InlineKeyboardButton("⚙️ Système & Outils",   callback_data="admin_systeme")])
    return InlineKeyboardMarkup(buttons)

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if um.is_banned(uid):
        await update.message.reply_text("🚫 Vous avez été banni. Contactez l'administrateur.")
        return
    if not is_authorized(uid):
        await update.message.reply_text(
            f"⛔ {b('Accès non autorisé.')}\n\nPour demander l'accès :\n👉 @{Config.ENROLL_BOT_USERNAME}",
            parse_mode="HTML")
        return
    if veille_active(uid):
        await update.message.reply_text("🌙 Le bot est en mode veille. Réessayez plus tard.")
        return
    name    = esc(update.effective_user.first_name)
    role    = " [ADMIN PRINCIPAL]" if is_main_admin(uid) else (" [ADMIN]" if is_admin(uid) else (" [RESTREINT]" if um.is_restricted(uid) else ""))
    country = get_user_country(uid)
    await update.message.reply_text(
        f"👋 Bonjour {b(name)}{role} !\n\n"
        f"🤖 {b('Bot Numéros Virtuels')} — Agence\n"
        f"🇫🇷 France & 🇺🇸 USA — {i('Choisissez votre pays ci-dessous')}\n\n"
        f"Pays actuel : {b(country_display(country))}\n\n"
        f"Utilisez les boutons ci-dessous.",
        parse_mode="HTML", reply_markup=build_main_keyboard(uid))

# ─── Handler boutons ──────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if um.is_banned(uid):
        await query.edit_message_text("🚫 Vous avez été banni.")
        return
    if not is_authorized(uid):
        await query.edit_message_text(f"⛔ Accès refusé. → @{Config.ENROLL_BOT_USERNAME}")
        return
    if veille_active(uid) and data not in ("menu",):
        await query.edit_message_text("🌙 Le bot est en mode veille.")
        return

    # ── Changement de pays ──
    if data.startswith("set_country_"):
        chosen = data.split("_")[2]
        if chosen in (COUNTRY_FR, COUNTRY_US):
            set_user_country(uid, chosen)
            flag  = country_flag(chosen)
            label = country_label(chosen)
            await query.edit_message_text(
                f"{flag} {b(f'Pays sélectionné : {label}')}\n\n"
                f"Vous pouvez maintenant commander un numéro {label}.",
                parse_mode="HTML",
                reply_markup=build_main_keyboard(uid))
        return

    # ── Capacité ──
    if   data == "capacite":          await handle_capacite(query, uid)
    elif data == "solde":             await handle_capacite(query, uid)
    # ── Agent ──
    elif data == "choisir_prix":      await handle_choisir_prix(query, uid)
    elif data == "voir_otp":          await handle_voir_otp(query, uid, context)
    elif data == "nouveau_code":      await handle_nouveau_code(query, uid, context)
    elif data == "liberer_numero":    await handle_liberer(query, uid)
    elif data == "mon_historique":    await handle_mon_historique(query, uid)
    elif data == "renouveler_numero": await handle_renouveler(query, uid, context)

    # ── Choix d'un prix spécifique ──
    elif data.startswith("pick_"):
        parts = data.split("_")
        idx = int(parts[1])
        await handle_demander_numero(query, uid, context, price_index=idx)

    # ── Sous-admin ──
    elif data == "sub_acces"                         and um.is_sub_admin(uid): await handle_sub_acces(query)
    elif data == "sub_voir_pending"                  and um.is_sub_admin(uid): await handle_sub_voir_pending(query)
    elif data == "sub_voir_authorized"               and um.is_sub_admin(uid): await handle_sub_voir_authorized(query)
    elif data.startswith("sub_approve_")             and um.is_sub_admin(uid): await sub_approve(query, int(data.split("_")[2]), context)
    elif data.startswith("sub_reject_")              and um.is_sub_admin(uid): await sub_reject(query, int(data.split("_")[2]), context)
    elif data.startswith("sub_rm_")                  and um.is_sub_admin(uid): await sub_rm(query, int(data.split("_")[2]), context)

    # ── Admin principal uniquement ──
    elif data == "toggle_veille" and is_main_admin(uid):
        bot_state["veille"] = not bot_state["veille"]
        msg = f"🌙 Mode veille {b('activé')}." if bot_state["veille"] else f"☀️ Mode veille {b('désactivé')}."
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=build_main_keyboard(uid))

    elif data == "admin_pending"    and is_admin(uid):       await handle_admin_pending(query)
    elif data == "admin_historique" and is_admin(uid):       await handle_admin_historique(query)
    elif data == "admin_agents"     and is_main_admin(uid):  await handle_admin_agents(query)
    elif data == "admin_prix"       and is_main_admin(uid):  await handle_admin_prix(query)
    elif data == "admin_admins"     and is_main_admin(uid):  await handle_admin_admins(query)
    elif data == "admin_recharge"   and is_main_admin(uid):  await handle_admin_recharge(query)

    # ── Système & Outils (admin + sous-admin) ──
    elif data == "admin_systeme"          and is_admin(uid): await handle_admin_systeme(query, uid)
    elif data == "sys_reload_proxy"       and is_admin(uid): await handle_sys_reload_proxy(query, uid)
    elif data == "sys_test_conn"          and is_admin(uid): await handle_sys_test_conn(query, uid)
    elif data == "sys_restart_confirm"    and is_admin(uid): await handle_sys_restart_confirm(query, uid)
    elif data == "sys_restart_do"         and is_admin(uid): await handle_sys_restart_do(query, uid, context)

    elif data.startswith("numok_")  and is_admin(uid): await handle_accept(query, int(data.split("_")[1]), context)
    elif data.startswith("numno_")  and is_admin(uid): await handle_decline(query, int(data.split("_")[1]), context)
    elif data.startswith("admin_liberer_") and is_admin(uid): await handle_decline(query, int(data.split("_")[2]), context)

    # racheter_{target_uid}_{number_tronqué}_{operator}
    elif data.startswith("racheter_"):
        parts      = data.split("_", 3)
        target_uid = int(parts[1])
        number     = parts[2]
        operator   = parts[3] if len(parts) > 3 else "any"
        if target_uid == uid or is_admin(uid):
            await handle_racheter(query, uid, target_uid, number, operator, context)
        else:
            await query.answer("⛔ Non autorisé.", show_alert=True)

    elif data.startswith("rm_")          and is_admin(uid):      await action_rm(query, uid, data, context)
    elif data.startswith("restrict_")    and is_admin(uid):      await action_restrict(query, uid, data, context)
    elif data.startswith("unrestrict_")  and is_admin(uid):      await action_unrestrict(query, uid, data, context)
    elif data.startswith("ban_")         and is_admin(uid):      await action_ban(query, uid, data, context)
    elif data.startswith("agent_")       and is_admin(uid):      await handle_agent_detail(query, int(data.split("_")[1]))
    elif data.startswith("promote_")     and is_main_admin(uid): await action_promote(query, uid, data, context)
    elif data.startswith("revoke_adm_")  and is_main_admin(uid): await action_revoke_admin(query, data, context)

    elif data == "refresh_prix":
        country = get_user_country(uid)
        bot_state["price_cache"][COUNTRY_FR] = []
        bot_state["price_cache"][COUNTRY_US] = []
        await handle_admin_prix(query)

    elif data == "menu":
        country = get_user_country(uid)
        await query.edit_message_text(
            f"🏠 {b('Menu principal')}\n"
            f"Pays actuel : {b(country_display(country))}",
            parse_mode="HTML", reply_markup=build_main_keyboard(uid))

# ─── Capacité ────────────────────────────────────────────────────────────────
async def handle_capacite(query, uid=None):
    if uid is None:
        uid = query.from_user.id

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Actualiser", callback_data="capacite"),
        InlineKeyboardButton("🔙 Retour",     callback_data="menu"),
    ]])

    try:
        balance    = await sms_client.get_balance()
        # Récupérer prix des deux pays
        prices_fr  = await get_prices_cached(COUNTRY_FR)
        prices_us  = await get_prices_cached(COUNTRY_US)
        best_fr    = prices_fr[0]["price"] if prices_fr else Config.PRICE_PER_NUMBER
        best_us    = prices_us[0]["price"] if prices_us else Config.PRICE_PER_NUMBER
        nb_fr      = int(balance / best_fr) if best_fr > 0 else 0
        nb_us      = int(balance / best_us) if best_us > 0 else 0
        nb_actifs  = len(bot_state["active_numbers"])
        nb_attente = len(bot_state["pending_numbers"])

        def alerte_stock(nb, flag):
            if nb == 0:   return f"\n🔴 {flag} {b('Plus de numéros disponibles !')}"
            elif nb <= 5: return f"\n🟡 {flag} {b('Stock faible')} — Seulement {b(str(nb))} numéro(s)"
            else:         return f"\n🟢 {flag} {b(str(nb))} numéro(s) disponible(s)"

        if is_main_admin(uid):
            nb_agents = len(um.get_authorized_ids())
            text = (
                f"💰 {b('Solde & Capacité')}\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💳 Solde : {b(f'{balance:.4f}')} $\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🇫🇷 Meilleur prix FR : {b(f'{best_fr:.4f}')} $ — {b(str(nb_fr))} numéro(s)\n"
                f"🇺🇸 Meilleur prix US : {b(f'{best_us:.4f}')} $ — {b(str(nb_us))} numéro(s)\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 {b('État en temps réel :')}\n"
                f"   ├ Numéros actifs : {b(str(nb_actifs))}\n"
                f"   ├ Demandes en attente : {b(str(nb_attente))}\n"
                f"   └ Agents autorisés : {b(str(nb_agents))}"
                f"{alerte_stock(nb_fr, '🇫🇷')}"
                f"{alerte_stock(nb_us, '🇺🇸')}"
            )
        else:
            text = (
                f"📦 {b('Numéros disponibles')}\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🇫🇷 Numéros FR : {b(str(nb_fr))}\n"
                f"🇺🇸 Numéros US : {b(str(nb_us))}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 {b('État en temps réel :')}\n"
                f"   ├ Numéros actifs : {b(str(nb_actifs))}\n"
                f"   └ Demandes en attente : {b(str(nb_attente))}"
                f"{alerte_stock(nb_fr, '🇫🇷')}"
                f"{alerte_stock(nb_us, '🇺🇸')}"
            )

    except Exception as e:
        text = f"❌ Erreur : {code(esc(str(e)))}"

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

async def handle_solde(query):
    await handle_capacite(query)

# ─── Choisir parmi les prix ───────────────────────────────────────────────────
async def handle_choisir_prix(query, uid):
    kb_back  = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])
    country  = get_user_country(uid)
    flag     = country_flag(country)
    label    = country_label(country)

    if not can_request(uid):
        info   = um.get_user_info(uid) or {}
        reason = esc(info.get("restrict_reason",""))
        await query.edit_message_text(
            f"⚠️ {b('Accès restreint')}\n\nVous ne pouvez pas demander de numéro.\n"
            f"{f'Raison : {i(reason)}' if reason else ''}",
            parse_mode="HTML", reply_markup=kb_back)
        return

    if uid in bot_state["active_numbers"]:
        num = bot_state["active_numbers"][uid]["number"]
        await query.edit_message_text(
            f"⚠️ Vous avez déjà le numéro actif : {code(esc(num))}\nLibérez-le d'abord.",
            parse_mode="HTML", reply_markup=kb_back)
        return

    if uid in bot_state["pending_numbers"]:
        await query.edit_message_text("⏳ Votre demande est déjà en attente.", reply_markup=kb_back)
        return

    await query.edit_message_text(f"⏳ Récupération des prix {flag} {label} en cours...", reply_markup=kb_back)
    prices = await get_prices_cached(country, force=True)

    if not prices:
        await query.edit_message_text(
            f"⚠️ {b('Prix indisponibles')}\n\nImpossible de récupérer les tarifs HeroSMS pour {flag} {label}.\n"
            f"Le numéro sera acheté au prix par défaut ({b(str(Config.PRICE_PER_NUMBER))} $).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Continuer au prix par défaut", callback_data="pick_0")],
                [InlineKeyboardButton("🔙 Retour", callback_data="menu")],
            ]))
        bot_state["price_cache"][country] = [{"operator": "any", "price": Config.PRICE_PER_NUMBER, "count": 0}]
        return

    medals  = ["🥇", "🥈", "🥉"]
    last_up = bot_state["price_last_update"][country]
    text    = (
        f"{flag} {b(f'Choisissez votre offre {label}')}\n"
        f"Service : {b(Config.SERVICE.upper())} | Pays : {label}\n"
        f"🕐 Mis à jour : {last_up}\n\n"
    )
    balance = None
    try:    balance = await sms_client.get_balance()
    except: pass

    buttons = []
    for idx, p in enumerate(prices):
        op    = esc(p["operator"])
        price = p["price"]
        count = p["count"]
        medal = medals[idx] if idx < 3 else "•"

        if balance is not None and balance < price:
            dispo = "🔴 Solde insuffisant"
        else:
            dispo = f"📦 {count} dispo" if count else "📦 Disponible"

        text += (
            f"{medal} {b(f'{price:.4f} $')} — opérateur {code(op)}\n"
            f"   {dispo}\n\n"
        )
        label_btn = f"{medal} {price:.4f} $ — {count} dispo"
        if balance is None or balance >= price:
            buttons.append([InlineKeyboardButton(label_btn, callback_data=f"pick_{idx}")])
        else:
            buttons.append([InlineKeyboardButton(f"🔴 {price:.4f} $ (solde insuffisant)", callback_data="solde")])

    buttons.append([
        InlineKeyboardButton("🔄 Rafraîchir les prix", callback_data="choisir_prix"),
        InlineKeyboardButton("🔙 Retour",              callback_data="menu"),
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Demander numéro avec prix choisi ─────────────────────────────────────────
async def handle_demander_numero(query, uid, context, price_index: int = 0):
    kb_back  = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])
    country  = get_user_country(uid)
    prices   = bot_state["price_cache"][country]

    if not prices or price_index >= len(prices):
        await query.edit_message_text("⚠️ Prix non disponible. Réessayez.", reply_markup=kb_back)
        return

    chosen   = prices[price_index]
    price    = chosen["price"]
    operator = chosen.get("operator", "any")
    flag     = country_flag(country)
    label    = country_label(country)

    try:
        balance = await sms_client.get_balance()
        if balance < price:
            await query.edit_message_text(
                f"🔴 {b('Solde insuffisant !')}\n\n"
                f"Solde : {b(f'{balance:.4f}')} $ | Prix : {b(f'{price:.4f}')} $",
                parse_mode="HTML", reply_markup=kb_back)
            return
    except Exception as e:
        await query.edit_message_text(f"❌ Erreur vérification solde : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)
        return

    try:
        result    = await sms_client.get_number(Config.SERVICE, country, operator)
        number    = result["number"]
        rental_id = result["id"]
        user_name = query.from_user.first_name

        d = {
            "number": number, "rental_id": rental_id, "user_name": user_name,
            "price": price, "operator": operator, "country": country,
            "accepted_at": now(), "requested_at": now(),
        }
        bot_state["active_numbers"][uid] = d
        add_history(uid, user_name, number, rental_id, price, operator, country, "accepte")
        um.increment_achats(uid, number)

        # ── Démarrer le compte à rebours 1h ──
        start_number_timer(uid)
        task = asyncio.create_task(number_expiry_task(uid, context.application))
        bot_state["timer_tasks"][uid] = task

        # ── Enregistrer le callback webhook SMS ──
        async def _on_sms_webhook(sms_payload, _uid=uid, _app=context.application):
            """Appelé instantanément par le webhook HeroSMS dès réception du SMS."""
            if _uid not in bot_state["active_numbers"]:
                return
            d2 = bot_state["active_numbers"][_uid]
            d2["webhook_sms"] = sms_payload
            # Notifier l'agent immédiatement via Telegram
            msg_id  = bot_state["panel_message_ids"].get(_uid)
            chat_id = bot_state["panel_chat_ids"].get(_uid)
            sms_code = sms_payload.get("code", "?")
            sms_msg  = sms_payload.get("message", sms_code)
            notif = (
                "\U0001f514 " + b("Code OTP recu en temps reel !") + "\n\n"
                "\U0001f511 Code : " + code(esc(str(sms_code))) + "\n"
                "\U0001f4e8 " + i(esc(str(sms_msg)))
            )
            try:
                await _app.bot.send_message(chat_id=_uid, text=notif, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Webhook notif failed for uid={_uid}: {e}")
            # Mettre a jour le panneau actif
            if msg_id and chat_id:
                try:
                    from bot import send_otp_panel
                except Exception:
                    pass
                try:
                    rows = [
                        [InlineKeyboardButton("\U0001f504 Rafraichir", callback_data="voir_otp"),
                         InlineKeyboardButton("\U0001f4e9 Nouveau code", callback_data="nouveau_code")],
                        [InlineKeyboardButton("\U0001f501 Changer numero", callback_data="renouveler_numero")],
                        [InlineKeyboardButton("\U0001f51a Liberer le numero", callback_data="liberer_numero")],
                        [InlineKeyboardButton("\U0001f519 Menu", callback_data="menu")],
                    ]
                    await _app.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=notif + "\n\n" + i("Panneau mis a jour automatiquement."),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(rows))
                except Exception:
                    pass

        wh.register_sms_callback(rental_id, _on_sms_webhook)

        stats      = um.get_user_stats(uid)
        admin_text = (
            f"📡 {b('Numéro attribué en temps réel')}\n\n"
            f"👤 {b(esc(user_name))} (ID: {code(str(uid))})\n"
            f"{flag} Pays : {b(label)} | Numéro : {code(format_number(number, country))}\n"
            f"💲 Prix : {b(f'{price:.4f}')} $ — opérateur {code(esc(operator))}\n"
            f"🕐 {now()} | 📦 Total achats : {b(str(stats['total_achats']))}\n\n"
            f"ℹ️ {i('Numéro actif. Vous pouvez suivre via Demandes actives.')}"
        )
        try:
            await context.bot.send_message(
                chat_id=Config.ADMIN_ID,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔚 Libérer pour l'agent", callback_data=f"admin_liberer_{uid}"),
                ]]))
        except: pass
        sub_ids = um.get_sub_admin_ids() if hasattr(um, "get_sub_admin_ids") else []
        for sub_id in sub_ids:
            try:
                await context.bot.send_message(chat_id=sub_id, text=admin_text, parse_mode="HTML")
            except: pass

        await send_otp_panel(query, uid, context, edit=True)

    except Exception as e:
        await query.edit_message_text(f"❌ Erreur : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)


async def send_otp_panel(query, uid, context, edit=False):
    """Envoie ou édite le panneau numéro+OTP de l'agent avec boutons adaptés selon l'état."""
    if uid not in bot_state["active_numbers"]:
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        text = "⚠️ Aucun numéro actif."
        if edit:
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=uid, text=text, reply_markup=kb)
        return

    d         = bot_state["active_numbers"][uid]
    number    = d["number"]
    rental_id = d["rental_id"]
    price     = d.get("price", 0)
    accepted  = d.get("accepted_at", now())
    country   = d.get("country", COUNTRY_FR)
    flag      = country_flag(country)

    # ── Timers ──
    remaining  = get_remaining_sec(uid)
    lockout    = get_lockout_remaining(uid)
    countdown  = format_countdown(remaining)

    # Couleur du compte à rebours selon urgence
    if remaining <= 600:
        timer_icon = "🔴"
    elif remaining <= 1800:
        timer_icon = "🟡"
    else:
        timer_icon = "🟢"

    otp_text = ""
    has_code = False
    try:
        sms = await sms_client.get_sms(rental_id)
        if sms:
            has_code = True
            otp_text = (
                f"\n━━━━━━━━━━━━━━━━━━\n"
                f"🔑 {b('Code OTP reçu !')}\n"
                f"   Code : {code(esc(sms['code']))}\n"
                f"   📨 {i(esc(sms['message']))}"
            )
        else:
            otp_text = f"\n━━━━━━━━━━━━━━━━━━\n⏳ {i('En attente du SMS...')} Rafraîchissez dans quelques secondes."
    except Exception as e:
        otp_text = f"\n⚠️ Erreur SMS : {code(esc(str(e)))}"

    text = (
        f"🟢 {b('Votre numéro actif')}\n\n"
        f"{flag} Numéro : {code(format_number(number, country))}\n"
        f"💲 Prix : {b(f'{price:.4f}')} $\n"
        f"🕐 Actif depuis : {accepted}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{timer_icon} {b('Temps restant :')} {code(countdown)}\n"
    )

    if lockout > 0:
        text += (
            f"⏳ {b(f'Disponible dans {format_countdown(lockout)}')}\n"
            f"{i('HeroSMS nécessite 2 min avant de libérer ou changer un numéro.')}\n"
        )

    text += otp_text

    # ── Boutons — Libérer et Changer numéro masqués pendant les 120s de lockout ──
    can_act = lockout == 0  # Les actions sur HeroSMS ne sont disponibles qu'après 120s

    rows = []

    if can_act:
        # Après 120s : tous les boutons disponibles
        if has_code:
            rows.append([
                InlineKeyboardButton("🔄 Rafraîchir",      callback_data="voir_otp"),
                InlineKeyboardButton("📩 Nouveau code",     callback_data="nouveau_code"),
            ])
            rows.append([InlineKeyboardButton("🔁 Changer numéro", callback_data="renouveler_numero")])
        else:
            rows.append([InlineKeyboardButton("🔄 Rafraîchir OTP", callback_data="voir_otp")])
            rows.append([InlineKeyboardButton("🔁 Changer numéro", callback_data="renouveler_numero")])
        rows.append([InlineKeyboardButton("🔚 Libérer le numéro", callback_data="liberer_numero")])
    else:
        # Pendant les 120s : uniquement le rafraîchissement OTP, pas d'action HeroSMS
        rows.append([InlineKeyboardButton("🔄 Rafraîchir OTP", callback_data="voir_otp")])

    rows.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])

    kb = InlineKeyboardMarkup(rows)
    if edit:
        sent = await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        # Stocker l'ID du message pour le rafraîchissement automatique
        try:
            msg_id  = query.message.message_id
            chat_id = query.message.chat_id
            bot_state["panel_message_ids"][uid] = msg_id
            bot_state["panel_chat_ids"][uid]    = chat_id
        except Exception: pass
    else:
        sent = await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML", reply_markup=kb)
        # Stocker l'ID du message pour le rafraîchissement automatique
        try:
            bot_state["panel_message_ids"][uid] = sent.message_id
            bot_state["panel_chat_ids"][uid]    = uid
        except Exception: pass

    # ── Lancer la tâche de rafraîchissement auto si pas déjà active ──
    if uid not in bot_state["refresh_tasks"] or bot_state["refresh_tasks"][uid].done():
        refresh_task = asyncio.create_task(panel_refresh_task(uid, context.application))
        bot_state["refresh_tasks"][uid] = refresh_task

# ─── Admin Accepter / Décliner ────────────────────────────────────────────────
async def handle_accept(query, target_uid, context):
    if target_uid not in bot_state["active_numbers"]:
        await query.edit_message_text("⚠️ Aucun numéro actif pour cet agent.")
        return
    d = bot_state["active_numbers"][target_uid]
    await query.edit_message_text(
        f"✅ Numéro {code(format_number(d['number'], d.get('country', COUNTRY_FR)))} de {b(esc(d['user_name']))} — déjà actif.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_pending")]]))

async def handle_decline(query, target_uid, context):
    if target_uid not in bot_state["active_numbers"]:
        await query.edit_message_text("⚠️ Aucun numéro actif pour cet agent.")
        return
    d       = bot_state["active_numbers"].pop(target_uid)
    country = d.get("country", COUNTRY_FR)
    flag    = country_flag(country)
    clear_number_timer(target_uid)
    add_history(target_uid, d["user_name"], d["number"], d["rental_id"], d["price"], d["operator"], country, "libere")
    wh.unregister_sms_callback(d["rental_id"])
    try: await sms_client.cancel_number(d["rental_id"])
    except Exception as e: logger.warning(f"Annulation HeroSMS: {e}")
    await context.bot.send_message(
        chat_id=target_uid,
        text=f"⚠️ Votre numéro {flag} a été {b('libéré par l\'administrateur')}.\nContactez l'admin si nécessaire.",
        parse_mode="HTML")
    await query.edit_message_text(
        f"🔚 Numéro de {b(esc(d['user_name']))} libéré de force.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_pending")]]))

# ─── Voir OTP ─────────────────────────────────────────────────────────────────
async def handle_voir_otp(query, uid, context):
    await send_otp_panel(query, uid, context=context, edit=True)

# ─── Nouveau code ─────────────────────────────────────────────────────────────
async def handle_nouveau_code(query, uid, context):
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
    if uid not in bot_state["active_numbers"]:
        await query.edit_message_text("⚠️ Aucun numéro actif.", reply_markup=kb_back)
        return

    d         = bot_state["active_numbers"][uid]
    old_num   = d["number"]
    operator  = d.get("operator", "any")
    price     = d.get("price", Config.PRICE_PER_NUMBER)
    user_name = d["user_name"]
    country   = d.get("country", COUNTRY_FR)

    try:
        balance = await sms_client.get_balance()
        if balance < price:
            await query.edit_message_text(
                f"🔴 {b('Solde insuffisant pour racheter un code !')}\n\n"
                f"Solde : {b(f'{balance:.4f}')} $ | Prix : {b(f'{price:.4f}')} $",
                parse_mode="HTML", reply_markup=kb_back)
            return
    except Exception as e:
        await query.edit_message_text(f"❌ Erreur vérification solde : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)
        return

    await query.edit_message_text(
        f"🔄 Rachat d'un nouveau code pour {code(esc(old_num))}...", parse_mode="HTML")

    try:
        old_rental_id = d["rental_id"]
        wh.unregister_sms_callback(old_rental_id)  # nouveau code
        try: await sms_client.cancel_number(old_rental_id)
        except: pass

        result     = await sms_client.get_number(Config.SERVICE, country, operator)
        new_number = result["number"]
        rental_id  = result["id"]

        new_d = {
            "number":       new_number,
            "rental_id":    rental_id,
            "user_name":    user_name,
            "price":        price,
            "operator":     operator,
            "country":      country,
            "accepted_at":  now(),
            "requested_at": now(),
        }
        bot_state["active_numbers"][uid] = new_d
        add_history(uid, user_name, new_number, rental_id, price, operator, country, "accepte")
        um.increment_achats(uid, new_number)

        # ── Redémarrer le compte à rebours ──
        start_number_timer(uid)
        task = asyncio.create_task(number_expiry_task(uid, context.application))
        bot_state["timer_tasks"][uid] = task

        await send_otp_panel(query, uid, context, edit=True)

    except Exception as e:
        await query.edit_message_text(
            f"❌ Erreur rachat code : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)

# ─── Racheter un numéro depuis l'historique ───────────────────────────────────
async def handle_racheter(query, requester_uid, target_uid, number, operator, context):
    back_cb = "admin_historique" if is_admin(requester_uid) else "mon_historique"
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Historique", callback_data=back_cb)]])

    if target_uid in bot_state["active_numbers"]:
        num_actif = bot_state["active_numbers"][target_uid]["number"]
        await query.edit_message_text(
            f"⚠️ {b('Numéro déjà actif')}\n\n"
            f"L'agent a déjà le numéro {code(esc(num_actif))} en cours.\n"
            f"Il doit d'abord le libérer avant de racheter.",
            parse_mode="HTML", reply_markup=kb_back)
        return

    # Déterminer le pays depuis l'historique
    country = COUNTRY_FR
    for e in reversed(bot_state["history"]):
        if e["uid"] == target_uid and e["number"][-len(number):] == number:
            country = e.get("country", COUNTRY_FR)
            break

    prices = await get_prices_cached(country)
    price  = prices[0]["price"] if prices else Config.PRICE_PER_NUMBER

    try:
        balance = await sms_client.get_balance()
        if balance < price:
            await query.edit_message_text(
                f"🔴 {b('Solde insuffisant !')}\n\n"
                f"Solde : {b(f'{balance:.4f}')} $ | Prix : {b(f'{price:.4f}')} $",
                parse_mode="HTML", reply_markup=kb_back)
            return
    except Exception as e:
        await query.edit_message_text(f"❌ Erreur solde : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)
        return

    await query.edit_message_text(f"🔄 Rachat du numéro {code(esc(number))} en cours...", parse_mode="HTML")

    try:
        result     = await sms_client.get_number(Config.SERVICE, country, operator)
        new_number = result["number"]
        rental_id  = result["id"]

        info      = um.get_user_info(target_uid) or {}
        user_name = info.get("first_name", f"Agent {target_uid}")

        d = {
            "number":       new_number,
            "rental_id":    rental_id,
            "user_name":    user_name,
            "price":        price,
            "operator":     operator,
            "country":      country,
            "accepted_at":  now(),
            "requested_at": now(),
        }
        bot_state["active_numbers"][target_uid] = d
        add_history(target_uid, user_name, new_number, rental_id, price, operator, country, "accepte")
        um.increment_achats(target_uid, new_number)

        same = "✅ Même numéro récupéré !" if new_number == number else f"ℹ️ Numéro attribué : {code(esc(new_number))}"
        flag = country_flag(country)

        if requester_uid == target_uid:
            await send_otp_panel(query, target_uid, context, edit=True)
        else:
            await query.edit_message_text(
                f"🔁 {b('Rachat effectué')}\n\n"
                f"👤 Agent : {b(esc(user_name))}\n"
                f"{flag} {same}\n"
                f"💲 Prix : {b(f'{price:.4f}')} $\n"
                f"🕐 {now()}",
                parse_mode="HTML", reply_markup=kb_back)
            try:
                await send_otp_panel(query, target_uid, context, edit=False)
            except: pass

    except Exception as e:
        await query.edit_message_text(
            f"❌ Erreur rachat : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)

# ─── Libérer numéro ───────────────────────────────────────────────────────────
async def handle_liberer(query, uid):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
    if uid not in bot_state["active_numbers"]:
        await query.edit_message_text("⚠️ Pas de numéro actif à libérer.", reply_markup=kb)
        return

    # ── Vérification du délai de 120 secondes ──
    lockout = get_lockout_remaining(uid)
    if lockout > 0:
        await query.edit_message_text(
            f"🔒 {b('Libération temporairement bloquée')}\n\n"
            f"Pour éviter les abus, vous devez attendre {b(format_countdown(lockout))} avant de libérer ce numéro.\n\n"
            f"⏳ Rafraîchissez votre panneau dans quelques secondes.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Rafraîchir", callback_data="voir_otp")],
                [InlineKeyboardButton("🔙 Menu",       callback_data="menu")],
            ]))
        return

    d       = bot_state["active_numbers"].pop(uid)
    country = d.get("country", COUNTRY_FR)
    flag    = country_flag(country)
    clear_number_timer(uid)
    add_history(uid, d["user_name"], d["number"], d["rental_id"], d["price"], d["operator"], country, "libere")
    wh.unregister_sms_callback(d["rental_id"])  # liberer manuel
    try:
        await sms_client.cancel_number(d["rental_id"])
        text = (
            f"✅ {b('Numéro libéré')}\n\n"
            f"{flag} {code(format_number(d['number'], country))} — libéré à {now()}\n"
            f"Vous pouvez désormais demander un nouveau numéro."
        )
    except Exception as e:
        text = f"⚠️ Retiré localement — erreur HeroSMS : {code(esc(str(e)))}"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

# ─── Renouveler numéro ────────────────────────────────────────────────────────
async def handle_renouveler(query, uid, context):
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
    if uid not in bot_state["active_numbers"]:
        await query.edit_message_text("⚠️ Pas de numéro actif à renouveler.", reply_markup=kb_back)
        return

    # ── Vérification du délai de 120 secondes ──
    lockout = get_lockout_remaining(uid)
    if lockout > 0:
        await query.edit_message_text(
            f"🔒 {b('Renouvellement temporairement bloqué')}\n\n"
            f"Pour éviter les erreurs, vous devez attendre {b(format_countdown(lockout))} avant de changer de numéro.\n\n"
            f"⏳ Rafraîchissez votre panneau dans quelques secondes.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Rafraîchir", callback_data="voir_otp")],
                [InlineKeyboardButton("🔙 Menu",       callback_data="menu")],
            ]))
        return

    d            = bot_state["active_numbers"].pop(uid)
    old_price    = d.get("price", Config.PRICE_PER_NUMBER)
    old_operator = d.get("operator", "any")
    country      = d.get("country", COUNTRY_FR)
    clear_number_timer(uid)
    add_history(uid, d["user_name"], d["number"], d["rental_id"], d["price"], d["operator"], country, "libere")
    wh.unregister_sms_callback(d["rental_id"])  # renouvellement
    try: await sms_client.cancel_number(d["rental_id"])
    except Exception as e: logger.warning(f"Annulation renouvellement: {e}")

    try:
        result    = await sms_client.get_number(Config.SERVICE, country, old_operator)
        number    = result["number"]
        rental_id = result["id"]
        user_name = d["user_name"]

        new_d = {
            "number": number, "rental_id": rental_id, "user_name": user_name,
            "price": old_price, "operator": old_operator, "country": country,
            "accepted_at": now(), "requested_at": now(),
        }
        bot_state["active_numbers"][uid] = new_d
        add_history(uid, user_name, number, rental_id, old_price, old_operator, country, "accepte")
        um.increment_achats(uid, number)

        # ── Redémarrer le compte à rebours ──
        start_number_timer(uid)
        task = asyncio.create_task(number_expiry_task(uid, context.application))
        bot_state["timer_tasks"][uid] = task

        await send_otp_panel(query, uid, context, edit=True)
    except Exception as e:
        await query.edit_message_text(f"❌ Erreur renouvellement : {code(esc(str(e)))}", parse_mode="HTML", reply_markup=kb_back)

# ─── Historique personnel ─────────────────────────────────────────────────────
async def handle_mon_historique(query, uid):
    mes   = [e for e in bot_state["history"] if e["uid"] == uid]
    stats = um.get_user_stats(uid)
    text  = f"📜 {b('Mon historique')}\n\n"
    text += f"📊 Total acheté : {b(str(stats['total_achats']))} numéro(s)\n\n"

    has_active = uid in bot_state["active_numbers"]
    buttons    = []
    seen       = set()

    if not mes:
        text += "Aucun numéro demandé pour l'instant."
    else:
        for e in reversed(mes[-10:]):
            num       = e["number"]
            c_flag    = country_flag(e.get("country", COUNTRY_FR))
            price_str = f" | 💲{e.get('price',0):.4f}$" if e.get("price") else ""
            text += f"├ {c_flag} {code(format_number(num, e.get('country', COUNTRY_FR)))} — {slabel(e['status'])}{price_str}\n"
            text += f"│  🕐 {e['requested_at']}\n"
            if e["accepted_at"]: text += f"│  ✅ {e['accepted_at']}\n"
            if e["ended_at"]:    text += f"│  🔚 {e['ended_at']}\n"
            text += "│\n"

            if e["status"] in ("libere", "decline") and num not in seen:
                seen.add(num)
                op     = e.get("operator", "any")
                num_cb = num[-15:]
                lbl    = f"🔁 Racheter {num[-6:]}"
                if has_active:
                    lbl = f"⛔ {num[-6:]} (libérez d'abord)"
                    buttons.append([InlineKeyboardButton(lbl, callback_data="liberer_numero")])
                else:
                    cb = f"racheter_{uid}_{num_cb}_{op}"
                    buttons.append([InlineKeyboardButton(lbl, callback_data=cb)])

        text = text.rstrip("│\n")

    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Admin : Historique global ────────────────────────────────────────────────
async def handle_admin_historique(query):
    h       = bot_state["history"]
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])

    if not h:
        await query.edit_message_text(f"📊 {b('Historique global')}\n\nAucune activité.", parse_mode="HTML", reply_markup=kb_back)
        return

    total    = len(h)
    actifs   = sum(1 for e in h if e["status"] == "accepte")
    liberes  = sum(1 for e in h if e["status"] == "libere")
    declines = sum(1 for e in h if e["status"] == "decline")
    depense  = sum(e.get("price", 0) for e in h if e["status"] in ("accepte", "libere"))

    # Stats par pays
    nb_fr = sum(1 for e in h if e.get("country", COUNTRY_FR) == COUNTRY_FR)
    nb_us = sum(1 for e in h if e.get("country") == COUNTRY_US)

    text = (
        f"📊 {b('Historique global')}\n\n"
        f"├ Total demandes : {b(str(total))}\n"
        f"├ ✅ Actifs : {b(str(actifs))}\n"
        f"├ 🔚 Libérés : {b(str(liberes))}\n"
        f"├ ❌ Déclinés : {b(str(declines))}\n"
        f"├ 💲 Total dépensé : {b(f'{depense:.4f}')} $\n"
        f"├ 🇫🇷 Numéros FR : {b(str(nb_fr))}\n"
        f"└ 🇺🇸 Numéros US : {b(str(nb_us))}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{b('— Par agent (10 dernières activités) —')}\n\n"
    )

    by_agent = {}
    for e in h:
        aid = e["uid"]
        by_agent.setdefault(aid, []).append(e)

    buttons = []
    for aid, entries in by_agent.items():
        last10     = list(reversed(entries[-10:]))
        agent_name = esc(entries[0]["user_name"])
        has_active = aid in bot_state["active_numbers"]
        text += f"👤 {b(agent_name)} — {b(str(len(entries)))} achat(s)\n"

        seen_nums = set()
        for e in last10:
            num       = e["number"]
            c_flag    = country_flag(e.get("country", COUNTRY_FR))
            price_str = f" {e.get('price',0):.4f}$" if e.get("price") else ""
            text += f"   ├ {c_flag} {code(format_number(num, e.get('country', COUNTRY_FR)))} {slabel(e['status'])}{price_str} — {e['requested_at']}\n"

            if e["status"] in ("libere", "decline") and num not in seen_nums:
                seen_nums.add(num)
                op     = e.get("operator", "any")
                num_cb = num[-15:]
                lbl    = f"🔁 Racheter {num[-6:]} → {agent_name}"
                cb     = f"racheter_{aid}_{num_cb}_{op}"
                if has_active:
                    lbl = f"⛔ {agent_name} a déjà un numéro actif"
                    buttons.append([InlineKeyboardButton(lbl, callback_data=f"numno_{aid}")])
                else:
                    buttons.append([InlineKeyboardButton(lbl, callback_data=cb)])

        text += "\n"

    buttons.append([InlineKeyboardButton("🔄 Actualiser", callback_data="admin_historique")])
    buttons.append([InlineKeyboardButton("🔙 Retour",     callback_data="menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Admin : Recharge ─────────────────────────────────────────────────────────
async def handle_admin_recharge(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Recharger sur HeroSMS", url=Config.HEROSMS_RECHARGE_URL)],
        [InlineKeyboardButton("🔄 Actualiser le solde",   callback_data="admin_recharge")],
        [InlineKeyboardButton("🔙 Retour",                callback_data="menu")],
    ])
    try:
        balance   = await sms_client.get_balance()
        prices_fr = await get_prices_cached(COUNTRY_FR)
        prices_us = await get_prices_cached(COUNTRY_US)
        best_fr   = prices_fr[0]["price"] if prices_fr else Config.PRICE_PER_NUMBER
        best_us   = prices_us[0]["price"] if prices_us else Config.PRICE_PER_NUMBER
        nb_fr     = int(balance / best_fr) if best_fr > 0 else 0
        nb_us     = int(balance / best_us) if best_us > 0 else 0

        def alerte(nb):
            if nb == 0:   return f"🔴 {b('SOLDE INSUFFISANT')}"
            elif nb <= 5: return f"🟡 {b('Solde faible')}"
            else:         return f"🟢 Suffisant"

        text = (
            f"💳 {b('Recharge du compte HeroSMS')}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Solde actuel : {b(f'{balance:.4f}')} $\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🇫🇷 Meilleur prix FR : {b(f'{best_fr:.4f}')} $ → {b(str(nb_fr))} numéro(s) {alerte(nb_fr)}\n"
            f"🇺🇸 Meilleur prix US : {b(f'{best_us:.4f}')} $ → {b(str(nb_us))} numéro(s) {alerte(nb_us)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ {i('HeroSMS ne propose pas de recharge via API.')}\n"
            f"Cliquez sur le bouton ci-dessous pour recharger directement\n"
            f"sur le site HeroSMS. Les fonds sont crédités {b('automatiquement')}\n"
            f"dans un délai maximum de {b('3 heures')} après le dépôt."
        )
    except Exception as e:
        text = (
            f"💳 {b('Recharge du compte HeroSMS')}\n\n"
            f"❌ Impossible de récupérer le solde : {code(esc(str(e)))}\n\n"
            f"Cliquez ci-dessous pour accéder à la page de recharge."
        )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

# ─── Admin : Suivi des prix (FR + US) ────────────────────────────────────────
async def handle_admin_prix(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh_prix"),
         InlineKeyboardButton("🔙 Retour",     callback_data="menu")],
    ])
    medals = ["🥇","🥈","🥉"]
    balance = None
    try:
        balance = await sms_client.get_balance()
    except: pass

    text = f"💲 {b('Suivi des prix')} — Service {b(Config.SERVICE.upper())}\n"
    text += f"🔔 Suivi auto toutes les {Config.PRICE_POLL_MINUTES} min\n"
    if balance is not None:
        text += f"💳 Solde : {b(f'{balance:.4f}')} $\n"
    text += "\n"

    for country in (COUNTRY_FR, COUNTRY_US):
        flag     = country_flag(country)
        label    = country_label(country)
        last_up  = bot_state["price_last_update"][country]
        prices   = await get_prices_cached(country, force=False)

        text += f"━━━━━━━━━━━━━━━━━━\n{flag} {b(label)} — 🕐 {last_up or 'Jamais'}\n\n"
        if not prices:
            text += f"⚠️ Aucun prix disponible pour {label}.\n\n"
            continue
        for idx, p in enumerate(prices):
            medal = medals[idx] if idx < 3 else "•"
            nb    = int((balance if balance else 0) / p["price"]) if p["price"] > 0 else 0
            text += (
                f"{medal} {b(str(round(p.get('price',0),4)))} $\n"
                f"   Opérateur : {code(esc(p['operator']))}\n"
                f"   Disponibles : {b(str(p['count']))}\n"
                f"   Achetables avec le solde : {b(str(nb))}\n\n"
            )

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

# ─── Admin : Numéros actifs ───────────────────────────────────────────────────
async def handle_admin_pending(query):
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])
    active  = bot_state["active_numbers"]

    if not active:
        await query.edit_message_text(
            f"📋 {b('Numéros actifs')}\n\n✅ Aucun numéro actif en ce moment.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Actualiser", callback_data="admin_pending")],
                [InlineKeyboardButton("🔙 Retour",     callback_data="menu")],
            ]))
        return

    text    = f"📡 {b('Numéros actifs en temps réel')} — {b(str(len(active)))} agent(s)\n"
    text   += f"🕐 {now()}\n━━━━━━━━━━━━━━━━━━\n\n"
    buttons = []

    for uid_key, d in active.items():
        uid_int    = int(uid_key) if isinstance(uid_key, str) else uid_key
        agent_name = esc(d["user_name"])
        number     = esc(d["number"])
        price      = d.get("price", 0)
        operator   = esc(d.get("operator", "any"))
        active_at  = d.get("accepted_at", "?")
        country    = d.get("country", COUNTRY_FR)
        flag       = country_flag(country)
        stats      = um.get_user_stats(uid_int)
        achats     = stats.get("total_achats", 0)

        text += (
            f"👤 {b(agent_name)} — 🆔 {code(str(uid_int))}\n"
            f"{flag} Numéro : {code(format_number(number, country))}\n"
            f"💲 {price:.4f}$ | 📡 Op. {code(operator)} | 📦 {achats} achat(s)\n"
            f"🕐 Actif depuis : {active_at}\n\n"
        )
        buttons.append([
            InlineKeyboardButton(f"🔚 Libérer {d['user_name']}", callback_data=f"numno_{uid_int}"),
        ])

    buttons.append([InlineKeyboardButton("🔄 Actualiser", callback_data="admin_pending")])
    buttons.append([InlineKeyboardButton("🔙 Retour",     callback_data="menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Admin : Agents & Stats ───────────────────────────────────────────────────
async def handle_admin_agents(query):
    lb      = um.get_leaderboard()
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])
    if not lb:
        await query.edit_message_text(f"👥 {b('Agents & Stats')}\n\nAucun agent.", parse_mode="HTML", reply_markup=kb_back)
        return
    text    = f"👥 {b('Agents & Stats')}\n\n"
    buttons = []
    for rank, u in enumerate(lb, 1):
        sid   = str(u["uid"])
        name  = esc(u["first_name"])
        icon  = "🛡️" if u["status"] == "sub_admin" else ("⚠️" if u["status"] == "restricted" else "✅")
        actif = f" 🟢 {code(esc(bot_state['active_numbers'][u['uid']]['number']))}" if u["uid"] in bot_state["active_numbers"] else ""
        text += f"{rank}. {icon} {b(name)}{actif}\n   📦 {b(str(u['total_achats']))} achat(s) | ❌ {u['total_declines']} refus\n\n"
        row   = [InlineKeyboardButton(f"🔍 {u['first_name']}", callback_data=f"agent_{sid}")]
        if u["status"] == "agent":
            row.append(InlineKeyboardButton("⚠️ Restreindre", callback_data=f"restrict_{sid}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Admin : Détail agent ─────────────────────────────────────────────────────
async def handle_agent_detail(query, target_uid):
    info   = um.get_user_info(target_uid)
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")]])
    if not info:
        await query.edit_message_text("⚠️ Agent introuvable.", reply_markup=kb_back)
        return
    sid    = str(target_uid)
    name   = esc(info.get("first_name","Inconnu"))
    uname  = f"@{esc(info['username'])}" if info.get("username") else "(pas de username)"
    smap   = {"sub_admin":"🛡️ Sous-admin","agent":"✅ Agent","restricted":"⚠️ Restreint","banned":"🚫 Banni","pending":"⏳ En attente"}
    ses    = [e for e in bot_state["history"] if e["uid"] == target_uid]
    dep    = sum(e.get("price",0) for e in ses if e["status"] in ("accepte","libere"))
    num_actif = bot_state["active_numbers"].get(target_uid,{}).get("number")
    num_country = bot_state["active_numbers"].get(target_uid,{}).get("country", COUNTRY_FR)
    text = (
        f"🔍 {b('Détail agent')}\n\n"
        f"👤 {b(name)} — {uname}\n"
        f"🆔 {code(sid)} | {smap.get(info.get('status',''),'?')}\n\n"
        f"📦 Achats total : {b(str(info.get('total_achats',0)))}\n"
        f"💲 Dépenses session : {b(f'{dep:.4f}')} $\n"
        f"❌ Déclinés : {info.get('total_declines',0)}\n"
        f"🕐 Dernière activité : {info.get('last_activity','Jamais')}\n"
    )
    if num_actif:
        text += f"🟢 Numéro actif : {country_flag(num_country)} {code(format_number(num_actif, num_country))}\n"
    if info.get("restrict_reason"):
        text += f"⚠️ Raison : {i(esc(info['restrict_reason']))}\n"
    buttons = []
    if info.get("status") == "agent":
        buttons.append([
            InlineKeyboardButton("⚠️ Restreindre", callback_data=f"restrict_{sid}"),
            InlineKeyboardButton("🔚 Retiêr",      callback_data=f"rm_{sid}"),
        ])
        if is_main_admin(query.from_user.id):
            buttons.append([InlineKeyboardButton("🛡️ Promouvoir sous-admin", callback_data=f"promote_{sid}")])
    elif info.get("status") == "restricted":
        buttons.append([
            InlineKeyboardButton("✅ Lever restriction", callback_data=f"unrestrict_{sid}"),
            InlineKeyboardButton("🔚 Retirer",           callback_data=f"rm_{sid}"),
        ])
    elif info.get("status") == "sub_admin" and is_main_admin(query.from_user.id):
        buttons.append([InlineKeyboardButton("🔻 Révoquer sous-admin", callback_data=f"revoke_adm_{sid}")])
    buttons.append([InlineKeyboardButton("🚫 Bannir", callback_data=f"ban_{sid}")])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Admin : Gestion des sous-admins ─────────────────────────────────────────
async def handle_admin_admins(query):
    admins  = um.get_admins()
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="menu")]])
    text    = f"🛡️ {b('Gestion des sous-admins')}\n\n"
    if not admins:
        text += "Aucun sous-admin pour l'instant.\n\nAllez dans Agents & Stats → cliquez sur un agent → Promouvoir sous-admin"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb_back)
        return
    text   += f"Total : {b(str(len(admins)))} sous-admin(s)\n\n"
    buttons = []
    for sid, u in admins.items():
        name    = esc(u.get("first_name","Inconnu"))
        uname   = f"@{esc(u['username'])}" if u.get("username") else ""
        promo   = u.get("promoted_at","")
        text   += f"🛡️ {b(name)} {uname}\n🆔 {code(sid)} | 🕐 Promu : {promo}\n\n"
        buttons.append([
            InlineKeyboardButton(f"🔻 Révoquer {u.get('first_name','')}", callback_data=f"revoke_adm_{sid}"),
            InlineKeyboardButton("🚫 Bannir",                             callback_data=f"ban_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ─── Actions admin ────────────────────────────────────────────────────────────
async def action_rm(query, uid, data, context):
    target = int(data.split("_")[1])
    user   = um.remove_user(target)
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(f"🔚 Accès de {b(name)} retiré.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")]]))
    if user:
        try: await context.bot.send_message(chat_id=target,
            text=f"🔚 {b('Accès retiré')}\n\nVotre accès au bot a été révoqué.", parse_mode="HTML")
        except: pass

async def action_restrict(query, uid, data, context):
    target = int(data.split("_")[1])
    user   = um.restrict_user(target, "Restriction manuelle par l'admin")
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(f"⚠️ {b(name)} restreint — plus de demandes de numéros.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")]]))
    if user:
        try: await context.bot.send_message(chat_id=target,
            text=f"⚠️ {b('Accès restreint')}\n\nVous ne pouvez plus demander de numéros. Contactez l'admin.", parse_mode="HTML")
        except: pass

async def action_unrestrict(query, uid, data, context):
    target = int(data.split("_")[1])
    user   = um.unrestrict_user(target)
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(f"✅ Restriction levée pour {b(name)}.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")]]))
    if user:
        try: await context.bot.send_message(chat_id=target,
            text=f"✅ {b('Restriction levée !')}\n\nVotre accès complet est rétabli.", parse_mode="HTML")
        except: pass

async def action_ban(query, uid, data, context):
    target = int(data.split("_")[1])
    user   = um.ban_user(target)
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(f"🚫 {b(name)} banni définitivement.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_agents")]]))

async def action_promote(query, admin_uid, data, context):
    target = int(data.split("_")[1])
    info   = um.get_user_info(target) or {}
    user   = um.promote_to_admin(target, admin_uid, info.get("first_name",""), info.get("username",""))
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(
        f"🛡️ {b(name)} a été promu {b('sous-admin')} !\nIl peut maintenant gérer les demandes de numéros.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_admins")]]))
    if user:
        try: await context.bot.send_message(chat_id=target,
            text=f"🛡️ {b('Vous êtes maintenant sous-admin !')}\n\nL'admin principal vous a accordé des droits d'administration sur le bot.",
            parse_mode="HTML")
        except: pass

async def action_revoke_admin(query, data, context):
    target = int(data.split("_")[2])
    user   = um.revoke_admin(target)
    name   = esc(user.get("first_name","Inconnu")) if user else "Inconnu"
    await query.edit_message_text(
        f"🔻 Droits admin de {b(name)} révoqués. Repassé agent.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="admin_admins")]]))
    if user:
        try: await context.bot.send_message(chat_id=target,
            text=f"🔻 {b('Droits admin révoqués')}\n\nVous êtes repassé agent sur le bot.", parse_mode="HTML")
        except: pass

# ─── Sous-admin : Gestion des accès ──────────────────────────────────────────
async def handle_sub_acces(query):
    users       = um.get_all_users()
    pending     = users["pending"]
    authorized  = users["authorized"]

    nb_pend = len(pending)
    nb_auth = len(authorized)

    text = (
        f"👥 {b('Gestion des accès agents')}\n\n"
        f"⏳ Demandes en attente : {b(str(nb_pend))}\n"
        f"✅ Agents autorisés : {b(str(nb_auth))}\n"
    )
    buttons = [
        [InlineKeyboardButton(f"⏳ Demandes en attente ({nb_pend})", callback_data="sub_voir_pending")],
        [InlineKeyboardButton(f"✅ Agents autorisés ({nb_auth})",    callback_data="sub_voir_authorized")],
        [InlineKeyboardButton("🔙 Retour",                           callback_data="menu")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_sub_voir_pending(query):
    pending = um.get_all_users()["pending"]
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]])
    if not pending:
        await query.edit_message_text("⏳ Aucune demande en attente.", reply_markup=kb_back)
        return
    text    = f"⏳ {b('Demandes en attente')} ({len(pending)})\n\n"
    buttons = []
    for sid, u in pending.items():
        name  = esc(u.get("first_name","Inconnu"))
        uname = f"@{esc(u['username'])}" if u.get("username") else "(pas de username)"
        text += f"👤 {b(name)} — {uname}\n🆔 {code(sid)} | 🕐 {u.get('requested_at','')}\n\n"
        buttons.append([
            InlineKeyboardButton(f"✅ {u.get('first_name','')}", callback_data=f"sub_approve_{sid}"),
            InlineKeyboardButton("❌ Refuser",                   callback_data=f"sub_reject_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_sub_voir_authorized(query):
    authorized = um.get_all_users()["authorized"]
    kb_back    = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]])
    if not authorized:
        await query.edit_message_text("✅ Aucun agent autorisé.", reply_markup=kb_back)
        return
    text    = f"✅ {b('Agents autorisés')} ({len(authorized)})\n\n"
    buttons = []
    for sid, u in authorized.items():
        name  = esc(u.get("first_name","Inconnu"))
        uname = f"@{esc(u['username'])}" if u.get("username") else "(pas de username)"
        achats = u.get("total_achats", 0)
        text += f"👤 {b(name)} — {uname}\n📦 {achats} achat(s) | 🆔 {code(sid)}\n\n"
        buttons.append([
            InlineKeyboardButton(f"🔚 Retirer {u.get('first_name','')}", callback_data=f"sub_rm_{sid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def sub_approve(query, target_uid: int, context):
    user = um.approve_user(target_uid)
    if user:
        name = esc(user.get("first_name","Inconnu"))
        await query.edit_message_text(
            f"✅ {b(name)} approuvé — accès accordé au bot principal.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"🎉 {b('Accès accordé !')}\n\nVotre demande a été approuvée.\nVous pouvez utiliser le bot 🇫🇷🇺🇸",
                parse_mode="HTML")
        except: pass
    else:
        await query.edit_message_text("⚠️ Demande introuvable (déjà traitée ?).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))


async def sub_reject(query, target_uid: int, context):
    user = um.reject_user(target_uid)
    if user:
        name = esc(user.get("first_name","Inconnu"))
        await query.edit_message_text(
            f"❌ Demande de {b(name)} refusée.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"❌ {b('Demande refusée')}\n\nContactez l'administrateur si nécessaire.",
                parse_mode="HTML")
        except: pass
    else:
        await query.edit_message_text("⚠️ Demande introuvable.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))


async def sub_rm(query, target_uid: int, context):
    user = um.remove_user(target_uid)
    if user:
        name = esc(user.get("first_name","Inconnu"))
        await query.edit_message_text(
            f"🔚 Accès de {b(name)} retiré.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"🔚 {b('Accès retiré')}\n\nVotre accès au bot a été révoqué.",
                parse_mode="HTML")
        except: pass
    else:
        await query.edit_message_text("⚠️ Agent introuvable.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="sub_acces")]]))

# ─── /veille & /agents ────────────────────────────────────────────────────────
async def cmd_veille(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return
    bot_state["veille"] = not bot_state["veille"]
    msg = f"🌙 Mode veille {b('activé')}." if bot_state["veille"] else f"☀️ Mode veille {b('désactivé')}."
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return
    lb = um.get_leaderboard()
    if not lb:
        await update.message.reply_text("Aucun agent.")
        return
    try:
        balance   = await sms_client.get_balance()
        prices_fr = await get_prices_cached(COUNTRY_FR)
        prices_us = await get_prices_cached(COUNTRY_US)
        best_fr   = prices_fr[0]["price"] if prices_fr else Config.PRICE_PER_NUMBER
        best_us   = prices_us[0]["price"] if prices_us else Config.PRICE_PER_NUMBER
        nb_fr     = int(balance / best_fr) if best_fr > 0 else 0
        nb_us     = int(balance / best_us) if best_us > 0 else 0
        header    = (
            f"💳 {b(f'{balance:.4f}')} $\n"
            f"🇫🇷 → {b(str(nb_fr))} numéro(s) | 🇺🇸 → {b(str(nb_us))} numéro(s)\n\n"
        )
    except: header = ""
    text = f"👥 {b('Récapitulatif agents')}\n\n{header}"
    for rank, u in enumerate(lb, 1):
        icon = "🛡️" if u["status"] == "sub_admin" else ("⚠️" if u["status"] == "restricted" else "✅")
        text += f"{rank}. {icon} {b(esc(u['first_name']))} — {b(str(u['total_achats']))} achat(s)\n"
    await update.message.reply_text(text, parse_mode="HTML")

# ─── Menu Système ─────────────────────────────────────────────────────────────
async def handle_admin_systeme(query, uid):
    from herosms_api import PROXIES
    nb_proxies  = len(PROXIES)
    proxy_label = f"{nb_proxies} proxy(s) charge(s)" if nb_proxies else "Aucun proxy configure"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Recharger les proxies",  callback_data="sys_reload_proxy"),
         InlineKeyboardButton("\U0001f310 Tester la connexion",    callback_data="sys_test_conn")],
        [InlineKeyboardButton("\U0001f501 Redemarrer le bot",      callback_data="sys_restart_confirm")],
        [InlineKeyboardButton("\U0001f519 Menu principal",          callback_data="menu")],
    ])
    pending_wh = len(wh.get_pending_rentals())
    wh_label   = f"{pending_wh} numero(s) en ecoute" if pending_wh else "aucun numero actif"
    txt = "\u2699\ufe0f <b>Systeme &amp; Outils</b>\n\n" \
          "\U0001f310 <b>Proxies :</b> " + proxy_label + "\n" \
          "\U0001f4e1 <b>Webhook SMS :</b> " + wh_label + "\n" \
          "\U0001f916 <b>Bot :</b> operationnel\n\n" \
          "Choisissez une action :"
    await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)


async def handle_sys_reload_proxy(query, uid):
    import herosms_api as _api
    _api.PROXIES = _api._load_proxies()
    nb = len(_api.PROXIES)
    if nb:
        label = f"\u2705 {nb} proxy(s) recharge(s)"
    else:
        label = "\u26a0\ufe0f Aucun proxy trouve (verifiez ROTATING_PROXY ou PROXY_LIST)"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 Retour Systeme", callback_data="admin_systeme")]])
    await query.edit_message_text("\U0001f504 <b>Rotation IP</b>\n\n" + label, parse_mode="HTML", reply_markup=kb)


async def handle_sys_test_conn(query, uid):
    import aiohttp
    from herosms_api import _pick_proxy
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Retester",       callback_data="sys_test_conn"),
         InlineKeyboardButton("\U0001f519 Retour Systeme", callback_data="admin_systeme")],
    ])
    await query.edit_message_text("\U0001f310 Test en cours...", parse_mode="HTML")
    try:
        proxy = _pick_proxy()
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org?format=json",
                             proxy=proxy, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
                ip_out = data.get("ip", "?")
        mode = "via proxy" if proxy else "sans proxy (IP Railway fixe)"
        txt = "\u2705 <b>Connexion OK</b>\n\n" \
              "\U0001f310 IP sortante : <code>" + ip_out + "</code>\n" \
              "\U0001f4e1 Mode : " + mode
        await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await query.edit_message_text(
            "\u274c <b>Echec connexion</b>\n\n<code>" + str(e)[:200] + "</code>",
            parse_mode="HTML", reply_markup=kb)


async def handle_sys_restart_confirm(query, uid):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Confirmer le redemarrage", callback_data="sys_restart_do")],
        [InlineKeyboardButton("\u274c Annuler",                  callback_data="admin_systeme")],
    ])
    txt = "\u26a0\ufe0f <b>Redemarrer le bot ?</b>\n\n" \
          "Le bot sera indisponible quelques secondes.\n" \
          "Les numeros actifs restent en base."
    await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)


async def handle_sys_restart_do(query, uid, context):
    await query.edit_message_text(
        "\U0001f501 <b>Redemarrage en cours...</b>\n\nLe bot revient dans quelques secondes.",
        parse_mode="HTML")
    if not is_main_admin(uid):
        try:
            name = query.from_user.first_name
            await context.bot.send_message(
                chat_id=Config.ADMIN_ID,
                text="\U0001f501 <b>Redemarrage declenche</b> par <b>" + esc(name) + "</b> (sous-admin).",
                parse_mode="HTML")
        except:
            pass
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ─── Commandes texte /restart et /rotateip ───────────────────────────────────
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("\u26d4 Reserve aux admins.")
        return
    await update.message.reply_text("\U0001f501 <b>Redemarrage en cours...</b>", parse_mode="HTML")
    if not is_main_admin(uid):
        try:
            name = esc(update.effective_user.first_name)
            await context.bot.send_message(
                chat_id=Config.ADMIN_ID,
                text="\U0001f501 <b>Redemarrage declenche</b> par <b>" + name + "</b> (sous-admin).",
                parse_mode="HTML")
        except:
            pass
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def cmd_rotateip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("\u26d4 Reserve aux admins.")
        return
    import herosms_api as _api
    _api.PROXIES = _api._load_proxies()
    nb = len(_api.PROXIES)
    if nb:
        label = f"\u2705 {nb} proxy(s) recharge(s)"
    else:
        label = "\u26a0\ufe0f Aucun proxy configure (ROTATING_PROXY ou PROXY_LIST manquant)"
    await update.message.reply_text("\U0001f310 <b>Rotation IP</b>\n\n" + label, parse_mode="HTML")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    import signal

    async def run():
        # 1. Démarrer le serveur HTTP EN PREMIER (Railway health-check)
        runner = await wh.start_webhook_server()

        # 2. Construire l'application Telegram
        app = Application.builder().token(Config.BOT_TOKEN).build()
        app.add_handler(CommandHandler("start",    start))
        app.add_handler(CommandHandler("veille",   cmd_veille))
        app.add_handler(CommandHandler("agents",   cmd_agents))
        app.add_handler(CommandHandler("restart",  cmd_restart))
        app.add_handler(CommandHandler("rotateip", cmd_rotateip))
        app.add_handler(CallbackQueryHandler(button_handler))

        wh.set_bot_app(app)

        # 3. post_init : lancer le price_watcher une fois le bot prêt
        async def post_init(application):
            asyncio.create_task(price_watcher(application))

        app.post_init = post_init

        logger.info("🤖 Bot principal démarré — 🇫🇷 France & 🇺🇸 USA | Suivi prix actif")

        # 4. run_polling gère initialize/start/stop/shutdown proprement
        #    allowed_updates=[] = tous les updates
        try:
            await app.run_polling(
                drop_pending_updates=True,
                close_loop=False,
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())


if __name__ == "__main__":
    main()
