"""
Configuration — Remplissez toutes les valeurs avant de lancer les bots.
"""
import os


class Config:
    # ── Bot principal ──────────────────────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8704129624:AAFlZVDveLmX6Mag2s-u84aptJzx52q-vRg")

    # ── Bot d'enrôlement ───────────────────────────────────────────────────────
    ENROLL_BOT_TOKEN: str    = os.getenv("ENROLL_BOT_TOKEN", "8672249926:AAGXjihtzyns9Bjr7LvmK0E77JPxvdas9kg")
    ENROLL_BOT_USERNAME: str = os.getenv("ENROLL_BOT_USERNAME", "demandaccess_bot")

    # ── Admin principal ────────────────────────────────────────────────────────
    ADMIN_ID: int = int(os.getenv("ADMIN_ID","6846254505"))

    # ── HeroSMS ────────────────────────────────────────────────────────────────
    HEROSMS_API_KEY: str = os.getenv("HEROSMS_API_KEY", "7b4ed25A77dc36706A98271e380251f4")
    SERVICE: str         = os.getenv("HEROSMS_SERVICE", "ig")   # ig = Instagram/Threads
    COUNTRY: str         = os.getenv("HEROSMS_COUNTRY", "187")   # 78 = France

    # Prix par défaut si l'API ne retourne pas les prix
    PRICE_PER_NUMBER: float = float(os.getenv("PRICE_PER_NUMBER", "0.077"))

    # ── Suivi des prix ─────────────────────────────────────────────────────────
    # Fréquence de mise à jour en minutes (recommandé : 10-15 min)
    PRICE_POLL_MINUTES: int = int(os.getenv("PRICE_POLL_MINUTES", "10"))

    # ── Lien recharge HeroSMS ──────────────────────────────────────────────────
    # Lien direct vers la page de recharge de votre compte HeroSMS
    HEROSMS_RECHARGE_URL: str = os.getenv(
        "HEROSMS_RECHARGE_URL",
        "https://hero-sms.com/balance/add"
    )

    # ── Claude AI (sélection intelligente des numéros) ─────────────────────────
    # Obtenir une clé sur : https://console.anthropic.com
    # Définir la variable d'environnement ANTHROPIC_API_KEY sur Railway/votre serveur
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
