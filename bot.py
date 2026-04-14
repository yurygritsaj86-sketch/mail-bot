import asyncio
import logging
import imaplib
import email
import smtplib
import os
import json
import tempfile
import re
import httpx
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

MAILERSEND_API_TOKEN = os.getenv("MAILERSEND_API_TOKEN", "")
MAILERSEND_FROM = os.getenv("MAILERSEND_USER", "")

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

SYSTEM_PROMPT = """Ты — ассистент Исполнителя по подготовке ответов в рабочей переписке с Заказчиком.
Твоя задача — помогать формировать ответы, которые:
  - сохраняют деловые и партнёрские отношения;
  - защищают интересы Исполнителя;
  - не допускают незафиксированных обязательств по объёму, срокам, стоимости и ответственности.

На вход ты получаешь:
  - текст письма или сообщения Заказчика/Директоров/Кураторов;
  - контекст ситуации от Исполнителя (голосовой или текстовый);
  - при наличии — выдержки из договора, ТЗ, приложений.

Что ты должен сделать:
  1. Кратко объяснить, что реально просит Заказчик (1-2 предложения).
  2. Выявить риски для Исполнителя: деньги, сроки, объём, качество, ответственность.
  3. Определить, входит ли запрос в текущий объём договора или это допработы.
  4. Предложить рекомендованную позицию Исполнителя.
  5. Подготовить 3 варианта готового ответа: мягкий / нейтральный / жёсткий.
  6. Указать, что нужно зафиксировать письменно.

Правила:
  - не подтверждай новые обязательства без оговорок;
  - не соглашайся на допобъём без фиксации стоимости и сроков;
  - не допускай формулировок, ухудшающих позицию Исполнителя;
  - пиши кратко, вежливо, уверенно и по делу;
  - если данных недостаточно — задай уточняющие вопросы.

ВАЖНО: Ответ верни строго в формате JSON (без markdown, без преамбулы):
{
  "summary": "Суть запроса",
  "risks": "Риски для Исполнителя",
  "position": "Рекомендованная позиция",
  "fix_in_writing": "Что зафиксировать письменно",
  "variant_1": "Мягкий вариант ответа",
  "variant_2": "Нейтральный вариант ответа",
  "variant_3": "Жёсткий вариант ответа"
}"""


def encode_folder_utf7(s: str) -> str:
    import base64
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
            encoded = base64.b64encode(non_ascii.encode('utf-16-be')).decode('ascii')
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
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def sender_allowed(from_field: str) -> bool:
    from_lower = from_field.lower()
    return any(domain in from_lower for domain in ALLOWED_DOMAINS)


def mark_as_read(folder: str, uid: str):
    """Помечает письмо как прочитанное в IMAP — вызывается только после отправки ответа."""
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
                        # Сразу сбрасываем флаг прочитанного — письмо остаётся непрочитанным
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
        f"<pre>{body_safe}</pre>\n\n"
        f"✏️ <b>Добавь контекст</b> — напиши или надиктуй голосом, "
        f"что хочешь ответить и какую позицию занять."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{email_key}")
    ]])

    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error(f"Ошибка отправки в Telegram: {e}")
        try:
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"Новое письмо от {em['from']}\nТема: {em['subject']}\n\nНапишите контекст для ответа.",
                reply_markup=kb
            )
        except Exception as e2:
            log.error(f"Повторная ошибка Telegram: {e2}")
            return

    pending_emails[email_key]["notified"] = True
    state = dp.fsm.resolve_context(bot, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID)
    await state.set_state(MailFlow.waiting_context)
    await state.update_data(current_email_key=email_key)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("👋 Почтовый ассистент запущен.\nЖду новых писем с разрешённых адресов.")


@dp.callback_query(F.data.startswith("skip:"))
async def skip_email(call: CallbackQuery, state: FSMContext):
    email_key = call.data.split("skip:")[1]
    pending_emails.pop(email_key, None)
    await state.clear()
    await call.message.edit_text("⏭ Письмо пропущено.")


