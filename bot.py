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
    try:
        me = await context.bot.get_me()
        add_url = f"https://t.me/{me.username}?startgroup=novo"
    except Exception:
        add_url = None

    kb = None
    link_line = ""
    if add_url:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Adicionar a um grupo", url=add_url)
        ]])
        link_line = f"\nSe o botão não abrir: <a href=\"{add_url}\">{add_url}</a>\n"

    await update.message.reply_text(
        "🤖 <b>Bot Manager ativo!</b>\n\n"
        "📲 <b>Adicionar a um grupo:</b> toque no botão abaixo e escolha o grupo. "
        "Assim que eu entrar, ele aparece <b>automaticamente no painel</b>."
        + link_line +
        "\n🔥 <b>Emojis animados:</b> envie aqui uma mensagem com os emojis animados que deseja usar "
        "— o ID é salvo automaticamente para uso nas mensagens.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def cmd_add_group(update, context) -> None:
    """Mostra o botão para adicionar o bot a um grupo (deep link startgroup)."""
    me = await context.bot.get_me()
    if not me.username:
        await update.message.reply_text(
            "⚠️ Este bot ainda não tem um @username definido no BotFather, "
            "então não consigo gerar o link de adicionar a grupo."
        )
        return

    add_url = f"https://t.me/{me.username}?startgroup=novo"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Selecionar grupo e me adicionar", url=add_url)
    ]])
    await update.message.reply_text(
        "Toque no botão e <b>escolha o grupo</b> onde quer me adicionar.\n"
        f"Se o botão não abrir, use este link:\n👉 <a href=\"{add_url}\">{add_url}</a>\n\n"
        "Ao entrar, eu registro o grupo no painel automaticamente. ✅\n\n"
        "<i>⚠️ Importante:</i> no <b>@BotFather → Bot Settings → Allow Groups?</b> precisa estar "
        "<b>Enabled</b>, senão o Telegram não deixa me adicionar a grupos.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


def _register_group_from_chat(chat, bot_token: Optional[str] = None) -> tuple:
    """Registra o grupo no config se ainda não existir. Retorna (registrado, nome, key).
    Associa o grupo ao bot dono (pelo token), para o multi-bot funcionar."""
    import db as _db
    import re as _re
    cfg = _db.load_config()
    groups = cfg.setdefault("groups", {})

    # descobre o bot_id dono a partir do token
    owner_bot_id = None
    if bot_token:
        for b in cfg.get("bots", []):
            if b.get("token") == bot_token:
                owner_bot_id = b.get("id")
                break

    # já registrado por chat_id? (atualiza o dono se faltava)
    for k, g in groups.items():
        if str(g.get("id")) == str(chat.id):
            if owner_bot_id and not g.get("bot_id"):
                g["bot_id"] = owner_bot_id
                _db.save_config(cfg)
            return (False, g.get("name", k), k)

    base = "grp_" + (_re.sub(r"[^a-z0-9]+", "", (chat.title or "grupo").lower())[:14] or "novo")
    key, i = base, 2
    while key in groups:
        key = f"{base}{i}"; i += 1

    groups[key] = {
        "name": chat.title or key,
        "id": str(chat.id),
        "default_buttons": [],
        "color": "#229ED9",
        "bot_id": owner_bot_id,
    }
    _db.save_config(cfg)
    return (True, groups[key]["name"], key)


async def on_my_chat_member(update, context) -> None:
    """Detecta quando o bot é adicionado/removido de um grupo e registra no painel."""
    cm = update.my_chat_member
    if not cm:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup"):
        return

    new_status = cm.new_chat_member.status if cm.new_chat_member else None
    if new_status not in ("member", "administrator"):
        return  # saiu/foi removido/banido — ignora

    try:
        registered, name, key = _register_group_from_chat(chat, getattr(context.bot, "token", None))
    except Exception as exc:
        logger.error("Falha ao registrar grupo %s: %s", chat.id, exc)
        return

    try:
        if registered:
            logger.info("Grupo registrado via chat: %s (%s) → %s", name, chat.id, key)
            await context.bot.send_message(
                chat_id=chat.id,
                text=(f"✅ <b>Conectado!</b>\n\nEste grupo foi registrado no painel como "
                      f"<b>{name}</b>.\nAgora você já pode agendar mensagens para ele no dashboard. 🚀"),
                parse_mode="HTML",
            )
        else:
            logger.info("Grupo já estava registrado: %s (%s)", name, chat.id)
    except Exception as exc:
        logger.warning("Não consegui enviar confirmação no grupo %s: %s", chat.id, exc)


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


async def handle_sticker_registration(update, context) -> None:
    """Recebe um sticker em chat privado e responde com o file_id correto para este bot.

    O file_id de sticker é único por bot — não é possível reutilizar file_ids de outros
    bots ou do @RawDataBot. Envie o sticker aqui para obter o file_id válido.
    """
    msg = update.message
    if not msg or not msg.sticker:
        return

    sticker  = msg.sticker
    file_id  = sticker.file_id
    emoji    = sticker.emoji or "—"
    animated = sticker.is_animated
    video    = sticker.is_video
    tipo     = "Animado 🌀" if animated else ("Vídeo 🎬" if video else "Estático 🖼")

    await msg.reply_text(
        f"🎭 <b>Sticker recebido!</b>\n\n"
        f"<b>File ID (copie isso):</b>\n"
        f"<code>{file_id}</code>\n\n"
        f"Tipo: {tipo}  |  Emoji: {emoji}\n\n"
        f"Cole esse <code>file_id</code> no campo <b>Sticker</b> do condicional no dashboard.\n"
        f"<i>⚠️ Este file_id funciona apenas com este bot específico.</i>",
        parse_mode="HTML",
    )


def setup_handlers(app) -> None:
    """Registra handlers de comandos e mensagens no Application do Telegram."""
    from telegram.ext import MessageHandler, CommandHandler, ChatMemberHandler
    from telegram.ext import filters as tg_filters

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["adicionar", "grupo", "grupos"], cmd_add_group))

    # Bot adicionado/removido de um grupo → registra o grupo no painel
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Stickers em chat privado → retorna o file_id correto para este bot
    app.add_handler(MessageHandler(
        tg_filters.ChatType.PRIVATE & tg_filters.Sticker.ALL,
        handle_sticker_registration,
    ))

    # Texto/foto em chat privado → registra emojis animados
    app.add_handler(MessageHandler(
        tg_filters.ChatType.PRIVATE & ~tg_filters.COMMAND & ~tg_filters.Sticker.ALL,
        handle_emoji_registration,
    ))
    logger.info("Handlers configurados: /adicionar, grupos, stickers e emojis")


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
    candle_symbol: Optional[str] = None,
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
                raise RuntimeError(
                    "Arquivo de vídeo não encontrado no servidor. Reenvie o vídeo "
                    "(uploads antigos podem ter sido perdidos em redeploy)."
                )
            except Exception as exc:
                logger.error("Erro ao enviar vídeo bolinha '%s': %s", video_note, exc)
                raise
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
            logger.info(
                "Condicional ATIVADO para '%s' | candle=%s | aguardando %ds...",
                message_name, candle_hour, _CANDLE_CLOSE_WAIT,
            )
            asyncio.create_task(
                _check_and_send_conditional(
                    bot=bot,
                    chat_id=chat_id,
                    message_name=message_name,
                    candle_hour=candle_hour,
                    conditional_win=conditional_win if isinstance(conditional_win, dict) else {},
                    conditional_loss=conditional_loss if isinstance(conditional_loss, dict) else {},
                    button_keys=button_keys,
                    config=config,
                    candle_symbol=candle_symbol,
                )
            )

    except Exception as exc:
        logger.error("Erro ao enviar '%s' para %s: %s", message_name, chat_id, exc)
        raise


