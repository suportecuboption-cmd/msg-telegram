import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.request
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

    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    custom_entities = [e for e in entities if e.type == "custom_emoji"]

    if not custom_entities:
        return

    registered = []
    for entity in custom_entities:
        # parse_entity() converte offsets UTF-16 (API do Telegram) para índices
        # Python corretamente — evita extração errada quando há emojis multi-byte
        # antes da entidade no mesmo texto.
        try:
            emoji_char = msg.parse_entity(entity)
        except Exception:
            # fallback: indexação direta (só correto para texto puramente ASCII)
            text = msg.text or msg.caption or ""
            emoji_char = text[entity.offset: entity.offset + entity.length]

        emoji_id = entity.custom_emoji_id
        if emoji_char and emoji_id:
            _db.save_emoji(emoji_char, emoji_id)
            key = (emoji_char, emoji_id)
            if key not in registered:
                registered.append(key)

    if registered:
        lines = []
        for emoji_char, emoji_id in registered:
            codepoints = " ".join(f"U+{ord(c):04X}" for c in emoji_char)
            lines.append(
                f"{emoji_char}  <code>{emoji_id[:20]}…</code>  "
                f"<i>({codepoints})</i>"
            )
        await msg.reply_text(
            f"✅ <b>{len(registered)} emoji(s) registrado(s):</b>\n\n"
            + "\n".join(lines)
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


# ── Conversão para vídeo bolinha (formato quadrado) ──────────────────────────

def _prepare_video_note(src: str) -> tuple[str, bool]:
    """Converte o vídeo para MP4 quadrado usando ffmpeg.
    Retorna (caminho, foi_convertido). Se ffmpeg não estiver disponível
    ou falhar, devolve o arquivo original."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src,
                "-vf", "crop=min(iw\\,ih):min(iw\\,ih),scale=480:480",
                "-c:v", "libx264", "-preset", "fast",
                "-c:a", "aac", "-movflags", "+faststart",
                "-t", "60",          # garante máx 1 minuto
                tmp.name,
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0:
            return tmp.name, True
        os.unlink(tmp.name)
    except Exception as exc:
        logger.warning("ffmpeg não disponível ou falhou (%s) — enviando original", exc)
    return src, False


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
    video_note: Optional[str] = None,
    conditional_enabled: bool = False,
    candle_hour: Optional[str] = None,
    conditional_win: Optional[dict] = None,
    conditional_loss: Optional[dict] = None,
) -> None:
    """Envia uma mensagem agendada.

    Em modo HTML, emojis animados registrados são automaticamente envolvidos
    com <tg-emoji emoji-id="..."> antes do envio.

    Se conditional_enabled=True e candle_hour estiver definido, dispara uma task
    assíncrona que aguarda o fechamento da vela M1 (60s + 10s buffer) e envia
    automaticamente a mensagem condicional WIN ou LOSS correspondente.
    """
    try:
        # Detecta vídeo salvo por engano no campo image
        _VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
        if not video_note and image and not image.startswith("http") \
                and image.lower().endswith(_VIDEO_EXTS):
            video_note = image
            image = None

        # Vídeo bolinha — usa file_id em cache ou converte e envia o arquivo
        if video_note:
            # Se já temos o file_id do Telegram, reutiliza diretamente (sem ffmpeg)
            file_id = None
            if video_note.startswith("file_id:"):
                file_id = video_note[len("file_id:"):]

            try:
                if file_id:
                    msg = await bot.send_video_note(
                        chat_id=chat_id, video_note=file_id, length=480
                    )
                else:
                    vid_path, converted = _prepare_video_note(video_note)
                    try:
                        with open(vid_path, "rb") as vf:
                            msg = await bot.send_video_note(
                                chat_id=chat_id, video_note=vf, length=480
                            )
                        # Persiste o file_id para envios futuros (evita reconversão)
                        if hasattr(msg, "video_note") and msg.video_note:
                            import db as _db
                            _db.set_video_note_file_id(video_note, msg.video_note.file_id)
                            logger.info("file_id do vídeo bolinha salvo para reuso")
                    finally:
                        if converted:
                            try: os.unlink(vid_path)
                            except OSError: pass
                logger.info("Vídeo bolinha '%s' enviado para %s", message_name, chat_id)
            except FileNotFoundError:
                logger.warning("Arquivo de vídeo não encontrado: %s", video_note)
            except Exception as exc:
                logger.error("Erro ao enviar vídeo bolinha '%s': %s", video_note, exc)
            return

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
            # Suporta tanto URL quanto arquivo local (DATA_DIR/uploads/...)
            if image.startswith("http://") or image.startswith("https://"):
                photo_data = image
            else:
                try:
                    photo_data = open(image, "rb")
                except OSError:
                    logger.warning("Imagem local não encontrada: %s", image)
                    photo_data = None

            if photo_data:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_data,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=pm,
                )
                if hasattr(photo_data, "close"):
                    photo_data.close()
            else:
                await bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode=pm
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=pm,
            )
        logger.info("Mensagem '%s' enviada para %s", message_name, chat_id)

        # ── Verificação condicional pós-fechamento do candle ──────────────────
        if conditional_enabled and candle_hour:
            asyncio.create_task(
                _check_and_send_conditional(
                    bot=bot,
                    chat_id=chat_id,
                    message_name=message_name,
                    candle_hour=candle_hour,
                    conditional_win=conditional_win or {},
                    conditional_loss=conditional_loss or {},
                    button_keys=button_keys,
                    config=config,
                )
            )

    except Exception as exc:
        logger.error("Erro ao enviar '%s' para %s: %s", message_name, chat_id, exc)
        raise


_CANDLES_API_URL = "https://web-production-cdff3.up.railway.app/candles"
_CANDLE_CLOSE_WAIT = 70   # 60s (duração M1) + 10s buffer


async def _check_and_send_conditional(
    bot: Bot,
    chat_id: str,
    message_name: str,
    candle_hour: str,
    conditional_win: dict,
    conditional_loss: dict,
    button_keys: list,
    config: dict,
    delay: int = _CANDLE_CLOSE_WAIT,
) -> None:
    """Aguarda o fechamento da vela M1 e envia a mensagem condicional WIN ou LOSS.

    Fluxo:
      1. Dorme ``delay`` segundos (padrão 70 = 60s vela + 10s buffer).
      2. Chama a API de candles.
      3. Localiza o candle de ``candle_hour`` (formato HH:MM).
      4. Envia a mensagem condicional correspondente (WIN ou LOSS) para ``chat_id``.
    """
    await asyncio.sleep(delay)

    def _fetch() -> dict:
        with urllib.request.urlopen(_CANDLES_API_URL, timeout=10) as resp:
            return json.loads(resp.read())

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("Condicional '%s': falha na API de candles após %ds: %s",
                       message_name, delay, exc)
        return

    candle_map: dict = {}
    for c in data.get("candles", []):
        hora_hm  = c.get("hora", "")[:5]   # "HH:MM"
        resultado = c.get("resultado")
        if hora_hm and resultado:
            candle_map[hora_hm] = resultado

    result = candle_map.get(candle_hour)
    if not result:
        logger.info("Condicional '%s': candle %s não encontrado na janela da API (delay=%ds)",
                    message_name, candle_hour, delay)
        return

    logger.info("Condicional '%s': candle %s → %s — enviando para %s",
                message_name, candle_hour, result.upper(), chat_id)

    cond_msg = conditional_win if result == "win" else conditional_loss
    if not cond_msg:
        return

    has_content = any(cond_msg.get(k) for k in ("text", "image", "video_note", "sticker"))
    if not has_content:
        logger.info("Condicional '%s' [%s]: nenhum conteúdo configurado", message_name, result.upper())
        return

    text_c      = cond_msg.get("text", "") or ""
    image_c     = cond_msg.get("image") or None
    video_note_c = cond_msg.get("video_note") or None
    sticker_c   = cond_msg.get("sticker") or None
    parse_mode_c = cond_msg.get("parse_mode", "HTML")
    show_buttons = cond_msg.get("show_buttons", True)

    pm = parse_mode_c if parse_mode_c and parse_mode_c.lower() not in ("none", "") else None

    if pm and pm.upper() == "HTML" and text_c:
        import db as _db
        emoji_map = _db.load_emoji_map()
        if emoji_map:
            text_c = preprocess_animated_emoji(text_c, emoji_map)

    keyboard = build_keyboard(button_keys, config) if show_buttons else None

    try:
        if sticker_c:
            # Envia sticker (file_id do Telegram)
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_c)
            # Texto acompanhante opcional
            if text_c:
                await bot.send_message(chat_id=chat_id, text=text_c,
                                       parse_mode=pm, reply_markup=keyboard)
        elif video_note_c:
            # Reutiliza a lógica completa de vídeo bolinha
            await send_scheduled_message(
                bot=bot, text="", chat_id=chat_id,
                button_keys=[], config=config,
                image=None, message_name=f"{message_name} [{result.upper()}]",
                parse_mode="HTML", video_note=video_note_c,
            )
            return
        elif image_c:
            if image_c.startswith("http://") or image_c.startswith("https://"):
                photo_data = image_c
            else:
                try:
                    photo_data = open(image_c, "rb")
                except OSError:
                    logger.warning("Condicional: imagem não encontrada: %s", image_c)
                    photo_data = None
            if photo_data:
                await bot.send_photo(
                    chat_id=chat_id, photo=photo_data,
                    caption=text_c or None, parse_mode=pm, reply_markup=keyboard,
                )
                if hasattr(photo_data, "close"):
                    photo_data.close()
            elif text_c:
                await bot.send_message(chat_id=chat_id, text=text_c,
                                       parse_mode=pm, reply_markup=keyboard)
        elif text_c:
            await bot.send_message(chat_id=chat_id, text=text_c,
                                   parse_mode=pm, reply_markup=keyboard)

        logger.info("Condicional [%s] '%s' enviado para %s", result.upper(), message_name, chat_id)

    except Exception as exc:
        logger.error("Erro ao enviar condicional [%s] '%s' para %s: %s",
                     result.upper(), message_name, chat_id, exc)


async def check_candle_results() -> None:
    """Verifica a API de candles M1 e atualiza o resultado WIN/LOSS dos templates marcados.

    Roda a cada minuto (agendado em setup_scheduler). Só atualiza mensagens que
    possuem candle_hour configurado. Nunca sobrescreve um resultado definido manualmente
    se o candle daquela hora não está mais na janela retornada pela API.
    """
    import db as _db

    def _fetch() -> dict:
        with urllib.request.urlopen(_CANDLES_API_URL, timeout=10) as resp:
            return json.loads(resp.read())

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("Candle API indisponível: %s", exc)
        return

    candles = data.get("candles", [])
    if not candles:
        return

    # Monta mapa "HH:MM" → resultado para os candles retornados
    candle_map: dict = {}
    for c in candles:
        hora_full = c.get("hora", "")   # ex: "09:00:00"
        hora_hm   = hora_full[:5]       # ex: "09:00"
        resultado = c.get("resultado")  # "win" ou "loss"
        if hora_hm and resultado:
            candle_map[hora_hm] = resultado

    if not candle_map:
        return

    try:
        messages = _db.load_messages().get("messages", [])
    except Exception as exc:
        logger.warning("Erro ao carregar mensagens para candle check: %s", exc)
        return

    updated = 0
    for msg in messages:
        candle_hour = msg.get("candle_hour")
        if not candle_hour:
            continue
        result = candle_map.get(candle_hour)
        if result and result != msg.get("candle_result"):
            try:
                _db.update_candle_result(msg["id"], result)
                logger.info(
                    "Candle %s → %s  |  mensagem '%s'",
                    candle_hour, result.upper(), msg.get("name", msg["id"]),
                )
                updated += 1
            except Exception as exc:
                logger.warning("Erro ao salvar resultado candle: %s", exc)

    if updated:
        logger.info("Candle checker: %d resultado(s) atualizado(s)", updated)


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
                      message.get("parse_mode", "HTML"),
                      message.get("video_note"),
                      bool(message.get("conditional_enabled", False)),
                      message.get("candle_hour"),
                      message.get("conditional_win") or {},
                      message.get("conditional_loss") or {}],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            job_count += 1

    # ── Job de verificação automática de candles (a cada minuto, aos :30s) ──
    scheduler.add_job(
        check_candle_results,
        trigger=CronTrigger(second=30, timezone=timezone),
        id="candle_checker",
        replace_existing=True,
        misfire_grace_time=30,
    )

    logger.info("%d agendamento(s) de mensagem + candle checker configurados", job_count)


async def reload_scheduler(scheduler: AsyncIOScheduler, bot: Bot, config: dict) -> None:
    setup_scheduler(scheduler, bot, config)
    logger.info("Scheduler recarregado com sucesso")
