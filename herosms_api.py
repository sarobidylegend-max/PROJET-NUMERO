"""
Client API HeroSMS
Inclut la récupération des prix en temps réel par opérateur.
Rotation d'IP automatique via proxies rotatifs (WebShare / tout proxy HTTP).
"""

import aiohttp
import asyncio
import logging
import os
import random

logger = logging.getLogger(__name__)

# ── Configuration proxies ──────────────────────────────────────────────────────
# Sur Railway, définissez la variable d'environnement PROXY_LIST avec vos proxies,
# format : "http://user:pass@host1:port,http://user:pass@host2:port,..."
# Ou utilisez un proxy rotatif unique (ex. WebShare, BrightData, Oxylabs) :
# ROTATING_PROXY=http://user:pass@proxy.webshare.io:80
#
# Si aucun proxy n'est configuré, le bot tourne sans proxy (comportement actuel).

def _load_proxies() -> list[str]:
    """Charge la liste de proxies depuis les variables d'environnement."""
    # Option 1 : proxy rotatif unique (recommandé avec WebShare, BrightData, etc.)
    rotating = os.getenv("ROTATING_PROXY", "").strip()
    if rotating:
        return [rotating]

    # Option 2 : liste de proxies séparés par des virgules
    proxy_list = os.getenv("PROXY_LIST", "").strip()
    if proxy_list:
        proxies = [p.strip() for p in proxy_list.split(",") if p.strip()]
        if proxies:
            logger.info(f"[HeroSMS] {len(proxies)} proxy(s) chargé(s).")
            return proxies

    logger.warning("[HeroSMS] Aucun proxy configuré. Requêtes sans rotation d'IP.")
    return []

PROXIES: list[str] = _load_proxies()

# Nombre de tentatives en cas d'erreur réseau/IP
MAX_RETRIES = int(os.getenv("HEROSMS_MAX_RETRIES", "3"))


def _pick_proxy() -> str | None:
    """Retourne un proxy aléatoire depuis la liste, ou None si pas de proxy."""
    return random.choice(PROXIES) if PROXIES else None


class HeroSMSClient:
    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(self, params: dict, retries: int = MAX_RETRIES) -> str:
        """GET avec rotation de proxy et retry automatique."""
        params["api_key"] = self.api_key
        last_err = None

        for attempt in range(1, retries + 1):
            proxy = _pick_proxy()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        self.BASE_URL,
                        params=params,
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        text = await resp.text()
                        if attempt > 1:
                            logger.info(f"[HeroSMS] Succès à la tentative {attempt}.")
                        return text
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[HeroSMS] Tentative {attempt}/{retries} échouée"
                    f"{' via ' + proxy if proxy else ''} : {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(1.5 * attempt)  # backoff exponentiel léger

        raise Exception(f"HeroSMS inaccessible après {retries} tentatives : {last_err}")

    async def _get_json(self, params: dict, retries: int = MAX_RETRIES):
        """GET JSON avec rotation de proxy et retry automatique."""
        params["api_key"] = self.api_key
        last_err = None

        for attempt in range(1, retries + 1):
            proxy = _pick_proxy()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        self.BASE_URL,
                        params=params,
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        return await resp.json(content_type=None)
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[HeroSMS JSON] Tentative {attempt}/{retries} échouée"
                    f"{' via ' + proxy if proxy else ''} : {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(1.5 * attempt)

        raise Exception(f"HeroSMS JSON inaccessible après {retries} tentatives : {last_err}")

    # ── Solde ──────────────────────────────────────────────────────────────────
    async def get_balance(self) -> float:
        text = await self._get({"action": "getBalance"})
        if "ACCESS_BALANCE" in text:
            return float(text.split(":")[1])
        raise Exception(f"Erreur solde: {text}")

    # ── Prix disponibles ───────────────────────────────────────────────────────
    async def get_prices(self, service: str, country: str = "0") -> list[dict]:
        """
        Récupère et retourne les 3 offres les moins chères pour un service/pays.
        Retourne: [{"operator": "any", "price": 0.085, "count": 892}, ...]
        """
        try:
            data = await self._get_json({
                "action": "getPrices",
                "service": service,
                "country": country,
            })
            results = []
            if isinstance(data, dict):
                for _country, services in data.items():
                    if not isinstance(services, dict): continue
                    for _svc, operators in services.items():
                        if not isinstance(operators, dict): continue
                        for op, info in operators.items():
                            if not isinstance(info, dict): continue
                            cost  = float(info.get("cost") or info.get("price") or 0)
                            count = int(info.get("count", 0))
                            if cost > 0 and count > 0:
                                results.append({"operator": op, "price": cost, "count": count})

            results.sort(key=lambda x: x["price"])
            return results[:3] if results else []

        except Exception:
            try:
                data = await self._get_json({
                    "action": "getNumbersStatus",
                    "service": service,
                    "country": country,
                })
                results = []
                if isinstance(data, dict):
                    for op, info in data.items():
                        if not isinstance(info, dict): continue
                        cost  = float(info.get("cost") or info.get("price") or 0)
                        count = int(info.get("count", 0))
                        if cost > 0 and count > 0:
                            results.append({"operator": op, "price": cost, "count": count})
                results.sort(key=lambda x: x["price"])
                return results[:3]
            except Exception:
                return []

    # ── Obtenir un numéro ──────────────────────────────────────────────────────
    async def get_number(self, service: str, country: str = "0", operator: str = "any") -> dict:
        """
        Demande un numéro virtuel avec opérateur optionnel.
        Retourne {"id": ..., "number": ..., "operator": ...}
        """
        params = {"action": "getNumber", "service": service, "country": country}
        if operator and operator != "any":
            params["operator"] = operator

        text = await self._get(params)
        if text.startswith("ACCESS_NUMBER"):
            parts = text.split(":")
            return {"id": parts[1], "number": parts[2], "operator": operator}
        raise Exception(f"Erreur get_number: {text}")

    # ── Récupérer SMS / OTP ────────────────────────────────────────────────────
    async def get_sms(self, rental_id: str) -> dict | None:
        text = await self._get({"action": "getStatus", "id": rental_id})
        if text.startswith("STATUS_OK"):
            parts = text.split(":", 2)
            code     = parts[1] if len(parts) > 1 else "N/A"
            full_msg = parts[2] if len(parts) > 2 else code
            return {"code": code, "message": full_msg}
        if "STATUS_WAIT_CODE" in text or "STATUS_WAIT_RETRY" in text:
            return None
        raise Exception(f"Erreur get_sms: {text}")

    # ── Annuler un numéro ──────────────────────────────────────────────────────
    async def cancel_number(self, rental_id: str) -> bool:
        text = await self._get({"action": "setStatus", "id": rental_id, "status": "8"})
        if "ACCESS_CANCEL" in text:
            return True
        raise Exception(f"Erreur cancel_number: {text}")

    # ── Confirmer réception ────────────────────────────────────────────────────
    async def confirm_sms(self, rental_id: str) -> bool:
        text = await self._get({"action": "setStatus", "id": rental_id, "status": "6"})
        return "ACCESS_ACTIVATION" in text