_CANDLES_API_BASE = "https://web-production-cdff3.up.railway.app"
_CANDLES_API_URL = _CANDLES_API_BASE + "/candles"   # mantido p/ retrocompat
_DEFAULT_CANDLE_SYMBOL = "BTCUSD-OTC"
_CANDLE_CLOSE_WAIT = 70   # 60s (duração M1) + 10s buffer


def _candles_url(symbol: Optional[str] = None) -> str:
    """URL da API de candles. Com símbolo → consulta só aquele par."""
    if symbol:
        return f"{_CANDLES_API_BASE}/candles/{symbol}"
    return _CANDLES_API_URL


async def _fetch_candle_map(symbol: Optional[str] = None) -> dict:
    """Retorna {'HH:MM': 'win'|'loss'} para o símbolo dado (ou padrão)."""
    sym = symbol or _DEFAULT_CANDLE_SYMBOL

    def _fetch() -> dict:
        with urllib.request.urlopen(_candles_url(sym), timeout=10) as resp:
            return json.loads(resp.read())

    data = await asyncio.to_thread(_fetch)
    candle_map: dict = {}
    for c in data.get("candles", []):
        hora_hm   = c.get("hora", "")[:5]
        resultado = c.get("resultado")
        if hora_hm and resultado:
            candle_map[hora_hm] = resultado
    return candle_map


