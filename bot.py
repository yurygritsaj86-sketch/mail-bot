import asyncio
import logging
import imaplib
import email
import smtplib
import os
import json
import tempfile
import re
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import anthropic
import openai

from config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    YANDEX_EMAIL, YANDEX_APP_PASSWORD,
    ANTHROPIC_API_KEY, OPENAI_API_KEY,
    ALLOWED_DOMAINS, MONITORED_FOLDERS,
    IMAP_CHECK_INTERVAL
)

YANDEX_OAUTH_TOKEN = os.getenv("YANDEX_OAUTH_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

pending_emails: dict[str, dict] = {}

class MailFlow(StatesGroup):
    waiting_context   = State()
    waiting_own_reply = State()
    confirm_send      = State()

SYSTEM_PROMPT = """Ты — персональный ассистент Грицай Юрия Александровича, руководителя проекта и заместителя директора по корпоративному питанию компании «Партнеры Красноярск».

Твоя задача — помогать Юрию составлять ответы на рабочие письма. Ответы должны звучать как живая человеческая речь — без канцелярита, без шаблонных фраз, без признаков ИИ. Всегда обращайся к адресату по имени-отчеству. Если в переписке несколько человек — используй «коллеги».

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ОПРЕДЕЛЕНИЕ РОЛИ ОТПРАВИТЕЛЯ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. ЗАКАЗЧИК — если email содержит домен @polymetal.ru, @areal.ru или отправитель явно представляет заказчика в тексте письма.

2. ДИРЕКТОР (выше по иерархии) — если отправитель один из:
   - Устинова Ирина Александровна
   - Катцина Ирина
   - Тиунов Ярослав Алексеевич
   - Матвеев Иван Владимирович
   - Погодаева Виктория Олеговна
   - Коваленко Анастасия
   - Шиятая Светлана Юрьевна
   - Мерц Софья

3. РАВНЫЙ КОЛЛЕГА — коллега с @p-krsk.ru не входящий в список директоров и не являющийся подчинённым Юрия.

4. ПОДЧИНЁННЫЙ — сотрудник которым Юрий руководит напрямую на своих проектах.

Если роль неочевидна — определяй по контексту письма: тон, должность в подписи, характер просьбы.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
СТИЛЬ ОТВЕТА ПО РОЛЯМ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ЗАКАЗЧИК:
- Деловой, уверенный, партнёрский тон
- Защищай интересы Исполнителя — не допускай незафиксированных обязательств
- Не соглашайся на доп. объём без фиксации стоимости и сроков
- Вежливо но твёрдо

ДИРЕКТОР (выше по иерархии):
- Уважительно, кратко, конструктивно
- Без лишних слов — только суть и позиция
- Обязательно по имени-отчеству
- Тон уважительный но не подобострастный

РАВНЫЙ КОЛЛЕГА:
- Неформально, по делу
- Живо, без официоза
- Обращение по имени-отчеству
- Тон дружеский но профессиональный

ПОДЧИНЁННЫЙ:
- Вызывать уважение и желание работать
- Тон поддерживающий, доверительный
- Чёткие указания без давления
- Обращение по имени-отчеству
- Мотивирующий финал если уместно

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ОБЩИЕ ПРАВИЛА НАПИСАНИЯ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Никакого канцелярита
- Живые обороты: «посмотрел — вижу такую картину», «давайте разберёмся»
- Всегда по имени-отчеству в начале письма
- Письмо должно читаться как написанное живым человеком
- Никаких маркированных списков в тексте ответа — только живые абзацы

ВАЖНО: Ответ верни строго в формате JSON (без markdown, без преамбулы):
{
  "role": "заказчик / директор / равный коллега / подчинённый",
  "summary": "Суть запроса в 1-2 предложениях",
  "risks": "Риски для Исполнителя если применимо, иначе пустая строка",
  "position": "Рекомендованная позиция Юрия",
  "fix_in_writing": "Что нужно зафиксировать письменно если применимо, иначе пустая строка",
  "variant_1": "Первый вариант ответа",
  "variant_2": "Второй вариант ответа",
  "variant_3": "Третий вариант ответа"
}"""


def encode_folder_utf7(s: str) -> str:
    import base64 as b64
    res = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '&':
            res.append('&-')
            i += 1
        elif ord(c) < 128:
            res.append(c)
            i += 1
        else:
            j = i
            while j < len(s) and ord(s[j]) >= 128:
                j += 1
            non_ascii = s[i:j]
            encoded = b64.b64encode(non_ascii.encode('utf-16-be')).decode('ascii')
            encoded = encoded.replace('/', ',')
            res.append('&' + encoded + '-')
            i = j
    return ''.join(res)


def clean_html(text: str) -> str:
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text


def decode_str(value: str) -> str:
    parts = decode_header(value)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += str(part)
    return result


def get_body(msg) -> str:
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            if ct == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
            elif ct == "text/html" and not html_body:
                charset = part.get_content_charset() or "utf-8"
                html_body = part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True).decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            html_body = payload
        else:
            return payload

    if html_body:
        html_body = re.sub(r'<[^>]+>', ' ', html_body)
        html_body = re.sub(r'\s+', ' ', html_body).strip()
        return html_body
    return ""


