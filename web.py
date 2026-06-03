import asyncio
import functools
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Callable

from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)

logger = logging.getLogger(__name__)

import db as _db

# Uploads ficam no volume persistente (DATA_DIR) para sobreviver a redeploys
_DATA = Path(os.getenv("DATA_DIR", "."))
_UPLOAD_DIR = _DATA / "uploads"

_manager = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_reload_callback: Optional[Callable] = None
_start_bot_callback: Optional[Callable] = None
_stop_bot_callback: Optional[Callable] = None

# Quando NO_AUTH=1/true o login é desativado (útil para dev local)
NO_AUTH = os.getenv("NO_AUTH", "").lower() in ("1", "true", "yes")

# Lista de pares usada como fallback se a API de candles estiver indisponível
_DEFAULT_PARES = [
    {"symbol": "AMAZON-OTC", "nome": "Amazon",     "ativo": True},
    {"symbol": "APPLE-OTC",  "nome": "Apple",      "ativo": True},
    {"symbol": "AUDUSD-OTC", "nome": "AUD/USD",    "ativo": True},
    {"symbol": "BTCUSD-OTC", "nome": "Bitcoin",    "ativo": True},
    {"symbol": "ETHUSD-OTC", "nome": "Ethereum",   "ativo": True},
    {"symbol": "EURGBP-OTC", "nome": "EUR/GBP",    "ativo": True},
    {"symbol": "EURJPY-OTC", "nome": "EUR/JPY",    "ativo": True},
    {"symbol": "EURUSD-OTC", "nome": "EUR/USD",    "ativo": True},
    {"symbol": "GBPJPY-OTC", "nome": "GBP/JPY",    "ativo": True},
    {"symbol": "GBPUSD-OTC", "nome": "GBP/USD",    "ativo": True},
    {"symbol": "MCDON-OTC",  "nome": "McDonald's", "ativo": True},
    {"symbol": "MSFT-OTC",   "nome": "Microsoft",  "ativo": True},
    {"symbol": "NZDUSD-OTC", "nome": "NZD/USD",    "ativo": True},
    {"symbol": "USDCAD-OTC", "nome": "USD/CAD",    "ativo": True},
    {"symbol": "USDJPY-OTC", "nome": "USD/JPY",    "ativo": True},
    {"symbol": "XAUUSD-OTC", "nome": "Ouro",       "ativo": True},
]


def set_context(manager, loop: asyncio.AbstractEventLoop,
                reload_callback: Callable,
                start_bot_callback: Callable,
                stop_bot_callback: Callable) -> None:
    global _manager, _loop, _reload_callback, _start_bot_callback, _stop_bot_callback
    _manager = manager
    _loop = loop
    _reload_callback = reload_callback
    _start_bot_callback = start_bot_callback
    _stop_bot_callback = stop_bot_callback


# ── Helpers internos ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    return _db.load_config()

def _save_config(data: dict) -> None:
    _db.save_config(data)

def _load_messages() -> dict:
    return _db.load_messages()

def _save_messages(data: dict) -> None:
    _db.save_messages(data)

def _ensure_schedule_ids(schedules: list) -> list:
    for s in schedules:
        if not s.get("id"):
            s["id"] = uuid.uuid4().hex[:8]
    return schedules

_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")

def _normalize_media(msg: dict) -> dict:
    """Se um arquivo de vídeo foi salvo no campo image, move para video_note.
    Garante que vídeos sempre sejam enviados como vídeo bolinha (send_video_note)
    e nunca como foto/vídeo normal (send_photo)."""
    img = msg.get("image")
    if img and not str(img).startswith("http") \
            and str(img).lower().endswith(_VIDEO_EXTS):
        if not msg.get("video_note"):
            msg["video_note"] = img
        msg["image"] = None
    return msg


def _check_token(token: str) -> dict:
    if not token or token == "SEU_TOKEN_AQUI":
        return {"online": False, "reason": "Token não configurado"}
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            r = data["result"]
            return {"online": True, "name": r.get("first_name", ""),
                    "username": r.get("username", ""), "id": r.get("id")}
        return {"online": False, "reason": data.get("description", "Erro")}
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read()).get("description", str(exc))
        except Exception:
            reason = str(exc)
        return {"online": False, "reason": reason}
    except Exception as exc:
        return {"online": False, "reason": str(exc)}


