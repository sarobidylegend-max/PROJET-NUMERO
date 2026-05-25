"""
Gestionnaire d'utilisateurs — MongoDB Atlas
"""

import os
from datetime import datetime
from pymongo import MongoClient

_client = None
_db     = None
_col    = None

def _get_col():
    global _client, _db, _col
    if _col is None:
        mongo_url = os.environ.get("MONGO_URL")
        if not mongo_url:
            raise RuntimeError("Variable d'environnement MONGO_URL manquante !")
        _client = MongoClient(mongo_url)
        _db     = _client["telebot"]
        _col    = _db["users"]
    return _col

def _now() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")

def is_authorized(uid: int) -> bool:
    return _get_col().find_one({"_id": str(uid), "status": {"$in": ["agent", "sub_admin"]}}) is not None

def is_sub_admin(uid: int) -> bool:
    return _get_col().find_one({"_id": str(uid), "status": "sub_admin"}) is not None

def is_pending(uid: int) -> bool:
    return _get_col().find_one({"_id": str(uid), "status": "pending"}) is not None

def is_banned(uid: int) -> bool:
    return _get_col().find_one({"_id": str(uid), "status": "banned"}) is not None

def is_restricted(uid: int) -> bool:
    u = _get_col().find_one({"_id": str(uid), "status": "agent"})
    return u is not None and u.get("restricted", False)

def get_all_users() -> dict:
    agents, sub_admins, pending, banned = {}, {}, {}, {}
    for u in _get_col().find():
        sid = str(u["uid"])
        if u["status"] == "agent": agents[sid] = u
        elif u["status"] == "sub_admin": sub_admins[sid] = u
        elif u["status"] == "pending": pending[sid] = u
        elif u["status"] == "banned": banned[sid] = u
    return {"agents": agents, "sub_admins": sub_admins, "pending": pending, "banned": banned}

def get_authorized_ids() -> list:
    return [u["uid"] for u in _get_col().find({"status": {"$in": ["agent", "sub_admin"]}})]

def get_admins() -> dict:
    return {str(u["uid"]): u for u in _get_col().find({"status": "sub_admin"})}

def get_user_info(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid)})
    if u is None: return None
    u.pop("_id", None)
    if u["status"] == "agent":
        u["status"] = "restricted" if u.get("restricted") else "agent"
    return u

def get_user_stats(uid: int) -> dict:
    u = _get_col().find_one({"_id": str(uid), "status": {"$in": ["agent", "sub_admin"]}})
    if u is None:
        return {"total_achats": 0, "total_declines": 0, "last_number": None, "last_activity": None}
    return {"total_achats": u.get("total_achats", 0), "total_declines": u.get("total_declines", 0),
            "last_number": u.get("last_number"), "last_activity": u.get("last_activity")}

def increment_achats(uid: int, number: str):
    _get_col().update_one(
        {"_id": str(uid), "status": {"$in": ["agent", "sub_admin"]}},
        {"$inc": {"total_achats": 1}, "$set": {"last_number": number, "last_activity": _now()}}
    )

def increment_declines(uid: int):
    _get_col().update_one(
        {"_id": str(uid), "status": {"$in": ["agent", "sub_admin"]}},
        {"$inc": {"total_declines": 1}, "$set": {"last_activity": _now()}}
    )

def add_pending(uid: int, username: str, first_name: str) -> bool:
    if _get_col().find_one({"_id": str(uid)}) is not None: return False
    _get_col().insert_one({
        "_id": str(uid), "uid": uid, "status": "pending",
        "first_name": first_name or "Inconnu", "username": username or "",
        "requested_at": _now(), "total_achats": 0, "total_declines": 0,
        "last_number": None, "last_activity": None, "restricted": False, "restrict_reason": "",
    })
    return True

def approve_user(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "pending"})
    if u is None: return None
    _get_col().update_one({"_id": str(uid)}, {"$set": {"status": "agent", "approved_at": _now()}})
    return _get_col().find_one({"_id": str(uid)})

def reject_user(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "pending"})
    if u is None: return None
    _get_col().delete_one({"_id": str(uid)})
    return u

def remove_user(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "agent"})
    if u is None: return None
    _get_col().delete_one({"_id": str(uid)})
    return u

def restrict_user(uid: int, reason: str = "") -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "agent"})
    if u is None: return None
    _get_col().update_one({"_id": str(uid)}, {"$set": {"restricted": True, "restrict_reason": reason or "Restriction manuelle"}})
    return _get_col().find_one({"_id": str(uid)})

def unrestrict_user(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "agent"})
    if u is None: return None
    _get_col().update_one({"_id": str(uid)}, {"$set": {"restricted": False, "restrict_reason": ""}})
    return _get_col().find_one({"_id": str(uid)})

def ban_user(uid: int) -> dict | None:
    _get_col().update_one({"_id": str(uid)}, {"$set": {"status": "banned", "banned_at": _now()}}, upsert=True)
    return _get_col().find_one({"_id": str(uid)})

def unban_user(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "banned"})
    if u is None: return None
    _get_col().delete_one({"_id": str(uid)})
    return u

def promote_to_admin(uid: int, promoted_by: int, first_name: str = "", username: str = "") -> dict | None:
    existing = _get_col().find_one({"_id": str(uid)})
    if existing and existing["status"] == "sub_admin": return existing
    if existing:
        _get_col().update_one({"_id": str(uid)}, {"$set": {"status": "sub_admin", "promoted_at": _now()}})
    else:
        _get_col().insert_one({
            "_id": str(uid), "uid": uid, "status": "sub_admin",
            "first_name": first_name or "Inconnu", "username": username or "",
            "promoted_at": _now(), "total_achats": 0, "total_declines": 0,
            "last_number": None, "last_activity": None, "restricted": False, "restrict_reason": "",
        })
    return _get_col().find_one({"_id": str(uid)})

def revoke_admin(uid: int) -> dict | None:
    u = _get_col().find_one({"_id": str(uid), "status": "sub_admin"})
    if u is None: return None
    _get_col().update_one({"_id": str(uid)}, {"$set": {"status": "agent", "approved_at": _now()}})
    return _get_col().find_one({"_id": str(uid)})

def get_leaderboard() -> list:
    users = []
    for u in _get_col().find({"status": {"$in": ["agent", "sub_admin"]}}):
        users.append({
            "uid": u["uid"], "first_name": u.get("first_name", "Inconnu"),
            "username": u.get("username", ""), "total_achats": u.get("total_achats", 0),
            "total_declines": u.get("total_declines", 0), "last_activity": u.get("last_activity"),
            "last_number": u.get("last_number"),
            "status": "restricted" if (u["status"] == "agent" and u.get("restricted")) else u["status"],
            "restrict_reason": u.get("restrict_reason", ""),
        })
    return sorted(users, key=lambda x: x["total_achats"], reverse=True)