def sender_allowed(from_field: str) -> bool:
    from_lower = from_field.lower()
    return any(domain in from_lower for domain in ALLOWED_DOMAINS)


def mark_as_read(folder: str, uid: str):
    try:
        imap = imaplib.IMAP4_SSL("imap.yandex.ru")
        imap.login(YANDEX_EMAIL, YANDEX_APP_PASSWORD)
        encoded_folder = encode_folder_utf7(folder)
        imap.select(f'"{encoded_folder}"')
        imap.store(uid, '+FLAGS', '\\Seen')
        imap.logout()
        log.info(f"Письмо {uid} в папке {folder} помечено как прочитанное")
    except Exception as e:
        log.error(f"Ошибка пометки письма: {e}")


def send_email(to: str, subject: str, body: str) -> bool:
    try:
        match = re.search(r'<(.+?)>', to)
        to_email = match.group(1) if match else to.strip()

        msg = MIMEMultipart()
        msg["From"] = YANDEX_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # OAuth2 строка аутентификации для Яндекса
        auth_string = f"user={YANDEX_EMAIL}\x01auth=Bearer {YANDEX_OAUTH_TOKEN}\x01\x01"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()

        with smtplib.SMTP_SSL("smtp.yandex.ru", 465) as server:
            server.ehlo()
            server.docmd("AUTH", "XOAUTH2 " + auth_b64)
            server.sendmail(YANDEX_EMAIL, to_email, msg.as_string())

        log.info(f"Письмо отправлено через Яндекс OAuth: {to_email} / {subject}")
        return True
    except Exception as e:
        log.error(f"Яндекс OAuth SMTP ошибка: {e}")
        return False


async def check_mail():
    seen_ids: set[str] = set()

    while True:
        try:
            imap = imaplib.IMAP4_SSL("imap.yandex.ru")
            imap.login(YANDEX_EMAIL, YANDEX_APP_PASSWORD)
            log.info("IMAP подключение успешно")

            for folder in MONITORED_FOLDERS:
                try:
                    encoded_folder = encode_folder_utf7(folder)
                    status, _ = imap.select(f'"{encoded_folder}"')
                    if status != "OK":
                        log.warning(f"Папка не найдена: {folder}")
                        continue

                    log.info(f"Проверяю папку: {folder}")
                    _, data = imap.search(None, "UNSEEN")
                    if not data[0]:
                        continue

                    for uid in data[0].split():
                        uid_str = uid.decode()
                        global_id = f"{folder}:{uid_str}"

                        if global_id in seen_ids:
                            continue
                        seen_ids.add(global_id)

                        _, msg_data = imap.fetch(uid, "(RFC822)")
                        imap.store(uid, '-FLAGS', '\\Seen')
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)

                        from_field = decode_str(msg.get("From", ""))
                        if not sender_allowed(from_field):
                            log.info(f"Отправитель не в списке: {from_field}")
                            continue

                        subject = decode_str(msg.get("Subject", "(без темы)"))
                        reply_to = msg.get("Reply-To") or msg.get("From", "")
                        body = get_body(msg)[:3000]

                        pending_emails[global_id] = {
                            "from": from_field,
                            "subject": subject,
                            "body": body,
                            "reply_to": reply_to,
                            "folder": folder,
                            "uid": uid_str,
                        }

                        await notify_user(global_id)

                except Exception as e:
                    log.error(f"Ошибка папки {folder}: {e}")

            imap.logout()

        except Exception as e:
            log.error(f"IMAP ошибка: {e}")

        await asyncio.sleep(IMAP_CHECK_INTERVAL)


