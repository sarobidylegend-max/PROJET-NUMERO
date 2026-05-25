# 📦 Déploiement du Bot Telegram sur Railway

## Étape 1 — Créer un dépôt GitHub

1. Va sur https://github.com et connecte-toi (ou crée un compte gratuit)
2. Clique sur **"New repository"** (bouton vert)
3. Nom : `mon-bot-telegram` (ou ce que tu veux)
4. Laisse tout par défaut et clique **"Create repository"**
5. Sur la page suivante, clique **"uploading an existing file"**
6. Glisse-dépose TOUS les fichiers de ce ZIP dans la page
7. Clique **"Commit changes"**

## Étape 2 — Déployer sur Railway

1. Va sur https://railway.app
2. Clique **"Login"** → **"Login with GitHub"**
3. Clique **"New Project"**
4. Choisis **"Deploy from GitHub repo"**
5. Sélectionne ton dépôt `mon-bot-telegram`
6. Railway va détecter automatiquement le projet Python ✅

## Étape 3 — Configurer les 2 bots (IMPORTANT)

Railway doit lancer 2 processus séparément.

1. Dans ton projet Railway, va dans **Settings** → **Services**
2. Tu verras que Railway a créé 1 service. Clique dessus.
3. Va dans l'onglet **"Deploy"** → **"Start Command"** → entre : `python bot.py`
4. Clique sur **"+ New Service"** → **"GitHub Repo"** → même dépôt
5. Pour ce 2ème service → **Start Command** : `python enroll_bot.py`

## Étape 4 — Variables d'environnement (sécurité)

Pour chaque service, va dans **"Variables"** et ajoute :

| Variable | Valeur |
|---|---|
| `BOT_TOKEN` | Token de ton bot principal |
| `ENROLL_BOT_TOKEN` | Token du bot d'enrôlement |
| `ENROLL_BOT_USERNAME` | `demandaccess_bot` |
| `ADMIN_ID` | Ton ID Telegram |
| `HEROSMS_API_KEY` | Ta clé API HeroSMS |

## Étape 5 — Vérifier que ça tourne

- Dans chaque service, va dans l'onglet **"Logs"**
- Tu dois voir `Bot started` ou similaire sans erreur

## ⚠️ Important — Changer tes tokens

Les tokens dans config.py sont visibles publiquement si ton repo est public.
→ Mets ton repo en **PRIVÉ** sur GitHub (Settings → Danger Zone → Make private)
→ Ou change tes tokens via @BotFather sur Telegram

## 🆓 Plan gratuit Railway

Railway offre **5$ de crédit/mois** gratuit.
2 bots légers = environ 0.50$/mois → largement suffisant ✅
