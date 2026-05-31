"""
Camada de acesso a dados.
- DATABASE_URL definida → usa PostgreSQL (Railway)
- Sem DATABASE_URL       → usa arquivos JSON locais
"""

import json
import logging
import os
import secrets
import uuid
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
_DATA = Path(os.getenv("DATA_DIR", "."))
_CONFIG_FILE     = _DATA / "config.json"
_MESSAGES_FILE   = _DATA / "messages.json"
_EMOJI_MAP_FILE  = _DATA / "emoji_map.json"
_USERS_FILE      = _DATA / "users.json"
_SECRET_KEY_FILE = _DATA / ".secret_key"

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
        cur = c.cursor()
        cur.execute("""
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
                id         TEXT PRIMARY KEY,
                name       TEXT    NOT NULL,
                text       TEXT    NOT NULL DEFAULT '',
                image      TEXT,
                video_note TEXT,
                active     BOOLEAN DEFAULT TRUE,
                parse_mode TEXT    DEFAULT 'HTML'
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
            CREATE TABLE IF NOT EXISTS emoji_map (
                emoji_char      TEXT PRIMARY KEY,
                custom_emoji_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id       TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT 'user',
                active   BOOLEAN NOT NULL DEFAULT TRUE
            );
        """)
        # Migrations: add columns that may not exist in older deployments
        cur.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS parse_mode TEXT DEFAULT 'HTML'"
        )
        cur.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS video_note TEXT"
        )
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


# ── Secret Key ────────────────────────────────────────────────────────────────

