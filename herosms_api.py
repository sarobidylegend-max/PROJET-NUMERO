"""
Client API HeroSMS
Inclut la récupération des prix en temps réel par opérateur.
"""

import aiohttp


class HeroSMSClient:
    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(self, params: dict) -> str:
        params["api_key"] = self.api_key
        async with aiohttp.ClientSession() as session:
            async with session.get(self.BASE_URL, params=params) as resp:
                return await resp.text()

    async def _get_json(self, params: dict):
        params["api_key"] = self.api_key
        async with aiohttp.ClientSession() as session:
            async with session.get(self.BASE_URL, params=params) as resp:
                return await resp.json(content_type=None)

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
            # Format: {country: {service: {operator: {cost, count}}}}
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
            # Fallback via getNumbersStatus
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
