# Configuration proxy sur Railway — Rotation d'IP automatique

## Pourquoi c'est nécessaire

Railway utilise une IP fixe partagée. HeroSMS peut bloquer ou limiter les
requêtes `getNumber` venant d'une même IP trop souvent. La rotation d'IP via
proxy résout ce problème en changeant l'IP à chaque requête (ou au hasard).

---

## Option 1 — Proxy rotatif unique (✅ Recommandé)

Services compatibles : **WebShare**, **BrightData**, **Oxylabs**, **ProxyMesh**

### WebShare (gratuit jusqu'à 10 proxies)
1. Créez un compte sur https://proxy.webshare.io
2. Dans Dashboard → Proxy → Rotating Endpoint, copiez votre endpoint
3. Sur Railway → Variables d'environnement, ajoutez :

```
ROTATING_PROXY=http://VOTRE_USER:VOTRE_PASS@rotating.proxy.webshare.io:6131
```

L'IP change automatiquement à chaque connexion.

---

## Option 2 — Liste de proxies fixes

Si vous avez une liste de proxies HTTP, ajoutez sur Railway :

```
PROXY_LIST=http://user:pass@host1:port,http://user:pass@host2:port,http://user:pass@host3:port
```

Le bot choisira un proxy aléatoire à chaque requête.

---

## Variable optionnelle

| Variable | Défaut | Rôle |
|---|---|---|
| `ROTATING_PROXY` | _(vide)_ | Proxy rotatif unique |
| `PROXY_LIST` | _(vide)_ | Liste de proxies séparés par `,` |
| `HEROSMS_MAX_RETRIES` | `3` | Nombre de tentatives en cas d'échec |

> **Si aucun proxy n'est configuré**, le bot fonctionne comme avant (sans rotation).

---

## Vérification dans les logs Railway

Après redémarrage, vous devriez voir :
```
[HeroSMS] 1 proxy(s) chargé(s).
```
ou
```
[HeroSMS] Aucun proxy configuré. Requêtes sans rotation d'IP.
```
