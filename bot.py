import json
import logging
import os
import re
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


# ── Pré-processamento de emojis animados ──────────────────────────────────────

def preprocess_animated_emoji(text: str, emoji_map: dict) -> str:
    """Substitui chars de emoji conhecidos pelo wrapper <tg-emoji emoji-id="...">emoji</tg-emoji>.

    - Só opera em partes de texto puro (não dentro de tags HTML existentes).
    - Emojis já envolvidos por <tg-emoji> não são re-processados.
    - Emojis mais longos (multi-codepoint) têm prioridade na substituição.
    """
    if not emoji_map or not text:
        return text

    # Divide o texto em pedaços: tags HTML e conteúdo entre elas
    parts = re.split(r'(<[^>]+>)', text)
    result = []
    in_tg_emoji = 0  # profundidade dentro de <tg-emoji>...</tg-emoji>

    for part in parts:
        if part.startswith('<'):
            result.append(part)
            if re.match(r'<tg-emoji', part, re.IGNORECASE):
                in_tg_emoji += 1
            elif re.match(r'</tg-emoji', part, re.IGNORECASE):
                in_tg_emoji = max(0, in_tg_emoji - 1)
        else:
            if in_tg_emoji > 0:
                # Dentro de um bloco <tg-emoji> existente — não modificar
                result.append(part)
            else:
                # Texto livre: substituir emojis conhecidos (maior comprimento primeiro)
                for emoji_char in sorted(emoji_map, key=len, reverse=True):
                    if emoji_char in part:
                        emoji_id = emoji_map[emoji_char]
                        part = part.replace(
                            emoji_char,
                            f'<tg-emoji emoji-id="{emoji_id}">{emoji_char}</tg-emoji>',
                        )
                result.append(part)

    return "".join(result)


# ── Handler de registro automático de emojis animados ────────────────────────

async def cmd_start(update, context) -> None:
    await update.message.reply_text(
        "🤖 <b>Bot Manager ativo!</b>\n\n"
        "Para registrar emojis animados, envie uma mensagem aqui contendo "
        "os emojis animados que deseja usar nas mensagens agendadas.\n\n"
        "O sistema extrai o ID automaticamente via API do Telegram e salva o mapeamento. "
        "Depois é só usar o emoji normalmente no dashboard — ele será animado no envio! 🔥💎🚀",
        parse_mode="HTML",
    )


async def handle_emoji_registration(update, context) -> None:
    """Detecta custom_emoji entities em mensagens privadas e persiste o mapeamento."""
    import db as _db

    msg = update.message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    custom_entities = [e for e in entities if e.type == "custom_emoji"]

    if not custom_entities:
        # Não responde a mensagens sem emoji animado para não ser intrusivo
        return

    registered = []
    for entity in custom_entities:
        emoji_char = text[entity.offset: entity.offset + entity.length]
        emoji_id = entity.custom_emoji_id
        if emoji_char and emoji_id:
            _db.save_emoji(emoji_char, emoji_id)
            if emoji_char not in registered:
                registered.append(emoji_char)

    if registered:
        await msg.reply_text(
            f"✅ <b>{len(registered)} emoji(s) animado(s) registrado(s):</b> "
            + " ".join(registered)
            + "\n\nUse-os normalmente no dashboard — o sistema aplica o ID automaticamente!",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text("⚠️ Não foi possível extrair os IDs dos emojis.")


def setup_handlers(app) -> None:
    """Registra handlers de comandos e mensagens no Application do Telegram."""
    from telegram.ext import MessageHandler, CommandHandler
    from telegram.ext import filters as tg_filters

    app.add_handler(CommandHandler("start", cmd_start))
    # Captura qualquer mensagem privada (texto ou legenda de foto) que não seja comando
    app.add_handler(MessageHandler(
        tg_filters.ChatType.PRIVATE & ~tg_filters.COMMAND,
        handle_emoji_registration,
    ))
    logger.info("Handlers de registro de emojis configurados")


# ── Envio de mensagem agendada ────────────────────────────────────────────────

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
    """Envia uma mensagem agendada.

    Em modo HTML, emojis animados registrados são automaticamente envolvidos
    com <tg-emoji emoji-id="..."> antes do envio.
    """
    try:
        # Normaliza parse_mode: "none"/vazio → None
        pm = parse_mode if parse_mode and parse_mode.lower() not in ("none", "") else None

        # Pré-processa emojis animados (apenas no modo HTML)
        if pm and pm.upper() == "HTML":
            import db as _db
            emoji_map = _db.load_emoji_map()
            if emoji_map:
                text = preprocess_animated_emoji(text, emoji_map)

        keyboard = build_keyboard(button_keys, config)

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