# ── Decoradores de autenticação ───────────────────────────────────────────────

def _current_user_id():
    return session.get("user_id") if not NO_AUTH else "noauth"

def _current_role():
    return session.get("role", "admin") if not NO_AUTH else "admin"

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if NO_AUTH:
            return f(*args, **kwargs)
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Não autenticado"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if NO_AUTH:
            return f(*args, **kwargs)
        if not session.get("user_id"):
            return jsonify({"error": "Não autenticado"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Acesso restrito a administradores"}), 403
        return f(*args, **kwargs)
    return decorated


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = _db.get_or_create_secret_key()

    # Garante que o admin padrão exista
    _db.create_default_admin()

    # Middleware: protege todas as rotas exceto auth e static
    @app.before_request
    def require_login():
        if NO_AUTH:
            return None
        exempt = {"/login", "/auth/login", "/auth/logout", "/lp"}
        if request.path in exempt or request.path.startswith("/static/"):
            return None
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Não autenticado"}), 401
            return redirect("/login")
        return None

    # ── Auth ──────────────────────────────────────────────────────────────────

    @app.route("/login")
    def login_page():
        if NO_AUTH or session.get("user_id"):
            return redirect("/")
        return render_template("login.html")

    @app.route("/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(force=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")
        user = _db.verify_user(username, password)
        if not user:
            return jsonify({"error": "Usuário ou senha incorretos"}), 401
        session.permanent = True
        session["user_id"]  = user["id"]
        session["username"] = user["username"]
        session["role"]     = user["role"]
        return jsonify({"success": True, "role": user["role"]})

    @app.route("/auth/logout")
    def auth_logout():
        session.clear()
        return redirect("/login")

    @app.route("/auth/me")
    def auth_me():
        if NO_AUTH:
            return jsonify({"id": "noauth", "username": "dev", "role": "admin"})
        if not session.get("user_id"):
            return jsonify({"error": "Não autenticado"}), 401
        return jsonify({
            "id":       session["user_id"],
            "username": session["username"],
            "role":     session["role"],
        })

    # ── Users (admin only) ────────────────────────────────────────────────────

    @app.route("/api/users", methods=["GET"])
    @admin_required
    def list_users():
        return jsonify(_db.load_users())

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def create_user_route():
        data = request.get_json(force=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        role     = data.get("role", "user")
        if not username or not password:
            return jsonify({"error": "username e password obrigatórios"}), 400
        if role not in ("admin", "user"):
            return jsonify({"error": "role deve ser 'admin' ou 'user'"}), 400
        try:
            user = _db.create_user(username, password, role)
        except Exception as exc:
            return jsonify({"error": f"Erro ao criar usuário: {exc}"}), 400
        return jsonify(user), 201

    @app.route("/api/users/<user_id>", methods=["PUT"])
    @admin_required
    def update_user_route(user_id):
        data = request.get_json(force=True) or {}
        # Impede o admin de se auto-desativar ou de tirar seu próprio role
        if user_id == session.get("user_id") and not NO_AUTH:
            data.pop("active", None)
            data.pop("role", None)
        ok = _db.update_user(user_id, data)
        if not ok:
            return jsonify({"error": "Usuário não encontrado"}), 404
        return jsonify(_db.get_user_by_id(user_id) or {})

    @app.route("/api/users/<user_id>", methods=["DELETE"])
    @admin_required
    def delete_user_route(user_id):
        if user_id == session.get("user_id") and not NO_AUTH:
            return jsonify({"error": "Você não pode excluir sua própria conta"}), 400
        ok = _db.delete_user(user_id)
        if not ok:
            return jsonify({"error": "Usuário não encontrado"}), 404
        return jsonify({"success": True})

    # ── Bot instances ─────────────────────────────────────────────────────────

    @app.route("/api/bots", methods=["GET"])
    @login_required
    def list_bots():
        cfg = _load_config()
        running_ids = set(_manager.running_bot_ids) if _manager else set()
        result = []
        for b in cfg.get("bots", []):
            status = _check_token(b.get("token", ""))
            result.append({**b, "status": status, "running": b["id"] in running_ids})
        return jsonify(result)

    @app.route("/api/bots/check", methods=["POST"])
    @login_required
    def check_bot():
        token = (request.get_json(force=True) or {}).get("token", "")
        return jsonify(_check_token(token))

    @app.route("/api/bots", methods=["POST"])
    @login_required
    def add_bot():
        data = request.get_json(force=True)
        cfg  = _load_config()
        if "bots" not in cfg:
            cfg["bots"] = []
        new_bot = {
            "id":     uuid.uuid4().hex[:8],
            "name":   data.get("name", "Novo Bot"),
            "token":  data.get("token", ""),
            "active": False,
        }
        cfg["bots"].append(new_bot)
        _save_config(cfg)
        return jsonify(new_bot), 201

    @app.route("/api/bots/<bot_id>", methods=["PUT"])
    @login_required
    def update_bot(bot_id):
        data = request.get_json(force=True)
        cfg  = _load_config()
        running_ids = set(_manager.running_bot_ids) if _manager else set()
        for i, b in enumerate(cfg.get("bots", [])):
            if b["id"] == bot_id:
                new_token = data.get("token", b["token"]).strip()
                cfg["bots"][i]["name"]  = data.get("name", b["name"])
                cfg["bots"][i]["token"] = new_token
                _save_config(cfg)
                if bot_id in running_ids and _start_bot_callback:
                    _start_bot_callback(bot_id, new_token)
                return jsonify(cfg["bots"][i])
        return jsonify({"error": "Bot não encontrado"}), 404

    @app.route("/api/bots/<bot_id>/activate", methods=["POST"])
    @login_required
    def activate_bot(bot_id):
        cfg = _load_config()
        bot_to_start = None
        for b in cfg.get("bots", []):
            if b["id"] == bot_id:
                b["active"] = True
                bot_to_start = b
        if not bot_to_start:
            return jsonify({"error": "Bot não encontrado"}), 404
        _save_config(cfg)
        if _start_bot_callback:
            _start_bot_callback(bot_id, bot_to_start["token"])
        return jsonify({"success": True})

    @app.route("/api/bots/<bot_id>/deactivate", methods=["POST"])
    @login_required
    def deactivate_bot(bot_id):
        cfg = _load_config()
        found = False
        for b in cfg.get("bots", []):
            if b["id"] == bot_id:
                b["active"] = False
                found = True
        if not found:
            return jsonify({"error": "Bot não encontrado"}), 404
        _save_config(cfg)
        if _stop_bot_callback:
            _stop_bot_callback(bot_id)
        return jsonify({"success": True})

    @app.route("/api/bots/<bot_id>", methods=["DELETE"])
    @login_required
    def delete_bot(bot_id):
        cfg    = _load_config()
        before = len(cfg.get("bots", []))
        cfg["bots"] = [b for b in cfg.get("bots", []) if b["id"] != bot_id]
        if len(cfg["bots"]) == before:
            return jsonify({"error": "Bot não encontrado"}), 404
        _save_config(cfg)
        return jsonify({"success": True})

    # ── Status ────────────────────────────────────────────────────────────────

    @app.route("/api/status", methods=["GET"])
    @login_required
    def get_status():
        running_ids = list(_manager.running_bot_ids) if _manager else []
        if not running_ids:
            return jsonify({"online": False, "bots": []})

        bots_info = []
        for bot_id in running_ids:
            bot = _manager.get_bot(bot_id) if _manager else None
            if bot and _loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(bot.get_me(), _loop)
                    info = future.result(timeout=5)
                    bots_info.append({
                        "bot_id": bot_id, "online": True,
                        "name": info.full_name, "username": info.username,
                        "id": info.id,
                    })
                except Exception:
                    bots_info.append({"bot_id": bot_id, "online": False})

        return jsonify({"online": bool(bots_info), "bots": bots_info})

    # ── Config ────────────────────────────────────────────────────────────────

    @app.route("/api/config", methods=["GET"])
    @login_required
    def get_config():
        cfg = _load_config()
        return jsonify({k: v for k, v in cfg.items() if k != "bot_token"})

    @app.route("/api/config", methods=["PUT"])
    @login_required
    def update_config():
        data = request.get_json(force=True)
        cfg  = _load_config()
        for key in ("groups", "button_configs", "timezone", "web_port"):
            if key in data:
                cfg[key] = data[key]
        _save_config(cfg)
        if _reload_callback:
            _reload_callback()
        return jsonify({"success": True})

    # ── Flow order (ordem do fluxograma por grupo) ──────────────────────────────

    @app.route("/api/flow-order", methods=["GET"])
    @login_required
    def get_flow_order():
        return jsonify(_db.load_flow_order())

    @app.route("/api/flow-order", methods=["PUT"])
    @login_required
    def set_flow_order():
        data = request.get_json(force=True) or {}
        # Espera {group_key: [message_id, ...]}
        clean = {k: list(v) for k, v in data.items() if isinstance(v, list)}
        _db.save_flow_order(clean)
        return jsonify({"success": True})

    @app.route("/api/flow-layout", methods=["GET"])
    @login_required
    def get_flow_layout():
        return jsonify(_db.load_flow_layout())

    @app.route("/api/flow-layout", methods=["PUT"])
    @login_required
    def set_flow_layout():
        data = request.get_json(force=True) or {}
        # Espera {group_key: {message_id: {x, y}}}
        clean = {}
        for gk, nodes in data.items():
            if not isinstance(nodes, dict):
                continue
            clean[gk] = {}
            for mid, pos in nodes.items():
                if isinstance(pos, dict) and "x" in pos and "y" in pos:
                    try:
                        clean[gk][mid] = {"x": float(pos["x"]), "y": float(pos["y"])}
                    except (TypeError, ValueError):
                        pass
        _db.save_flow_layout(clean)
        return jsonify({"success": True})

    # ── Messages ──────────────────────────────────────────────────────────────

    @app.route("/api/messages", methods=["GET"])
    @login_required
    def get_messages():
        return jsonify(_load_messages())

    @app.route("/api/messages", methods=["POST"])
    @login_required
    def create_message():
        data = request.get_json(force=True)
        message = {
            "id":         uuid.uuid4().hex[:8],
            "name":       data.get("name", "Nova Mensagem"),
            "text":       data.get("text", ""),
            "image":      data.get("image") or None,
            "video_note": data.get("video_note") or None,
            "active":     data.get("active", True),
            "parse_mode": data.get("parse_mode", "HTML"),
            "schedules":  _ensure_schedule_ids(data.get("schedules", [])),
            # marcador de candle + condicionais (eram perdidos ao criar via POST)
            "candle_hour":         data.get("candle_hour") or None,
            "candle_symbol":       data.get("candle_symbol") or None,
            "conditional_enabled": bool(data.get("conditional_enabled", False)),
            "conditional_win":     data.get("conditional_win") or {},
            "conditional_loss":    data.get("conditional_loss") or {},
        }
        _normalize_media(message)
        _db.upsert_message(message)
        if _reload_callback:
            _reload_callback()
        return jsonify(message), 201

    @app.route("/api/messages/<message_id>", methods=["PUT"])
    @login_required
    def update_message(message_id):
        data = request.get_json(force=True)
        data["id"] = message_id
        data.setdefault("parse_mode", "HTML")
        data["video_note"] = data.get("video_note") or None
        data["schedules"] = _ensure_schedule_ids(data.get("schedules", []))
        _normalize_media(data)
        _db.upsert_message(data)
        if _reload_callback:
            _reload_callback()
        return jsonify(data)

    # ── Upload de imagem ──────────────────────────────────────────────────────
    _ALLOWED_IMG   = {"jpg", "jpeg", "png", "gif", "webp"}
    _ALLOWED_VIDEO = {"mp4", "mov", "webm", "avi", "mkv"}
    _ALLOWED_UPLOAD = _ALLOWED_IMG | _ALLOWED_VIDEO

    @app.route("/api/upload", methods=["POST"])
    @login_required
    def upload_image():
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "Nenhum arquivo"}), 400
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in _ALLOWED_UPLOAD:
            return jsonify({"error": "Tipo não permitido"}), 400
        filename = f"{uuid.uuid4().hex[:16]}.{ext}"
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = _UPLOAD_DIR / filename
        f.save(str(dest))
        # path: caminho absoluto que o bot abre diretamente do disco (volume /data)
        # url:  rota servida pela web para preview no painel
        return jsonify({"path": str(dest.resolve()).replace("\\", "/"),
                        "url": f"/media/{filename}"})

    @app.route("/media/<path:filename>")
    @login_required
    def serve_upload(filename):
        from flask import send_from_directory
        return send_from_directory(_UPLOAD_DIR.resolve(), filename)

    @app.route("/api/pares", methods=["GET"])
    @login_required
    def list_pares():
        """Lista os pares de ativos disponíveis na API de candles.
        Faz proxy para evitar CORS; usa lista padrão como fallback.
        """
        try:
            url = "https://web-production-cdff3.up.railway.app/pares"
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read())
            pares = data.get("pares", data if isinstance(data, list) else [])
            if pares:
                return jsonify({"pares": pares})
        except Exception as exc:
            logger.warning("Falha ao buscar pares da API: %s", exc)
        return jsonify({"pares": _DEFAULT_PARES})

    # ── IA (DeepSeek) ───────────────────────────────────────────────────────────

    def _deepseek_key():
        return os.getenv("DEEPSEEK_API_KEY") or _db.get_setting("deepseek_api_key", "") or ""

    @app.route("/api/ai/config", methods=["GET"])
    @login_required
    def ai_config():
        return jsonify({
            "has_key": bool(_deepseek_key()),
            "from_env": bool(os.getenv("DEEPSEEK_API_KEY")),
        })

    @app.route("/api/ai/config", methods=["POST"])
    @login_required
    def ai_set_config():
        data = request.get_json(force=True) or {}
        key = (data.get("api_key") or "").strip()
        _db.set_setting("deepseek_api_key", key)
        return jsonify({"success": True, "has_key": bool(_deepseek_key())})

    @app.route("/api/ai/generate", methods=["POST"])
    @login_required
    def ai_generate():
        key = _deepseek_key()
        if not key:
            return jsonify({"error": "Configure a chave da API DeepSeek na aba IA."}), 400

        body = request.get_json(force=True) or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "Descreva o que deseja gerar."}), 400

        cfg = _load_config()
        groups = cfg.get("groups", {})
        buttons = list(cfg.get("button_configs", {}).keys())
        pares = [p["symbol"] for p in _DEFAULT_PARES]
        group_lines = "\n".join(f'- "{k}" = {g.get("name", k)}' for k, g in groups.items()) or "(nenhum)"
        first_group = next(iter(groups.keys()), "")

        # Contexto: mensagens existentes deste cliente (para a IA copiar/replicar modelos)
        existing = _load_messages().get("messages", [])
        ctx_lines = []
        for m in existing[:40]:
            gs = ",".join(sorted({s.get("group", "") for s in (m.get("schedules") or [])}))
            snippet = (m.get("text", "") or "").replace("\n", " ")[:160]
            flags = []
            if m.get("candle_symbol"): flags.append(f"ativo={m['candle_symbol']}")
            if m.get("conditional_enabled"): flags.append("condicional")
            ctx_lines.append(
                f'- nome="{m.get("name","")}" grupos=[{gs}] {" ".join(flags)} texto="{snippet}"'
            )
        existing_block = "\n".join(ctx_lines) or "(nenhuma mensagem ainda)"

        system = (
            "Você cria e organiza fluxos de mensagens agendadas para um bot de Telegram de sinais de trading.\n"
            "Responda SOMENTE com um objeto JSON válido, sem comentários, no formato:\n"
            '{"groups":[{"key":"slug_sem_espacos","name":"Nome do Grupo","color":"#7c3aed"}],'
            '"messages":[{"name":"texto curto","text":"conteudo (pode usar <b> <i>)",'
            '"parse_mode":"HTML","active":true,"candle_symbol":"BTCUSD-OTC ou null",'
            '"conditional_enabled":true,'
            '"conditional_win":{"text":"mensagem de vitoria","parse_mode":"HTML","show_buttons":true},'
            '"conditional_loss":{"text":"mensagem de derrota","parse_mode":"HTML","show_buttons":true},'
            '"schedules":[{"group":"<chave_do_grupo>","time":"HH:MM",'
            '"days":["mon","tue","wed","thu","fri"],"buttons":[]}]}]}\n\n'
            f"Grupos EXISTENTES (use a CHAVE no campo group):\n{group_lines}\n\n"
            f"Botões disponíveis (chaves): {buttons}\n"
            f"Ativos disponíveis para candle_symbol (use EXATAMENTE uma destas strings):\n{pares}\n\n"
            f"Mensagens/modelos JÁ EXISTENTES deste cliente (pode copiar/replicar fielmente quando pedido):\n{existing_block}\n\n"
            "Regras gerais:\n"
            "- Você PODE criar novos grupos em \"groups\" para organizar (key em minúsculas, sem espaços/acentos; "
            "  inclua uma cor hex). Só crie grupos novos se o usuário pedir para organizar/separar.\n"
            "- Para copiar um modelo existente, reproduza fielmente name/text/candle_symbol/condicional, "
            "  mudando apenas o grupo/horário conforme pedido.\n"
            "- Use chaves de grupo existentes quando se referir a grupos já criados; para grupos novos, "
            "  use a key que você definiu em \"groups\".\n"
            "- days aceita: mon,tue,wed,thu,fri,sat,sun ; time em 24h HH:MM.\n"
            "- Não invente chaves de botão fora da lista.\n"
            f"- Se o usuário não citar grupo nem pedir grupo novo, use \"{first_group}\".\n\n"
            "DEFINIÇÃO AUTOMÁTICA DE ATIVO E CONDICIONAL (muito importante):\n"
            "- Deduza o ATIVO pelo contexto da mensagem e escolha o candle_symbol correspondente da lista. "
            "Ex.: fala de Bitcoin/BTC -> BTCUSD-OTC; Ethereum/ETH -> ETHUSD-OTC; Euro/EUR/USD -> EURUSD-OTC; "
            "Ouro/Gold -> XAUUSD-OTC; Apple -> APPLE-OTC; etc. Se não der para deduzir, use BTCUSD-OTC.\n"
            "- Toda mensagem que for um SINAL de entrada/operação DEVE ter candle_symbol definido, "
            "conditional_enabled=true e conteúdo em conditional_win e conditional_loss.\n"
            "  * conditional_win.text: mensagem curta comemorando o acerto (ex.: '✅ <b>WIN!</b> Mais um green confirmado 💰'). "
            "  * conditional_loss.text: mensagem curta de gestão/encorajamento (ex.: '🔴 <b>LOSS</b>. Faz parte, gestão de banca e bora pra próxima 💪'). "
            "  * Use HTML em ambos e mantenha coerência com o tom do sinal.\n"
            "- Mensagens informativas (bom dia, avisos, promoções) NÃO são sinais: candle_symbol=null, "
            "conditional_enabled=false e sem conditional_win/loss.\n"
            "- Crie um fluxo SEMANAL coerente. Retorne apenas o JSON."
        )

        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "stream": False,
        }

        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            return jsonify({"error": f"DeepSeek: {detail}"}), 502
        except Exception as exc:
            return jsonify({"error": f"Falha ao chamar DeepSeek: {exc}"}), 502

        content = (((result.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        try:
            parsed = json.loads(content)
        except Exception:
            return jsonify({"error": "A IA não retornou JSON válido.", "raw": content}), 502

        msgs = parsed.get("messages") if isinstance(parsed, dict) else None
        if not isinstance(msgs, list):
            return jsonify({"error": "JSON sem lista 'messages'.", "raw": content}), 502

        # Grupos novos propostos pela IA
        import re as _re
        new_groups = []
        for g in (parsed.get("groups") or []):
            if not isinstance(g, dict):
                continue
            key = (g.get("key") or "").strip().lower()
            key = _re.sub(r"[^a-z0-9_]+", "_", key).strip("_")
            if not key or key in groups:
                continue
            color = g.get("color") if _re.match(r"^#[0-9a-fA-F]{6}$", str(g.get("color", ""))) else "#7c3aed"
            ng = {"key": key, "name": g.get("name") or key, "color": color}
            new_groups.append(ng)

        valid = set(groups.keys()) | {g["key"] for g in new_groups}
        fallback = first_group or (new_groups[0]["key"] if new_groups else "")
        valid_pares = set(pares)
        for m in msgs:
            for s in (m.get("schedules") or []):
                if s.get("group") not in valid:
                    s["group"] = fallback
            # valida o ativo escolhido pela IA
            sym = m.get("candle_symbol")
            if sym and sym not in valid_pares:
                # tenta casar por prefixo (ex.: "BTC" -> "BTCUSD-OTC")
                up = str(sym).upper().replace("/", "").replace("-OTC", "")
                match = next((p for p in pares if p.upper().startswith(up[:3])), None)
                m["candle_symbol"] = match or "BTCUSD-OTC"
            # garante que condicionais sejam dicts
            if m.get("conditional_enabled"):
                if not isinstance(m.get("conditional_win"), dict):
                    m["conditional_win"] = {}
                if not isinstance(m.get("conditional_loss"), dict):
                    m["conditional_loss"] = {}

        return jsonify({"messages": msgs, "groups": new_groups})

    @app.route("/api/messages/<message_id>/test-conditional", methods=["POST"])
    @login_required
    def test_conditional(message_id):
        """Dispara imediatamente a mensagem condicional WIN ou LOSS sem esperar o candle.
        Body: {"result": "win"|"loss", "group": "group_key"}
        """
        body   = request.get_json(force=True) or {}
        result = body.get("result", "win")
        group_key = body.get("group", "")

        if result not in ("win", "loss"):
            return jsonify({"error": "result deve ser 'win' ou 'loss'"}), 400

        bot = _manager.get_bot() if _manager else None
        if not bot or not _loop:
            return jsonify({"error": "Nenhum bot online"}), 503

        cfg = _load_config()
        if not group_key:
            # usa o primeiro grupo disponível
            group_key = next(iter(cfg.get("groups", {})), "")
        if not group_key:
            return jsonify({"error": "Nenhum grupo configurado"}), 400

        from bot import send_conditional_now
        future = asyncio.run_coroutine_threadsafe(
            send_conditional_now(bot, message_id, result, group_key, cfg),
            _loop,
        )
        try:
            outcome = future.result(timeout=30)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        status_code = 200 if outcome.get("success") else 400
        return jsonify(outcome), status_code

    @app.route("/api/messages/<message_id>/candle-result", methods=["PATCH"])
    @login_required
    def set_candle_result(message_id):
        """Define manualmente o resultado WIN/LOSS de um template.
        Body: {"result": "win" | "loss" | null}
        """
        body   = request.get_json(force=True) or {}
        result = body.get("result")
        if result not in ("win", "loss", None):
            return jsonify({"error": "result deve ser 'win', 'loss' ou null"}), 400
        _db.update_candle_result(message_id, result)
        return jsonify({"success": True, "result": result})

    @app.route("/api/messages/<message_id>", methods=["DELETE"])
    @login_required
    def delete_message_route(message_id):
        _db.delete_message(message_id)
        if _reload_callback:
            _reload_callback()
        return jsonify({"success": True})

    # ── Send Now ──────────────────────────────────────────────────────────────

    @app.route("/api/messages/<message_id>/send-now", methods=["POST"])
    @login_required
    def send_now(message_id):
        bot = _manager.get_bot() if _manager else None
        if not bot or not _loop:
            return jsonify({"error": "Nenhum bot online — ative pelo menos um bot"}), 503

        data  = request.get_json(force=True) or {}
        cfg   = _load_config()
        store = _load_messages()

        message = next((m for m in store["messages"] if m["id"] == message_id), None)
        if not message:
            return jsonify({"error": "Mensagem não encontrada"}), 404

        group_key      = data.get("group")
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
                    message.get("parse_mode", "HTML"),
                    message.get("video_note"),
                ),
                _loop,
            )
            try:
                future.result(timeout=30)
                results.append({"group": gk, "success": True})
            except Exception as exc:
                results.append({"group": gk, "success": False, "error": str(exc)})

        return jsonify({"results": results})

    # ── Emoji Map ─────────────────────────────────────────────────────────────

    @app.route("/api/emoji", methods=["GET"])
    @login_required
    def get_emoji_map():
        data = _db.load_emoji_map()
        return jsonify([{"char": k, "id": v} for k, v in sorted(data.items())])

    @app.route("/api/emoji", methods=["POST"])
    @login_required
    def add_emoji():
        body     = request.get_json(force=True) or {}
        char     = body.get("char", "").strip()
        emoji_id = body.get("id", "").strip()
        if not char or not emoji_id:
            return jsonify({"error": "char e id são obrigatórios"}), 400
        _db.save_emoji(char, emoji_id)
        return jsonify({"char": char, "id": emoji_id}), 201

    @app.route("/api/emoji", methods=["DELETE"])
    @login_required
    def del_emoji():
        body = request.get_json(force=True) or {}
        char = body.get("char", "")
        if not char:
            return jsonify({"error": "char obrigatório"}), 400
        _db.delete_emoji(char)
        return jsonify({"success": True})

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Landing page pública (vendas) ───────────────────────────────────────────
    @app.route("/lp")
    def landing_page():
        from flask import send_from_directory
        landing_dir = Path(__file__).resolve().parent / "landing"
        return send_from_directory(landing_dir, "index.html")

    return app