async def _check_and_send_conditional(
    bot: Bot,
    chat_id: str,
    message_name: str,
    candle_hour: str,
    conditional_win: dict,
    conditional_loss: dict,
    button_keys: list,
    config: dict,
    candle_symbol: Optional[str] = None,
    delay: int = _CANDLE_CLOSE_WAIT,
) -> None:
    """Aguarda o fechamento da vela M1 e envia a mensagem condicional WIN ou LOSS.

    Fluxo:
      1. Dorme ``delay`` segundos (padrão 70 = 60s vela + 10s buffer).
      2. Consulta a API do ativo ``candle_symbol`` (com retry após 30s).
      3. Localiza o candle de ``candle_hour`` (formato HH:MM).
      4. Envia a mensagem condicional correspondente (WIN ou LOSS) para ``chat_id``.
    """
    sym = candle_symbol or _DEFAULT_CANDLE_SYMBOL
    logger.info("Condicional '%s': aguardando %ds para verificar candle %s do ativo %s",
                message_name, delay, candle_hour, sym)
    await asyncio.sleep(delay)

    result = None

    # Tenta até 3 vezes (70s, 100s, 130s desde o envio) antes de desistir
    for attempt in range(1, 4):
        try:
            candle_map = await _fetch_candle_map(sym)
        except Exception as exc:
            logger.warning("Condicional '%s': falha na API %s (tentativa %d): %s",
                           message_name, sym, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(30)
            continue

        logger.info("Condicional '%s' [%s]: candles disponíveis = %s (procurando %s, tent.%d)",
                    message_name, sym, list(candle_map.keys()), candle_hour, attempt)

        result = candle_map.get(candle_hour)
        if result:
            break

        logger.info("Condicional '%s': candle %s ainda não disponível — aguardando 30s (tent.%d/3)",
                    message_name, candle_hour, attempt)
        if attempt < 3:
            await asyncio.sleep(30)

    if not result:
        logger.warning("Condicional '%s': candle %s (%s) não encontrado após 3 tentativas — abortando",
                       message_name, candle_hour, sym)
        return

    logger.info("Condicional '%s': resultado %s para candle %s (%s) — enviando para %s",
                message_name, result.upper(), candle_hour, sym, chat_id)

    # Garante que cond_msg é sempre um dict (psycopg2 pode retornar string JSONB)
    raw_win  = conditional_win  if isinstance(conditional_win,  dict) else {}
    raw_loss = conditional_loss if isinstance(conditional_loss, dict) else {}
    cond_msg = raw_win if result == "win" else raw_loss

    if not cond_msg:
        logger.warning("Condicional '%s' [%s]: nenhum dicionário de mensagem configurado "
                       "(cond_msg vazio) — verifique se salvou o condicional.", message_name, result.upper())
        return

    logger.info("Condicional '%s' [%s]: cond_msg = %s", message_name, result.upper(), cond_msg)

    has_content = any(cond_msg.get(k) for k in ("text", "image", "video_note", "sticker"))
    if not has_content:
        logger.warning("Condicional '%s' [%s]: campos text/image/video_note/sticker todos vazios",
                       message_name, result.upper())
        return

    try:
        await _dispatch_conditional(bot, chat_id, message_name, result, cond_msg, button_keys, config)
    except Exception as exc:
        logger.error("Condicional '%s' [%s] falhou no envio agendado: %s",
                     message_name, result.upper(), exc)
        return

    # Atualiza o candle_result no banco para refletir o resultado recém-enviado
    try:
        import db as _db
        _db.update_candle_result_by_name(message_name, result)
    except Exception:
        pass  # não crítico


async def _dispatch_conditional(
    bot: Bot,
    chat_id: str,
    message_name: str,
    result: str,
    cond_msg: dict,
    button_keys: list,
    config: dict,
) -> None:
    """Envia o conteúdo condicional (sticker, vídeo bolinha, texto ou foto)."""
    text_c       = cond_msg.get("text", "") or ""
    image_c      = cond_msg.get("image") or None
    video_note_c = cond_msg.get("video_note") or None
    sticker_c    = cond_msg.get("sticker") or None
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
            logger.info("Condicional [%s] '%s': enviando sticker '%s'",
                        result.upper(), message_name, sticker_c[:24])
            try:
                await bot.send_sticker(chat_id=chat_id, sticker=sticker_c)
            except Exception as sx:
                raise RuntimeError(
                    f"Falha ao enviar sticker (file_id inválido para este bot?). "
                    f"Envie o sticker ao bot no chat privado para obter o file_id correto. "
                    f"Detalhe: {sx}"
                )
            if text_c:
                await bot.send_message(chat_id=chat_id, text=text_c,
                                       parse_mode=pm, reply_markup=keyboard)

        elif video_note_c:
            logger.info("Condicional [%s] '%s': enviando vídeo bolinha", result.upper(), message_name)
            await send_scheduled_message(
                bot=bot, text="", chat_id=chat_id,
                button_keys=[], config=config,
                image=None, message_name=f"{message_name} [{result.upper()}]",
                parse_mode="HTML", video_note=video_note_c,
            )

        elif image_c:
            logger.info("Condicional [%s] '%s': enviando foto", result.upper(), message_name)
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
            logger.info("Condicional [%s] '%s': enviando texto", result.upper(), message_name)
            await bot.send_message(chat_id=chat_id, text=text_c,
                                   parse_mode=pm, reply_markup=keyboard)

        logger.info("✅ Condicional [%s] '%s' enviado com sucesso para %s",
                    result.upper(), message_name, chat_id)

    except Exception as exc:
        logger.error("❌ Erro ao enviar condicional [%s] '%s' para %s: %s",
                     result.upper(), message_name, chat_id, exc)
        raise   # propaga p/ o chamador (ex.: botão Testar mostra o erro real)


async def check_candle_results() -> None:
    """Verifica a API de candles M1 e atualiza o resultado WIN/LOSS dos templates marcados.

    Roda a cada minuto (agendado em setup_scheduler). O horário do candle de cada
    mensagem é derivado dos horários dos seus agendamentos (ou do candle_hour manual,
    se definido — retrocompatibilidade).
    """
    import db as _db

    try:
        messages = _db.load_messages().get("messages", [])
    except Exception as exc:
        logger.warning("Erro ao carregar mensagens para candle check: %s", exc)
        return

    # Apenas mensagens que têm horários (agendamento ou candle_hour legado)
    relevant = []
    for msg in messages:
        hours = {s.get("time", "")[:5] for s in msg.get("schedules", []) if s.get("time")}
        if msg.get("candle_hour"):
            hours.add(msg["candle_hour"][:5])
        if hours:
            relevant.append((msg, hours))

    if not relevant:
        return

    # Busca candles por símbolo (uma requisição por ativo distinto)
    symbols = {(m.get("candle_symbol") or _DEFAULT_CANDLE_SYMBOL) for m, _ in relevant}
    symbol_maps: dict = {}
    for sym in symbols:
        try:
            symbol_maps[sym] = await _fetch_candle_map(sym)
        except Exception as exc:
            logger.warning("Candle API indisponível para %s: %s", sym, exc)
            symbol_maps[sym] = {}

    updated = 0
    for msg, hours in relevant:
        sym = msg.get("candle_symbol") or _DEFAULT_CANDLE_SYMBOL
        candle_map = symbol_maps.get(sym, {})
        if not candle_map:
            continue

        # Usa o resultado do candle mais recente disponível na API entre os horários
        latest_hour = None
        latest_result = None
        for h in hours:
            r = candle_map.get(h)
            if r and (latest_hour is None or h > latest_hour):
                latest_hour, latest_result = h, r

        if latest_result and latest_result != msg.get("candle_result"):
            try:
                _db.update_candle_result(msg["id"], latest_result)
                logger.info(
                    "Candle %s [%s] → %s  |  mensagem '%s'",
                    latest_hour, sym, latest_result.upper(), msg.get("name", msg["id"]),
                )
                updated += 1
            except Exception as exc:
                logger.warning("Erro ao salvar resultado candle: %s", exc)

    if updated:
        logger.info("Candle checker: %d resultado(s) atualizado(s)", updated)


async def send_conditional_now(
    bot: Bot,
    message_id: str,
    result: str,
    group_key: str,
    config: dict,
) -> dict:
    """Dispara imediatamente a mensagem condicional WIN ou LOSS de um template.

    Usado pelo endpoint /api/messages/<id>/test-conditional para testar sem
    depender do horário do candle.

    Retorna {"success": True} ou {"success": False, "error": "..."}.
    """
    import db as _db
    try:
        messages = _db.load_messages().get("messages", [])
        msg = next((m for m in messages if m["id"] == message_id), None)
        if not msg:
            return {"success": False, "error": "Mensagem não encontrada"}

        if not msg.get("conditional_enabled"):
            return {"success": False, "error": "Verificação condicional não está ativada nessa mensagem"}

        group = config.get("groups", {}).get(group_key)
        if not group or not group.get("id"):
            return {"success": False, "error": f"Grupo '{group_key}' não encontrado ou sem ID"}

        cond_key = "conditional_win" if result == "win" else "conditional_loss"
        cond_msg = _parse_jsonb_local(msg.get(cond_key) or {})

        if not cond_msg:
            return {"success": False, "error": f"Conteúdo condicional [{result.upper()}] não configurado"}

        has_content = any(cond_msg.get(k) for k in ("text", "image", "video_note", "sticker"))
        if not has_content:
            return {"success": False, "error": f"Condicional [{result.upper()}]: text/image/video_note/sticker vazios"}

        chat_id    = group["id"]
        button_keys = group.get("default_buttons", [])

        await _dispatch_conditional(
            bot=bot,
            chat_id=chat_id,
            message_name=msg.get("name", message_id),
            result=result,
            cond_msg=cond_msg,
            button_keys=button_keys,
            config=config,
        )
        return {"success": True}

    except Exception as exc:
        logger.error("send_conditional_now erro: %s", exc)
        return {"success": False, "error": str(exc)}


def _parse_jsonb_local(v) -> dict:
    """Helper local para garantir que v é um dict."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            r = json.loads(v)
            return r if isinstance(r, dict) else {}
        except Exception:
            return {}
    return {}


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot, config: dict,
                    bot_id: Optional[str] = None, primary_bot_id: Optional[str] = None) -> None:
    """Agenda as mensagens. Com vários bots, cada grupo é tratado APENAS pelo seu
    bot dono (group.bot_id). Grupos sem dono são tratados só pelo bot primário,
    evitando envios duplicados."""
    scheduler.remove_all_jobs()
    timezone = config.get("timezone", "America/Sao_Paulo")
    job_count = 0

    def _owns(group: dict) -> bool:
        # Sem multi-bot (bot_id None) → este bot cuida de tudo (comportamento antigo)
        if bot_id is None:
            return True
        owner = group.get("bot_id")
        if owner:
            return owner == bot_id
        # grupo sem dono → só o bot primário cuida
        return bot_id == primary_bot_id

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

            if not _owns(group):
                continue   # outro bot é o dono deste grupo

            button_keys = schedule.get("buttons", group.get("default_buttons", []))
            days = schedule.get("days", ["mon", "tue", "wed", "thu", "fri"])
            day_of_week = ",".join(DAYS_MAP.get(d, d) for d in days)

            hour, minute = map(int, schedule["time"].split(":"))
            job_id = f"{message['id']}_{schedule['id']}"

            # O candle a verificar é SEMPRE o do horário do próprio agendamento.
            candle_hour = schedule["time"][:5]   # "HH:MM"

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
                      candle_hour,
                      message.get("conditional_win") or {},
                      message.get("conditional_loss") or {},
                      message.get("candle_symbol")],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            job_count += 1

    # ── Job de verificação automática de candles (a cada minuto, aos :30s) ──
    # Só o bot primário roda o checker, para não duplicar atualizações no banco.
    if bot_id is None or bot_id == primary_bot_id:
        scheduler.add_job(
            check_candle_results,
            trigger=CronTrigger(second=30, timezone=timezone),
            id="candle_checker",
            replace_existing=True,
            misfire_grace_time=30,
        )

    logger.info("Bot %s: %d agendamento(s) configurados", bot_id or "default", job_count)


async def reload_scheduler(scheduler: AsyncIOScheduler, bot: Bot, config: dict) -> None:
    setup_scheduler(scheduler, bot, config)
    logger.info("Scheduler recarregado com sucesso")