def get_or_create_secret_key() -> str:
    """Retorna a chave secreta do Flask, gerando e persistindo se necessário."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key

    if not use_postgres():
        if _SECRET_KEY_FILE.exists():
            return _SECRET_KEY_FILE.read_text().strip()
        key = secrets.token_hex(32)
        _SECRET_KEY_FILE.write_text(key)
        return key

    # PostgreSQL: ler ou gerar na tabela settings
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT value FROM settings WHERE key='secret_key'")
        row = cur.fetchone()
        if row:
            return row[0]

    key = secrets.token_hex(32)
    with _conn() as c:
        c.cursor().execute(
            "INSERT INTO settings(key,value) VALUES('secret_key',%s) "
            "ON CONFLICT(key) DO NOTHING",
            (key,),
        )
    return key


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not use_postgres():
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        _apply_env(cfg)
        return cfg

    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT key, value FROM settings WHERE key != 'secret_key'")
        settings = dict(cur.fetchall())

        cur.execute("SELECT id, name, token, active FROM bots ORDER BY name")
        bots = [{"id": r[0], "name": r[1], "token": r[2], "active": r[3]} for r in cur.fetchall()]

        cur.execute("SELECT key, name, chat_id, default_buttons FROM groups ORDER BY key")
        groups = {r[0]: {"name": r[1], "id": r[2], "default_buttons": r[3]} for r in cur.fetchall()}

        cur.execute("SELECT key, label, url FROM button_configs ORDER BY key")
        button_configs = {r[0]: {"label": r[1], "url": r[2]} for r in cur.fetchall()}

    cfg = {
        "bot_token":      settings.get("bot_token", "SEU_TOKEN_AQUI"),
        "timezone":       settings.get("timezone", "America/Sao_Paulo"),
        "web_port":       int(settings.get("web_port", "5000")),
        "bots":           bots,
        "groups":         groups,
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
        cur.execute("SELECT id, name, text, image, video_note, active, parse_mode FROM messages ORDER BY name")
        msgs = []
        for mid, name, text, image, video_note, active, parse_mode in cur.fetchall():
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
            msgs.append({"id": mid, "name": name, "text": text, "image": image,
                          "video_note": video_note, "active": active,
                          "parse_mode": parse_mode or "HTML", "schedules": schedules})
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
                "INSERT INTO messages(id,name,text,image,video_note,active,parse_mode) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (msg["id"], msg.get("name",""), msg.get("text",""),
                 msg.get("image"), msg.get("video_note"),
                 msg.get("active", True), msg.get("parse_mode", "HTML"))
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
    """Cria ou atualiza uma mensagem individual."""
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
            "INSERT INTO messages(id,name,text,image,video_note,active,parse_mode) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, text=EXCLUDED.text, "
            "image=EXCLUDED.image, video_note=EXCLUDED.video_note, "
            "active=EXCLUDED.active, parse_mode=EXCLUDED.parse_mode",
            (msg["id"], msg.get("name",""), msg.get("text",""),
             msg.get("image"), msg.get("video_note"),
             msg.get("active", True), msg.get("parse_mode", "HTML"))
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


def set_video_note_file_id(local_path: str, file_id: str) -> None:
    """Substitui o caminho local pelo file_id do Telegram no campo video_note
    de todas as mensagens que apontam para esse arquivo."""
    new_value = f"file_id:{file_id}"
    if not use_postgres():
        data = load_messages()
        changed = False
        for msg in data["messages"]:
            if msg.get("video_note") == local_path:
                msg["video_note"] = new_value
                changed = True
        if changed:
            save_messages(data)
        return
    with _conn() as c:
        c.cursor().execute(
            "UPDATE messages SET video_note=%s WHERE video_note=%s",
            (new_value, local_path)
        )


def delete_message(message_id: str) -> None:
    if not use_postgres():
        data = load_messages()
        data["messages"] = [m for m in data["messages"] if m["id"] != message_id]
        save_messages(data)
        return
    with _conn() as c:
        c.cursor().execute("DELETE FROM messages WHERE id=%s", (message_id,))


# ── Emoji Map ─────────────────────────────────────────────────────────────────

def load_emoji_map() -> dict:
    """Retorna {emoji_char: custom_emoji_id}."""
    if not use_postgres():
        if not _EMOJI_MAP_FILE.exists():
            return {}
        try:
            return json.loads(_EMOJI_MAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT emoji_char, custom_emoji_id FROM emoji_map ORDER BY emoji_char")
        return {r[0]: r[1] for r in cur.fetchall()}


def save_emoji(emoji_char: str, custom_emoji_id: str) -> None:
    """Registra ou atualiza o mapeamento emoji_char → custom_emoji_id."""
    if not use_postgres():
        data = load_emoji_map()
        data[emoji_char] = custom_emoji_id
        _EMOJI_MAP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    with _conn() as c:
        c.cursor().execute(
            "INSERT INTO emoji_map(emoji_char, custom_emoji_id) VALUES(%s,%s) "
            "ON CONFLICT(emoji_char) DO UPDATE SET custom_emoji_id=EXCLUDED.custom_emoji_id",
            (emoji_char, custom_emoji_id),
        )


def delete_emoji(emoji_char: str) -> None:
    """Remove um mapeamento de emoji animado."""
    if not use_postgres():
        data = load_emoji_map()
        data.pop(emoji_char, None)
        _EMOJI_MAP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    with _conn() as c:
        c.cursor().execute("DELETE FROM emoji_map WHERE emoji_char=%s", (emoji_char,))


# ── Users ─────────────────────────────────────────────────────────────────────

def _load_users_raw() -> list:
    """JSON mode: carrega lista completa de usuários (inclui hash de senha)."""
    if not _USERS_FILE.exists():
        return []
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_users_raw(users: list) -> None:
    _USERS_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_users() -> list:
    """Retorna todos os usuários sem o hash de senha."""
    if not use_postgres():
        return [
            {k: v for k, v in u.items() if k != "password"}
            for u in _load_users_raw()
        ]
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, username, role, active FROM users ORDER BY username")
        return [{"id": r[0], "username": r[1], "role": r[2], "active": r[3]}
                for r in cur.fetchall()]


def get_user_by_id(user_id: str) -> dict | None:
    """Retorna usuário pelo ID sem senha."""
    if not use_postgres():
        for u in _load_users_raw():
            if u["id"] == user_id:
                return {k: v for k, v in u.items() if k != "password"}
        return None
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, username, role, active FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "role": row[2], "active": row[3]}
    return None


def _get_user_with_password(username: str) -> dict | None:
    """Uso interno (autenticação): retorna usuário com hash de senha."""
    if not use_postgres():
        for u in _load_users_raw():
            if u["username"].lower() == username.lower():
                return u
        return None
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, username, password, role, active FROM users WHERE username=%s",
            (username,),
        )
        row = cur.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "password": row[2],
                    "role": row[3], "active": row[4]}
    return None


def verify_user(username: str, password: str) -> dict | None:
    """Verifica credenciais e retorna usuário sem senha, ou None se inválido."""
    user = _get_user_with_password(username)
    if not user or not user.get("active"):
        return None
    if not check_password_hash(user["password"], password):
        return None
    return {k: v for k, v in user.items() if k != "password"}


def create_user(username: str, password: str, role: str = "user") -> dict:
    """Cria um novo usuário. Retorna o usuário criado (sem senha)."""
    user_id = uuid.uuid4().hex[:8]
    pw_hash = generate_password_hash(password)

    if not use_postgres():
        users = _load_users_raw()
        new = {"id": user_id, "username": username, "password": pw_hash,
               "role": role, "active": True}
        users.append(new)
        _save_users_raw(users)
        return {k: v for k, v in new.items() if k != "password"}

    with _conn() as c:
        c.cursor().execute(
            "INSERT INTO users(id,username,password,role,active) VALUES(%s,%s,%s,%s,%s)",
            (user_id, username, pw_hash, role, True),
        )
    return {"id": user_id, "username": username, "role": role, "active": True}


def update_user(user_id: str, data: dict) -> bool:
    """Atualiza role, active e/ou senha de um usuário."""
    if not use_postgres():
        users = _load_users_raw()
        for i, u in enumerate(users):
            if u["id"] == user_id:
                if data.get("password"):
                    users[i]["password"] = generate_password_hash(data["password"])
                if "role" in data:
                    users[i]["role"] = data["role"]
                if "active" in data:
                    users[i]["active"] = bool(data["active"])
                _save_users_raw(users)
                return True
        return False

    with _conn() as c:
        cur = c.cursor()
        if data.get("password"):
            cur.execute("UPDATE users SET password=%s WHERE id=%s",
                        (generate_password_hash(data["password"]), user_id))
        if "role" in data:
            cur.execute("UPDATE users SET role=%s WHERE id=%s", (data["role"], user_id))
        if "active" in data:
            cur.execute("UPDATE users SET active=%s WHERE id=%s",
                        (bool(data["active"]), user_id))
    return True


def delete_user(user_id: str) -> bool:
    """Remove um usuário. Retorna False se não encontrado."""
    if not use_postgres():
        users = _load_users_raw()
        new = [u for u in users if u["id"] != user_id]
        if len(new) == len(users):
            return False
        _save_users_raw(new)
        return True
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        return cur.rowcount > 0


def create_default_admin() -> None:
    """Cria o usuário admin padrão se não houver nenhum usuário cadastrado."""
    if load_users():
        return
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    create_user("admin", password, role="admin")
    logger.info(
        "Usuário admin padrão criado — username: admin | "
        "defina ADMIN_PASSWORD no ambiente para personalizar a senha inicial"
    )


# ── Emoji defaults ────────────────────────────────────────────────────────────

# Mapeamento padrão do pack animado COMPRA.
# Estes IDs são aplicados/sobrescritos a cada inicialização para garantir
# que os emojis corretos estejam sempre no banco.
_COMPRA_EMOJI_MAP: dict = {
    "C": "5330523098347218561",
    "O": "5361583176550457135",
    "M": "5332321341024508571",
    "P": "5361909160273255840",
    "R": "5332514996804918116",
    "A": "5226734466315067436",
}


def seed_emoji_defaults() -> None:
    """Garante que os emojis animados do pack COMPRA estejam registrados."""
    try:
        for char, emoji_id in _COMPRA_EMOJI_MAP.items():
            save_emoji(char, emoji_id)
        logger.info("Emojis padrão COMPRA aplicados (%d letras)", len(_COMPRA_EMOJI_MAP))
    except Exception as exc:
        logger.warning("Não foi possível aplicar emojis padrão (não crítico): %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_env(cfg: dict) -> None:
    token = os.getenv("BOT_TOKEN")
    if token:
        cfg["bot_token"] = token
        for b in cfg.get("bots", []):
            if b.get("active"):
                b["token"] = token
