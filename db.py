"""
Camada de acesso a dados.
- DATABASE_URL definida → usa PostgreSQL (Railway)
- Sem DATABASE_URL       → usa arquivos JSON locais
"""

import json
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
_DATA = Path(os.getenv("DATA_DIR", "."))
_CONFIG_FILE = _DATA / "config.json"
_MESSAGES_FILE = _DATA / "messages.json"

_pool = None


def use_postgres() -> bool:
    return bool(DATABASE_URL)


# ── Pool de conexões ──────────────────────────────────────────────────────────

def _get_pool():
    global _pool
    if _pool is None:
        from psycopg2 import pool as pgpool
        _pool = pgpool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        logger.info("Pool PostgreSQL criado")
    return _pool


@contextmanager
def _conn():
    p = _get_pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c)


# ── Inicialização ─────────────────────────────────────────────────────────────

def init_db() -> None:
    if not use_postgres():
        return
    with _conn() as c:
        c.cursor().execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS bots (
                id     TEXT PRIMARY KEY,
                name   TEXT    NOT NULL,
                token  TEXT    NOT NULL,
                active BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS groups (
                key             TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                chat_id         TEXT NOT NULL DEFAULT '',
                default_buttons JSONB NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS button_configs (
                key   TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                url   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id     TEXT PRIMARY KEY,
                name   TEXT    NOT NULL,
                text   TEXT    NOT NULL DEFAULT '',
                image  TEXT,
                active BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id         TEXT PRIMARY KEY,
                message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                group_key  TEXT    NOT NULL DEFAULT '',
                time       TEXT    NOT NULL DEFAULT '09:00',
                days       JSONB   NOT NULL DEFAULT '[]',
                buttons    JSONB   NOT NULL DEFAULT '[]',
                active     BOOLEAN DEFAULT TRUE
            );
        """)
    logger.info("Banco de dados PostgreSQL pronto")


def migrate_from_json() -> None:
    """Importa JSON locais para o PostgreSQL na primeira execução."""
    if not use_postgres():
        return
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM settings")
        if cur.fetchone()[0] > 0:
            return  # já migrado

    try:
        if _CONFIG_FILE.exists():
            save_config(json.loads(_CONFIG_FILE.read_text(encoding="utf-8")))
            logger.info("config.json migrado para PostgreSQL")
    except Exception as exc:
        logger.warning("Falha ao migrar config: %s", exc)

    try:
        if _MESSAGES_FILE.exists():
            save_messages(json.loads(_MESSAGES_FILE.read_text(encoding="utf-8")))
            logger.info("messages.json migrado para PostgreSQL")
    except Exception as exc:
        logger.warning("Falha ao migrar mensagens: %s", exc)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not use_postgres():
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        _apply_env(cfg)
        return cfg

    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT key, value FROM settings")
        settings = dict(cur.fetchall())

        cur.execute("SELECT id, name, token, active FROM bots ORDER BY name")
        bots = [{"id": r[0], "name": r[1], "token": r[2], "active": r[3]} for r in cur.fetchall()]

        cur.execute("SELECT key, name, chat_id, default_buttons FROM groups ORDER BY key")
        groups = {r[0]: {"name": r[1], "id": r[2], "default_buttons": r[3]} for r in cur.fetchall()}

        cur.execute("SELECT key, label, url FROM button_configs ORDER BY key")
        button_configs = {r[0]: {"label": r[1], "url": r[2]} for r in cur.fetchall()}

    cfg = {
        "bot_token":    settings.get("bot_token", "SEU_TOKEN_AQUI"),
        "timezone":     settings.get("timezone", "America/Sao_Paulo"),
        "web_port":     int(settings.get("web_port", "5000")),
        "bots":         bots,
        "groups":       groups,
        "button_configs": button_configs,
    }
    _apply_env(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    if not use_postgres():
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return

    with _conn() as c:
        cur = c.cursor()
        for key in ("bot_token", "timezone", "web_port"):
            if key in cfg:
                cur.execute(
                    "INSERT INTO settings(key,value) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (key, str(cfg[key]))
                )

        cur.execute("DELETE FROM bots")
        for b in cfg.get("bots", []):
            cur.execute(
                "INSERT INTO bots(id,name,token,active) VALUES(%s,%s,%s,%s)",
                (b["id"], b["name"], b["token"], b.get("active", False))
            )

        cur.execute("DELETE FROM groups")
        for key, g in cfg.get("groups", {}).items():
            cur.execute(
                "INSERT INTO groups(key,name,chat_id,default_buttons) VALUES(%s,%s,%s,%s)",
                (key, g.get("name",""), g.get("id",""), json.dumps(g.get("default_buttons",[])))
            )

        cur.execute("DELETE FROM button_configs")
        for key, b in cfg.get("button_configs", {}).items():
            cur.execute(
                "INSERT INTO button_configs(key,label,url) VALUES(%s,%s,%s)",
                (key, b.get("label",""), b.get("url",""))
            )


# ── Messages ──────────────────────────────────────────────────────────────────

def load_messages() -> dict:
    if not use_postgres():
        with open(_MESSAGES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, name, text, image, active FROM messages ORDER BY name")
        msgs = []
        for mid, name, text, image, active in cur.fetchall():
            cur.execute(
                "SELECT id, group_key, time, days, buttons, active "
                "FROM schedules WHERE message_id=%s ORDER BY time",
                (mid,)
            )
            schedules = [
                {"id": r[0], "group": r[1], "time": r[2],
                 "days": r[3], "buttons": r[4], "active": r[5]}
                for r in cur.fetchall()
            ]
            msgs.append({"id": mid, "name": name, "text": text,
                          "image": image, "active": active, "schedules": schedules})
    return {"messages": msgs}


def save_messages(data: dict) -> None:
    if not use_postgres():
        with open(_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM messages")
        for msg in data.get("messages", []):
            cur.execute(
                "INSERT INTO messages(id,name,text,image,active) VALUES(%s,%s,%s,%s,%s)",
                (msg["id"], msg.get("name",""), msg.get("text",""),
                 msg.get("image"), msg.get("active", True))
            )
            for s in msg.get("schedules", []):
                cur.execute(
                    "INSERT INTO schedules(id,message_id,group_key,time,days,buttons,active) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (s["id"], msg["id"], s.get("group",""), s.get("time","09:00"),
                     json.dumps(s.get("days",[])), json.dumps(s.get("buttons",[])),
                     s.get("active", True))
                )


def upsert_message(msg: dict) -> dict:
    """Cria ou atualiza uma mensagem individual (mais eficiente que save_messages)."""
    if not use_postgres():
        data = load_messages()
        idx = next((i for i, m in enumerate(data["messages"]) if m["id"] == msg["id"]), None)
        if idx is None:
            data["messages"].append(msg)
        else:
            data["messages"][idx] = msg
        save_messages(data)
        return msg

    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO messages(id,name,text,image,active) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, text=EXCLUDED.text, "
            "image=EXCLUDED.image, active=EXCLUDED.active",
            (msg["id"], msg.get("name",""), msg.get("text",""),
             msg.get("image"), msg.get("active", True))
        )
        cur.execute("DELETE FROM schedules WHERE message_id=%s", (msg["id"],))
        for s in msg.get("schedules", []):
            sid = s.get("id") or uuid.uuid4().hex[:8]
            cur.execute(
                "INSERT INTO schedules(id,message_id,group_key,time,days,buttons,active) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (sid, msg["id"], s.get("group",""), s.get("time","09:00"),
                 json.dumps(s.get("days",[])), json.dumps(s.get("buttons",[])),
                 s.get("active", True))
            )
    return msg


def delete_message(message_id: str) -> None:
    if not use_postgres():
        data = load_messages()
        data["messages"] = [m for m in data["messages"] if m["id"] != message_id]
        save_messages(data)
        return
    with _conn() as c:
        c.cursor().execute("DELETE FROM messages WHERE id=%s", (message_id,))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_env(cfg: dict) -> None:
    token = os.getenv("BOT_TOKEN")
    if token:
        cfg["bot_token"] = token
        for b in cfg.get("bots", []):
            if b.get("active"):
                b["token"] = token
