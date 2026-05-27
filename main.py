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
import sys
import threading
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


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

            if not token or token == "SEU_TOKEN_AQUI":
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
            scheduler.start()

            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

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

    port = config.get("web_port", 5000)
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
