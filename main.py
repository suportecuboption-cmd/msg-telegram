"""
Telegram Bot Manager — Ponto de entrada principal.

Uso:
    python main.py

Pré-requisitos:
    1. pip install -r requirements.txt
    2. Edite config.json com seu token e IDs dos grupos
    3. Acesse http://localhost:5000 para o dashboard
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

_DATA = Path(os.getenv("DATA_DIR", "."))
_CONFIG_FILE = _DATA / "config.json"
_MESSAGES_FILE = _DATA / "messages.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def _init_data_dir() -> None:
    """Garante que DATA_DIR existe com os arquivos padrão."""
    _DATA.mkdir(parents=True, exist_ok=True)

    import db as db_module
    if not db_module.use_postgres():
        if not _CONFIG_FILE.exists():
            src = Path("config.example.json")
            if src.exists():
                shutil.copy(src, _CONFIG_FILE)
                logger.info("config.json criado em %s", _DATA)

        if not _MESSAGES_FILE.exists():
            src = Path("messages.default.json")
            if src.exists():
                shutil.copy(src, _MESSAGES_FILE)
            else:
                _MESSAGES_FILE.write_text('{"messages": []}', encoding="utf-8")
            logger.info("messages.json inicializado em %s", _DATA)
    else:
        db_module.init_db()
        db_module.migrate_from_json()

    db_module.create_default_admin()


def load_config() -> dict:
    import db as db_module
    return db_module.load_config()


_PLACEHOLDER_TOKENS = {"SEU_TOKEN_AQUI", "SEU_TOKEN_DO_BOT_AQUI", "YOUR_TOKEN_HERE"}


def _is_valid_token(token: str) -> bool:
    return bool(token) and token not in _PLACEHOLDER_TOKENS and ":" in token


class BotManager:
    """Gerencia múltiplas instâncias de bot do Telegram simultaneamente."""

    def __init__(self) -> None:
        self._apps: dict = {}       # bot_id -> Application
        self._schedulers: dict = {} # bot_id -> AsyncIOScheduler
        self._lock = asyncio.Lock()

    def get_bot(self, bot_id: str = None):
        """Retorna o bot do bot_id especificado ou o primeiro disponível."""
        if bot_id and bot_id in self._apps:
            return self._apps[bot_id].bot
        for app in self._apps.values():
            return app.bot
        return None

    @property
    def bot(self):
        """Compatibilidade: retorna o primeiro bot em execução."""
        return self.get_bot()

    @property
    def running_bot_ids(self) -> list:
        return list(self._apps.keys())

    async def start_bot(self, bot_id: str, token: str, config: dict) -> None:
        async with self._lock:
            await self._stop_bot_internal(bot_id)

            if not _is_valid_token(token):
                logger.warning("Token inválido para bot %s — permanece offline.", bot_id)
                return

            from telegram.ext import Application
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            import bot as bot_module

            app = Application.builder().token(token).build()
            scheduler = AsyncIOScheduler(
                timezone=config.get("timezone", "America/Sao_Paulo")
            )
            bot_module.setup_scheduler(scheduler, app.bot, config)
            bot_module.setup_handlers(app)
            scheduler.start()

            try:
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
            except Exception as exc:
                logger.error("Falha ao iniciar bot %s (token inválido?): %s", bot_id, exc)
                scheduler.shutdown(wait=False)
                return

            self._apps[bot_id] = app
            self._schedulers[bot_id] = scheduler
            logger.info("Bot %s iniciado — token: ...%s", bot_id, token[-8:])

    async def stop_bot(self, bot_id: str) -> None:
        async with self._lock:
            await self._stop_bot_internal(bot_id)

    async def reload_scheduler(self, config: dict) -> None:
        import bot as bot_module
        for bot_id, scheduler in self._schedulers.items():
            app = self._apps.get(bot_id)
            if app and scheduler:
                bot_module.setup_scheduler(scheduler, app.bot, config)
        if self._apps:
            logger.info("Scheduler recarregado para %d bot(s)", len(self._apps))

    async def shutdown(self) -> None:
        for bot_id in list(self._apps.keys()):
            await self._stop_bot_internal(bot_id)

    async def _stop_bot_internal(self, bot_id: str) -> None:
        scheduler = self._schedulers.pop(bot_id, None)
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
        app = self._apps.pop(bot_id, None)
        if app:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception as exc:
                logger.error("Erro ao parar bot %s: %s", bot_id, exc)


async def run() -> None:
    _init_data_dir()
    config = load_config()
    loop = asyncio.get_running_loop()
    manager = BotManager()

    # ── Callbacks para o servidor web ──────────────────────────────────────

    def reload_cb() -> None:
        fresh = load_config()
        asyncio.run_coroutine_threadsafe(manager.reload_scheduler(fresh), loop)

    def start_bot_cb(bot_id: str, token: str) -> None:
        fresh = load_config()
        asyncio.run_coroutine_threadsafe(manager.start_bot(bot_id, token, fresh), loop)

    def stop_bot_cb(bot_id: str) -> None:
        asyncio.run_coroutine_threadsafe(manager.stop_bot(bot_id), loop)

    # ── Iniciar servidor web ───────────────────────────────────────────────

    import web as web_module

    flask_app = web_module.create_app()
    web_module.set_context(manager, loop, reload_cb, start_bot_cb, stop_bot_cb)

    port = int(os.getenv("PORT", config.get("web_port", 5000)))
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=port, debug=False, use_reloader=False
        ),
        daemon=True,
    )
    flask_thread.start()
    logger.info("Dashboard disponível em: http://localhost:%d", port)

    # ── Iniciar todos os bots ativos ───────────────────────────────────────

    for bot_cfg in config.get("bots", []):
        if bot_cfg.get("active") and _is_valid_token(bot_cfg.get("token", "")):
            await manager.start_bot(bot_cfg["id"], bot_cfg["token"], config)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Encerrado pelo usuário.")
    except Exception as exc:
        logger.exception("Erro fatal: %s", exc)
        sys.exit(1)
