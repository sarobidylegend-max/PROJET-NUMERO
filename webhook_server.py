"""
webhook_server.py — Serveur webhook HeroSMS
Reçoit les notifications SMS en temps réel depuis HeroSMS.

Format POST envoyé par HeroSMS :
{
  "activationId": 123456,
  "service": "ig",
  "text": "Sms text",
  "code": "12345",
  "country": 2,
  "receivedAt": "2026-01-29T11:28:14Z"
}

HeroSMS attend une réponse HTTP 200. Si non reçu, il réessaie 7 fois toutes les 3 minutes.
Jusqu'à 3 adresses webhook configurables sur HeroSMS.

Railway expose automatiquement le port via la variable PORT.
URL à configurer sur HeroSMS :
  https://projet-numero-bot-number.up.railway.app/herosms-webhook
"""

import asyncio
import logging
import os
from aiohttp import web

logger = logging.getLogger(__name__)

# Référence vers l'application bot (définie au démarrage dans bot.py)
_bot_app = None
_sms_callbacks: dict = {}   # {str(rental_id): [coroutine_function, ...]}


def set_bot_app(app):
    global _bot_app
    _bot_app = app


def register_sms_callback(rental_id: str, callback):
    """Enregistre un callback appelé dès que le SMS pour ce rental_id arrive."""
    key = str(rental_id)
    if key not in _sms_callbacks:
        _sms_callbacks[key] = []
    _sms_callbacks[key].append(callback)
    logger.info(f"[Webhook] Callback enregistré pour rental_id={key}")


def unregister_sms_callback(rental_id: str):
    """Supprime les callbacks d'un rental_id (numéro libéré/expiré)."""
    _sms_callbacks.pop(str(rental_id), None)


def get_pending_rentals():
    return list(_sms_callbacks.keys())


async def _handle_webhook(request: web.Request) -> web.Response:
    """
    Point d'entrée du webhook HeroSMS.
    HeroSMS envoie un POST JSON. On répond HTTP 200 immédiatement.
    """
    try:
        # Lire le body JSON
        try:
            data = await request.json()
        except Exception:
            # Fallback form-encoded
            raw = await request.post()
            data = dict(raw)

        # Champs exacts selon la doc HeroSMS
        rental_id   = str(data.get("activationId", "")).strip()
        sms_code    = str(data.get("code", "")).strip()
        sms_text    = str(data.get("text", sms_code)).strip()
        service     = str(data.get("service", "")).strip()
        country     = str(data.get("country", "")).strip()
        received_at = str(data.get("receivedAt", "")).strip()

        logger.info(
            f"[Webhook] SMS recu — activationId={rental_id} "
            f"code={sms_code} service={service} country={country} at={received_at}"
        )

        # Répondre HTTP 200 immédiatement (HeroSMS l'exige)
        # On déclenche les callbacks en tâche de fond pour ne pas bloquer la réponse
        asyncio.create_task(_dispatch_callbacks(rental_id, sms_code, sms_text))

        return web.Response(status=200, text="OK")

    except Exception as e:
        logger.error(f"[Webhook] Erreur handler: {e}")
        # On répond quand même 200 pour éviter les renvois HeroSMS
        return web.Response(status=200, text="OK")


async def _dispatch_callbacks(rental_id: str, sms_code: str, sms_text: str):
    """Appelle tous les callbacks enregistrés pour ce rental_id."""
    callbacks = _sms_callbacks.get(rental_id, [])
    if not callbacks:
        logger.warning(f"[Webhook] Aucun callback pour rental_id={rental_id} (numero expiré ou inconnu)")
        return

    sms_payload = {
        "code":    sms_code,
        "message": sms_text,
    }
    for cb in callbacks:
        try:
            await cb(sms_payload)
        except Exception as e:
            logger.error(f"[Webhook] Erreur callback rental_id={rental_id}: {e}")


async def _handle_health(request: web.Request) -> web.Response:
    """Route de diagnostic."""
    pending = get_pending_rentals()
    return web.json_response({
        "status":          "ok",
        "pending_rentals": len(pending),
        "rental_ids":      pending,
    })


async def _handle_root(request: web.Request) -> web.Response:
    """Route racine — Railway health-check."""
    return web.Response(status=200, text="OK")


async def start_webhook_server():
    """Démarre le serveur HTTP sur le port fourni par Railway."""
    port = int(os.getenv("PORT", "8080"))
    app  = web.Application()

    app.router.add_route("GET",  "/",                 _handle_root)
    app.router.add_route("GET",  "/herosms-webhook",  _handle_webhook)
    app.router.add_route("POST", "/herosms-webhook",  _handle_webhook)
    app.router.add_route("GET",  "/health",           _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"[Webhook] Serveur HTTP démarré sur 0.0.0.0:{port}")
    return runner
