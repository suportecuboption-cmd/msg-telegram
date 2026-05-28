import json
import logging
import os
from pathlib import Path
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_DATA = Path(os.getenv("DATA_DIR", "."))
MESSAGES_FILE = _DATA / "messages.json"  # usado apenas no modo JSON local
CONFIG_FILE = Path("config.json")

DAYS_MAP = {
    "seg": "mon", "ter": "tue", "qua": "wed",
    "qui": "thu", "sex": "fri", "sab": "sat", "dom": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed",
    "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
}


def load_messages() -> dict:
    import db
    return db.load_messages()


def load_config() -> dict:
    import db
    return db.load_config()


def save_messages(data: dict) -> None:
    import db
    db.save_messages(data)


def build_keyboard(button_keys: list, config: dict) -> Optional[InlineKeyboardMarkup]:
    button_configs = config.get("button_configs", {})
    buttons = [
        InlineKeyboardButton(text=button_configs[k]["label"], url=button_configs[k]["url"])
        for k in button_keys
        if k in button_configs and button_configs[k].get("url")
    ]
    if not buttons:
        return None
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def send_scheduled_message(
    bot: Bot,
    text: str,
    chat_id: str,
    button_keys: list,
    config: dict,
    image: Optional[str],
    message_name: str,
    parse_mode: Optional[str] = "HTML",
) -> None:
    """Envia uma mensagem agendada para o chat_id.

    parse_mode pode ser "HTML", "MarkdownV2" ou None/"none" (sem formatação).
    Para usar emojis animados do Telegram em modo HTML, utilize a tag:
        <tg-emoji emoji-id="ID_DO_EMOJI">🔥</tg-emoji>
    """
    try:
        keyboard = build_keyboard(button_keys, config)
        # Normaliza: "none" ou vazio → None (sem parse_mode)
        pm = parse_mode if parse_mode and parse_mode.lower() not in ("none", "") else None
        if image:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=text,
                reply_markup=keyboard,
                parse_mode=pm,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=pm,
            )
        logger.info("Mensagem '%s' enviada para %s", message_name, chat_id)
    except Exception as exc:
        logger.error("Erro ao enviar '%s' para %s: %s", message_name, chat_id, exc)
        raise


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot, config: dict) -> None:
    scheduler.remove_all_jobs()
    timezone = config.get("timezone", "America/Sao_Paulo")
    job_count = 0

    for message in load_messages().get("messages", []):
        if not message.get("active", True):
            continue

        for schedule in message.get("schedules", []):
            if not schedule.get("active", True):
                continue

            group_key = schedule.get("group", "")
            group = config.get("groups", {}).get(group_key)
            if not group or not group.get("id"):
                logger.warning("Grupo '%s' não encontrado ou sem ID configurado", group_key)
                continue

            button_keys = schedule.get("buttons", group.get("default_buttons", []))
            days = schedule.get("days", ["mon", "tue", "wed", "thu", "fri"])
            day_of_week = ",".join(DAYS_MAP.get(d, d) for d in days)

            hour, minute = map(int, schedule["time"].split(":"))
            job_id = f"{message['id']}_{schedule['id']}"

            scheduler.add_job(
                send_scheduled_message,
                trigger=CronTrigger(
                    day_of_week=day_of_week,
                    hour=hour,
                    minute=minute,
                    timezone=timezone,
                ),
                args=[bot, message["text"], group["id"], button_keys, config,
                      message.get("image"), message.get("name", ""),
                      message.get("parse_mode", "HTML")],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            job_count += 1

    logger.info("%d agendamento(s) configurado(s)", job_count)


async def reload_scheduler(scheduler: AsyncIOScheduler, bot: Bot, config: dict) -> None:
    setup_scheduler(scheduler, bot, config)
    logger.info("Scheduler recarregado com sucesso")