async def notify_user(email_key: str):
    em = pending_emails[email_key]

    from_safe    = clean_html(em['from'])
    subject_safe = clean_html(em['subject'])
    body_safe    = clean_html(em['body'][:1500])
    folder_safe  = clean_html(em['folder'])

    text = (
        f"📩 <b>Новое письмо</b>\n"
        f"📁 Папка: <i>{folder_safe}</i>\n"
        f"👤 От: {from_safe}\n"
        f"📌 Тема: <b>{subject_safe}</b>\n\n"
        f"<pre>{body_safe}</pre>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ответить на это письмо", callback_data=f"reply:{email_key}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{email_key}")],
    ])

    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error(f"Ошибка отправки в Telegram: {e}")
        try:
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"Новое письмо от {em['from']}\nТема: {em['subject']}",
                reply_markup=kb
            )
        except Exception as e2:
            log.error(f"Повторная ошибка Telegram: {e2}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("👋 Почтовый ассистент запущен.\nЖду новых писем с разрешённых адресов.")


@dp.callback_query(F.data.startswith("reply:"))
async def reply_to_email(call: CallbackQuery, state: FSMContext):
    email_key = call.data.split("reply:")[1]

    if email_key not in pending_emails:
        await call.message.answer("⚠️ Письмо уже обработано или устарело.")
        await call.answer()
        return

    await state.set_state(MailFlow.waiting_context)
    await state.update_data(current_email_key=email_key)

    em = pending_emails[email_key]
    await call.message.answer(
        f"✏️ Отвечаю на письмо: <b>{clean_html(em['subject'])}</b>\n\n"
        f"Напиши или надиктуй голосом что хочешь ответить и какую позицию занять.",
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def skip_email(call: CallbackQuery, state: FSMContext):
    email_key = call.data.split("skip:")[1]
    pending_emails.pop(email_key, None)

    data = await state.get_data()
    if data.get("current_email_key") == email_key:
        await state.clear()

    await call.message.edit_text("⏭ Письмо пропущено.")
    await call.answer()


@dp.message(MailFlow.waiting_context, F.voice)
async def handle_voice_context(message: Message, state: FSMContext):
    data = await state.get_data()
    email_key = data.get("current_email_key")

    if not email_key or email_key not in pending_emails:
        await message.answer("⚠️ Сначала нажми кнопку «Ответить на это письмо».")
        await state.clear()
        return

    await message.answer("🎙 Распознаю голос...")

    file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await bot.download_file(file.file_path, tmp_path)

    with open(tmp_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=audio_file, language="ru"
        )
    os.unlink(tmp_path)

    context_text = transcript.text
    await message.answer(f"📝 Распознано: <i>{clean_html(context_text)}</i>", parse_mode="HTML")
    await generate_and_show_variants(message, state, email_key, context_text)


@dp.message(MailFlow.waiting_context, F.text)
async def handle_text_context(message: Message, state: FSMContext):
    data = await state.get_data()
    email_key = data.get("current_email_key")

    if not email_key or email_key not in pending_emails:
        await message.answer("⚠️ Сначала нажми кнопку «Ответить на это письмо».")
        await state.clear()
        return

    await generate_and_show_variants(message, state, email_key, message.text)


async def generate_and_show_variants(message: Message, state: FSMContext, email_key: str, context: str):
    em = pending_emails[email_key]
    await message.answer("⏳ Анализирую письмо, генерирую варианты...")

    user_prompt = (
        f"ПИСЬМО:\nОт: {em['from']}\nТема: {em['subject']}\n\n{em['body']}\n\n"
        f"КОНТЕКСТ ОТ ЮРИЯ:\n{context}"
    )

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = response.content[0].text.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        result = json.loads(match.group()) if match else {}

    await state.update_data(
        email_key=email_key,
        variant_1=result.get("variant_1", ""),
        variant_2=result.get("variant_2", ""),
        variant_3=result.get("variant_3", ""),
    )

    role = result.get("role", "не определена")
    role_emoji = {
        "заказчик": "🏢",
        "директор": "👔",
        "равный коллега": "🤝",
        "подчинённый": "👥",
    }.get(role, "❓")

    analysis_parts = [f"{role_emoji} <b>Роль:</b> {clean_html(role)}"]
    analysis_parts.append(f"🔍 <b>Суть:</b> {clean_html(result.get('summary', '—'))}")
    analysis_parts.append(f"📌 <b>Позиция:</b> {clean_html(result.get('position', '—'))}")
    if result.get("risks"):
        analysis_parts.append(f"⚠️ <b>Риски:</b> {clean_html(result.get('risks', ''))}")
    if result.get("fix_in_writing"):
        analysis_parts.append(f"📋 <b>Зафиксировать:</b> {clean_html(result.get('fix_in_writing', ''))}")

    await message.answer("\n\n".join(analysis_parts), parse_mode="HTML")

    v1 = clean_html(result.get("variant_1", "")[:400])
    v2 = clean_html(result.get("variant_2", "")[:400])
    v3 = clean_html(result.get("variant_3", "")[:400])

    variants_text = (
        f"✉️ <b>Варианты ответа:</b>\n\n"
        f"<b>1️⃣</b>\n{v1}\n\n"
        f"<b>2️⃣</b>\n{v2}\n\n"
        f"<b>3️⃣</b>\n{v3}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Вариант 1", callback_data="send_variant:1")],
        [InlineKeyboardButton(text="2️⃣ Вариант 2", callback_data="send_variant:2")],
        [InlineKeyboardButton(text="3️⃣ Вариант 3", callback_data="send_variant:3")],
        [InlineKeyboardButton(text="✏️ Свой вариант", callback_data="send_variant:own")],
        [InlineKeyboardButton(text="❌ Отмена",        callback_data="send_variant:cancel")],
    ])

    await message.answer(variants_text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("send_variant:"))