@dp.message(MailFlow.waiting_context, F.voice)
async def handle_voice_context(message: Message, state: FSMContext):
    data = await state.get_data()
    email_key = data.get("current_email_key")

    if not email_key or email_key not in pending_emails:
        await message.answer("⚠️ Письмо не найдено. Возможно, уже обработано.")
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
        await message.answer("⚠️ Письмо не найдено.")
        await state.clear()
        return

    await generate_and_show_variants(message, state, email_key, message.text)


async def generate_and_show_variants(message: Message, state: FSMContext, email_key: str, context: str):
    em = pending_emails[email_key]
    await message.answer("⏳ Анализирую письмо, генерирую варианты...")

    user_prompt = (
        f"ПИСЬМО:\nОт: {em['from']}\nТема: {em['subject']}\n\n{em['body']}\n\n"
        f"КОНТЕКСТ ОТ ИСПОЛНИТЕЛЯ:\n{context}"
    )

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
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

    analysis = (
        f"🔍 <b>Суть:</b> {clean_html(result.get('summary', '—'))}\n\n"
        f"⚠️ <b>Риски:</b> {clean_html(result.get('risks', '—'))}\n\n"
        f"📌 <b>Позиция:</b> {clean_html(result.get('position', '—'))}\n\n"
        f"📋 <b>Зафиксировать:</b> {clean_html(result.get('fix_in_writing', '—'))}"
    )
    await message.answer(analysis, parse_mode="HTML")

    v1 = clean_html(result.get("variant_1", "")[:300])
    v2 = clean_html(result.get("variant_2", "")[:300])
    v3 = clean_html(result.get("variant_3", "")[:300])

    variants_text = (
        f"✉️ <b>Варианты ответа:</b>\n\n"
        f"<b>1️⃣ Мягкий:</b>\n{v1}\n\n"
        f"<b>2️⃣ Нейтральный:</b>\n{v2}\n\n"
        f"<b>3️⃣ Жёсткий:</b>\n{v3}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Мягкий",      callback_data="send_variant:1")],
        [InlineKeyboardButton(text="2️⃣ Нейтральный",  callback_data="send_variant:2")],
        [InlineKeyboardButton(text="3️⃣ Жёсткий",      callback_data="send_variant:3")],
        [InlineKeyboardButton(text="✏️ Свой вариант",  callback_data="send_variant:own")],
        [InlineKeyboardButton(text="❌ Отмена",         callback_data="send_variant:cancel")],
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
        await call.message.answer("🔙 Отправьте новый контекст или выберите вариант заново.")
        await call.answer()
        return

    email_key = data.get("email_key")
    reply_text = data.get("selected_reply", "")
    em = pending_emails.get(email_key, {})

    success = await send_email(
        to=em.get("reply_to", ""),
        subject=f"Re: {em.get('subject', '')}",
        body=reply_text,
    )

    if success:
        await call.message.edit_text("✅ Письмо отправлено!")
        # Помечаем как прочитанное только после успешной отправки ответа
        mark_as_read(em.get("folder", "INBOX"), em.get("uid", ""))
        pending_emails.pop(email_key, None)
        await state.clear()
    else:
        await call.message.answer("❌ Ошибка отправки.")

    await call.answer()


async def send_email(to: str, subject: str, body: str) -> bool:
    try:
        match = re.search(r'<(.+?)>', to)
        to_email = match.group(1) if match else to.strip()

        payload = {
            "from": {
                "email": MAILERSEND_FROM,
                "name": "Почтовый ассистент"
            },
            "to": [{"email": to_email}],
            "subject": subject,
            "text": body,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.mailersend.com/v1/email",
                headers={
                    "Authorization": f"Bearer {MAILERSEND_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )

        if response.status_code in (200, 202):
            log.info(f"Письмо отправлено: {to_email} / {subject}")
            return True
        else:
            log.error(f"Mailersend ошибка: {response.status_code} {response.text}")
            return False

    except Exception as e:
        log.error(f"Ошибка отправки: {e}")
        return False


async def main():
    log.info("Бот запускается...")
    asyncio.create_task(check_mail())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
