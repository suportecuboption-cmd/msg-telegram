import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Callable

from flask import Flask, jsonify, request, render_template

logger = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")
MESSAGES_FILE = Path("messages.json")

_manager = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_reload_callback: Optional[Callable] = None
_restart_callback: Optional[Callable] = None


def set_context(manager, loop: asyncio.AbstractEventLoop,
                reload_callback: Callable, restart_callback: Callable) -> None:
    global _manager, _loop, _reload_callback, _restart_callback
    _manager = manager
    _loop = loop
    _reload_callback = reload_callback
    _restart_callback = restart_callback


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_messages() -> dict:
    with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_messages(data: dict) -> None:
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _ensure_schedule_ids(schedules: list) -> list:
    for s in schedules:
        if not s.get("id"):
            s["id"] = uuid.uuid4().hex[:8]
    return schedules


def _check_token(token: str) -> dict:
    """Valida um token chamando a API do Telegram diretamente."""
    if not token or token == "SEU_TOKEN_AQUI":
        return {"online": False, "reason": "Token não configurado"}
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            r = data["result"]
            return {
                "online": True,
                "name": r.get("first_name", ""),
                "username": r.get("username", ""),
                "id": r.get("id"),
            }
        return {"online": False, "reason": data.get("description", "Erro")}
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read()).get("description", str(exc))
        except Exception:
            reason = str(exc)
        return {"online": False, "reason": reason}
    except Exception as exc:
        return {"online": False, "reason": str(exc)}


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")

    # ── Bot instances ─────────────────────────────────────────────────────────

    @app.route("/api/bots", methods=["GET"])
    def list_bots():
        cfg = _load_config()
        result = []
        for b in cfg.get("bots", []):
            status = _check_token(b.get("token", ""))
            result.append({**b, "status": status})
        return jsonify(result)

    @app.route("/api/bots/check", methods=["POST"])
    def check_bot():
        token = (request.get_json(force=True) or {}).get("token", "")
        return jsonify(_check_token(token))

    @app.route("/api/bots", methods=["POST"])
    def add_bot():
        data = request.get_json(force=True)
        cfg = _load_config()
        if "bots" not in cfg:
            cfg["bots"] = []
        new_bot = {
            "id": uuid.uuid4().hex[:8],
            "name": data.get("name", "Novo Bot"),
            "token": data.get("token", ""),
            "active": False,
        }
        cfg["bots"].append(new_bot)
        _save_config(cfg)
        return jsonify(new_bot), 201

    @app.route("/api/bots/<bot_id>", methods=["PUT"])
    def update_bot(bot_id):
        data = request.get_json(force=True)
        cfg = _load_config()
        for i, b in enumerate(cfg.get("bots", [])):
            if b["id"] == bot_id:
                cfg["bots"][i]["name"]  = data.get("name",  b["name"])
                cfg["bots"][i]["token"] = data.get("token", b["token"])
                # Se for o bot ativo, atualiza bot_token e reinicia
                if b.get("active"):
                    cfg["bot_token"] = cfg["bots"][i]["token"]
                    _save_config(cfg)
                    if _restart_callback:
                        _restart_callback(cfg["bot_token"])
                else:
                    _save_config(cfg)
                return jsonify(cfg["bots"][i])
        return jsonify({"error": "Bot não encontrado"}), 404

    @app.route("/api/bots/<bot_id>/activate", methods=["POST"])
    def activate_bot(bot_id):
        cfg = _load_config()
        activated = None
        for b in cfg.get("bots", []):
            b["active"] = b["id"] == bot_id
            if b["active"]:
                activated = b
        if not activated:
            return jsonify({"error": "Bot não encontrado"}), 404
        cfg["bot_token"] = activated["token"]
        _save_config(cfg)
        if _restart_callback:
            _restart_callback(activated["token"])
        return jsonify({"success": True})

    @app.route("/api/bots/<bot_id>", methods=["DELETE"])
    def delete_bot(bot_id):
        cfg = _load_config()
        before = len(cfg.get("bots", []))
        cfg["bots"] = [b for b in cfg.get("bots", []) if b["id"] != bot_id]
        if len(cfg["bots"]) == before:
            return jsonify({"error": "Bot não encontrado"}), 404
        _save_config(cfg)
        return jsonify({"success": True})

    # ── Status ────────────────────────────────────────────────────────────────

    @app.route("/api/status", methods=["GET"])
    def get_status():
        bot = _manager.bot if _manager else None
        if not bot or not _loop:
            return jsonify({"online": False, "reason": "Bot não inicializado"})
        try:
            future = asyncio.run_coroutine_threadsafe(bot.get_me(), _loop)
            info = future.result(timeout=5)
            return jsonify({
                "online": True,
                "name": info.full_name,
                "username": info.username,
                "id": info.id,
            })
        except Exception as exc:
            return jsonify({"online": False, "reason": str(exc)})

    # ── Config ────────────────────────────────────────────────────────────────

    @app.route("/api/config", methods=["GET"])
    def get_config():
        cfg = _load_config()
        return jsonify({k: v for k, v in cfg.items() if k != "bot_token"})

    @app.route("/api/config", methods=["PUT"])
    def update_config():
        data = request.get_json(force=True)
        cfg = _load_config()
        for key in ("groups", "button_configs", "timezone", "web_port"):
            if key in data:
                cfg[key] = data[key]
        _save_config(cfg)
        if _reload_callback:
            _reload_callback()
        return jsonify({"success": True})

    # ── Messages ──────────────────────────────────────────────────────────────

    @app.route("/api/messages", methods=["GET"])
    def get_messages():
        return jsonify(_load_messages())

    @app.route("/api/messages", methods=["POST"])
    def create_message():
        data = request.get_json(force=True)
        store = _load_messages()
        message = {
            "id": uuid.uuid4().hex[:8],
            "name": data.get("name", "Nova Mensagem"),
            "text": data.get("text", ""),
            "image": data.get("image") or None,
            "active": data.get("active", True),
            "schedules": _ensure_schedule_ids(data.get("schedules", [])),
        }
        store["messages"].append(message)
        _save_messages(store)
        if _reload_callback:
            _reload_callback()
        return jsonify(message), 201

    @app.route("/api/messages/<message_id>", methods=["PUT"])
    def update_message(message_id):
        data = request.get_json(force=True)
        store = _load_messages()
        for i, msg in enumerate(store["messages"]):
            if msg["id"] == message_id:
                data["id"] = message_id
                data["schedules"] = _ensure_schedule_ids(data.get("schedules", []))
                store["messages"][i] = data
                _save_messages(store)
                if _reload_callback:
                    _reload_callback()
                return jsonify(data)
        return jsonify({"error": "Mensagem não encontrada"}), 404

    @app.route("/api/messages/<message_id>", methods=["DELETE"])
    def delete_message(message_id):
        store = _load_messages()
        store["messages"] = [m for m in store["messages"] if m["id"] != message_id]
        _save_messages(store)
        if _reload_callback:
            _reload_callback()
        return jsonify({"success": True})

    # ── Send Now ──────────────────────────────────────────────────────────────

    @app.route("/api/messages/<message_id>/send-now", methods=["POST"])
    def send_now(message_id):
        bot = _manager.bot if _manager else None
        if not bot or not _loop:
            return jsonify({"error": "Bot não disponível — configure e ative um token"}), 503

        data = request.get_json(force=True) or {}
        cfg = _load_config()
        store = _load_messages()

        message = next((m for m in store["messages"] if m["id"] == message_id), None)
        if not message:
            return jsonify({"error": "Mensagem não encontrada"}), 404

        group_key = data.get("group")
        groups_to_send = [group_key] if group_key else list(cfg["groups"].keys())

        from bot import send_scheduled_message

        results = []
        for gk in groups_to_send:
            group = cfg["groups"].get(gk)
            if not group or not group.get("id"):
                results.append({"group": gk, "success": False, "error": "Grupo sem ID"})
                continue
            button_keys = group.get("default_buttons", [])
            for sched in message.get("schedules", []):
                if sched.get("group") == gk:
                    button_keys = sched.get("buttons", button_keys)
                    break
            future = asyncio.run_coroutine_threadsafe(
                send_scheduled_message(
                    bot, message["text"], group["id"],
                    button_keys, cfg, message.get("image"), message.get("name", ""),
                ),
                _loop,
            )
            try:
                future.result(timeout=30)
                results.append({"group": gk, "success": True})
            except Exception as exc:
                results.append({"group": gk, "success": False, "error": str(exc)})

        return jsonify({"results": results})

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    return app