async def choose_variant(call: CallbackQuery, state: FSMContext):
    choice = call.data.split("send_variant:")[1]
    data = await state.get_data()

    if choice == "cancel":
        email_key = data.get("email_key")
        pending_emails.pop(email_key, None)
        await state.clear()
        await call.message.edit_text("❌ Отменено.")
        return

    if choice == "own":
        await call.message.answer("✏️ Напишите ваш вариант ответа:")
        await state.set_state(MailFlow.waiting_own_reply)
        await call.answer()
        return

    variant_text = data.get(f"variant_{choice}", "")
    await state.update_data(selected_reply=variant_text)
    await state.set_state(MailFlow.confirm_send)

    await call.message.answer(
        f"📤 <b>Будет отправлено:</b>\n\n{clean_html(variant_text)}\n\nПодтвердить отправку?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="🔙 Назад",     callback_data="confirm:no"),
        ]])
    )
    await call.answer()


@dp.message(MailFlow.waiting_own_reply, F.text)
async def handle_own_reply(message: Message, state: FSMContext):
    await state.update_data(selected_reply=message.text)
    await state.set_state(MailFlow.confirm_send)

    await message.answer(
        f"📤 <b>Будет отправлено:</b>\n\n{clean_html(message.text)}\n\nПодтвердить?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="🔙 Назад",     callback_data="confirm:no"),
        ]])
    )


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_send(call: CallbackQuery, state: FSMContext):
    choice = call.data.split("confirm:")[1]
    data = await state.get_data()

    if choice == "no":
        await state.set_state(MailFlow.waiting_context)
        await call.message.answer("🔙 Нажми «Ответить на это письмо» и отправь новый контекст.")
        await call.answer()
        return

    email_key = data.get("email_key")
    reply_text = data.get("selected_reply", "")
    em = pending_emails.get(email_key, {})

    await call.answer()
    await call.message.edit_text("📤 Отправляю письмо...")

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None, send_email,
        em.get("reply_to", ""),
        f"Re: {em.get('subject', '')}",
        reply_text,
    )

    if success:
        await call.message.edit_text("✅ Письмо отправлено!")
        mark_as_read(em.get("folder", "INBOX"), em.get("uid", ""))
        pending_emails.pop(email_key, None)
        await state.clear()
    else:
        await call.message.edit_text("❌ Ошибка отправки. Проверь логи Railway.")


async def main():
    log.info("Бот запускается...")
    asyncio.create_task(check_mail())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
