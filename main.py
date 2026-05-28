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
        db_module.init_db()        # ← cria as tabelas PRIMEIRO
        db_module.migrate_from_json()

    db_module.create_default_admin()  # ← só depois que as tabelas existem


def load_config() -> dict:
    import db as db_module
    return db_module.load_config()


class BotManager:
    """Gerencia o ciclo de vida do Application do Telegram.
    Permite parar e reiniciar o bot sem encerrar o processo."""

    def __init__(self) -> None:
        self._app = None
        self._scheduler = None
        self._lock = asyncio.Lock()

    @property
    def bot(self):
        return self._app.bot if self._app else None

    @property
    def running(self) -> bool:
        return self._app is not None

    async def start(self, token: str, config: dict) -> None:
        async with self._lock:
            await self._stop_internal()

            _placeholders = {"SEU_TOKEN_AQUI", "SEU_TOKEN_DO_BOT_AQUI", "YOUR_TOKEN_HERE"}
            if not token or token in _placeholders or ":" not in token:
                logger.warning("Token não configurado — bot permanece offline.")
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
                logger.error("Falha ao iniciar bot (token inválido?): %s", exc)
                scheduler.shutdown(wait=False)
                return

            self._app = app
            self._scheduler = scheduler
            logger.info("Bot iniciado — token: ...%s", token[-8:])

    async def restart(self, token: str, config: dict) -> None:
        logger.info("Reiniciando bot com novo token...")
        await self.start(token, config)

    async def reload_scheduler(self, config: dict) -> None:
        import bot as bot_module
        if self._app and self._scheduler:
            bot_module.setup_scheduler(self._scheduler, self._app.bot, config)
            logger.info("Scheduler recarregado")

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_internal()

    async def _stop_internal(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:
                logger.error("Erro ao parar bot: %s", exc)
            self._app = None


async def run() -> None:
    _init_data_dir()
    config = load_config()
    loop = asyncio.get_running_loop()
    manager = BotManager()

    # ── Callbacks para o servidor web ──────────────────────────────────────

    def reload_cb() -> None:
        fresh = load_config()
        asyncio.run_coroutine_threadsafe(manager.reload_scheduler(fresh), loop)

    def restart_cb(token: str) -> None:
        fresh = load_config()
        fresh["bot_token"] = token
        asyncio.run_coroutine_threadsafe(manager.restart(token, fresh), loop)

    # ── Iniciar servidor web ───────────────────────────────────────────────

    import web as web_module

    flask_app = web_module.create_app()
    web_module.set_context(manager, loop, reload_cb, restart_cb)

    port = int(os.getenv("PORT", config.get("web_port", 5000)))
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=port, debug=False, use_reloader=False
        ),
        daemon=True,
    )
    flask_thread.start()
    logger.info("Dashboard disponível em: http://localhost:%d", port)

    # ── Iniciar bot ────────────────────────────────────────────────────────

    await manager.start(config.get("bot_token", ""), config)

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
