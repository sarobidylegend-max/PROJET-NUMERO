"""
ai_selector.py — Sélection intelligente de numéro via Claude AI (Anthropic)

Rôle :
  1. Analyser la liste des prix HeroSMS et choisir l'ordre optimal des opérateurs
  2. Décider une stratégie de retry intelligente si un opérateur échoue
  3. Retourner un plan trié [{operator, price, count, priority, reason}]

L'IA reçoit le contexte complet (prix, historique d'échecs, solde) et décide
quel opérateur tenter en premier pour maximiser les chances au coût minimum.
"""

import aiohttp
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

# ── Clé API Anthropic ─────────────────────────────────────────────────────────
# Variable d'environnement : ANTHROPIC_API_KEY
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
AI_MODEL          = "claude-sonnet-4-20250514"

# Timeout pour l'appel IA (en secondes) — on ne veut pas bloquer l'agent trop longtemps
AI_TIMEOUT = 8

# Seuil maximum absolu (ne jamais dépasser, même si l'IA suggère autre chose)
HARD_PRICE_CAP = 0.09  # Cible 0.085 $ — rejette 0.30 $ et 1 $


async def ai_select_best_operators(
    prices: list[dict],
    country_label: str,
    service: str,
    balance: float,
    failed_operators: list[str] | None = None,
    attempt: int = 1,
) -> list[dict]:
    """
    Appelle Claude AI pour choisir l'ordre optimal des opérateurs.

    Args:
        prices:           Liste brute HeroSMS [{operator, price, count}, ...]
        country_label:    "France" ou "USA"
        service:          ex. "ig" (Instagram)
        balance:          Solde HeroSMS actuel en $
        failed_operators: Opérateurs déjà essayés et échoués dans cette session
        attempt:          Numéro de tentative (pour adapter la stratégie)

    Returns:
        Liste triée [{operator, price, count, priority, reason}, ...]
        Toujours filtrée <= HARD_PRICE_CAP et par prix croissant en fallback.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("[AI] ANTHROPIC_API_KEY non définie — fallback tri par prix.")
        return _fallback_sort(prices, failed_operators)

    # Filtrer hardcap et prix > solde
    eligible = [
        p for p in prices
        if p.get("price", 999) <= HARD_PRICE_CAP and p.get("price", 999) <= balance
    ]
    if not eligible:
        # Même si hors budget, garder le moins cher pour que le bot puisse lever
        # une erreur de solde claire plutôt que "aucun opérateur"
        eligible = sorted(prices, key=lambda x: x.get("price", 999))[:3]

    failed_str = ", ".join(failed_operators) if failed_operators else "aucun"

    prompt = f"""Tu es un optimiseur de coût pour un bot Telegram qui achète des numéros de téléphone virtuels.

Contexte :
- Service : {service} (vérification de compte)
- Pays : {country_label}
- Solde disponible : {balance:.4f} $
- Plafond absolu : {HARD_PRICE_CAP} $ par numéro
- Tentative n° : {attempt}
- Opérateurs déjà échoués cette session : {failed_str}

Liste des opérateurs disponibles (JSON) :
{json.dumps(eligible, ensure_ascii=False)}

Ta mission :
1. Trier les opérateurs du MEILLEUR au moins bon selon : prix bas EN PREMIER, puis stock élevé comme critère secondaire.
2. Exclure ABSOLUMENT les opérateurs déjà échoués ({failed_str}).
3. Si plusieurs opérateurs ont le même prix, privilégier celui avec le plus grand stock (count).
4. Ne jamais dépasser {HARD_PRICE_CAP:.2f} $ (cible le 0.085 $, rejette tout le reste).

Réponds UNIQUEMENT en JSON valide, sans texte avant ni après, sans markdown :
[
  {{"operator": "nom_operateur", "price": 0.0XX, "count": NNN, "priority": 1, "reason": "explication courte"}},
  ...
]"""

    try:
        headers = {
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      AI_MODEL,
            "max_tokens": 512,
            "messages":   [{"role": "user", "content": prompt}],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[AI] Erreur HTTP {resp.status}: {body[:200]}")
                    return _fallback_sort(eligible, failed_operators)

                data = await resp.json()

        # Extraire le texte de la réponse
        raw = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw += block["text"]

        raw = raw.strip()
        # Nettoyer les balises markdown si présentes
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)
        if not isinstance(result, list) or not result:
            raise ValueError("Réponse IA vide ou malformée")

        # Sécurité : re-filtrer le hardcap (l'IA ne doit pas dépasser)
        result = [r for r in result if r.get("price", 999) <= HARD_PRICE_CAP]
        if not result:
            return _fallback_sort(eligible, failed_operators)

        logger.info(
            f"[AI] Stratégie choisie ({len(result)} op.) : "
            + ", ".join(f"{r['operator']}@{r['price']:.4f}$" for r in result)
        )
        return result

    except asyncio.TimeoutError:
        logger.warning("[AI] Timeout — fallback tri par prix.")
        return _fallback_sort(eligible, failed_operators)
    except Exception as e:
        logger.warning(f"[AI] Erreur ({type(e).__name__}): {e} — fallback.")
        return _fallback_sort(eligible, failed_operators)


def _fallback_sort(prices: list[dict], failed_operators: list[str] | None = None) -> list[dict]:
    """Tri de secours : prix croissant, excluant les opérateurs échoués."""
    failed = set(failed_operators or [])
    eligible = [
        {**p, "priority": i + 1, "reason": "tri par prix (fallback)"}
        for i, p in enumerate(
            sorted(
                [p for p in prices if p.get("operator") not in failed],
                key=lambda x: (x.get("price", 999), -x.get("count", 0)),
            )
        )
        if p.get("price", 999) <= HARD_PRICE_CAP
    ]
    # Si tout est filtré (ex: tous échoués), garder quand même le moins cher
    if not eligible and prices:
        best = min(prices, key=lambda x: x.get("price", 999))
        eligible = [{**best, "priority": 1, "reason": "dernier recours"}]
    return eligible
