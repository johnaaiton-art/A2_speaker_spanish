"""
HablaRápido - Spanish A2 Speaking Bot + Grammar Practice
Single-file bot, matches existing VM stack (systemd + venv + .env + google-creds.json)

MODE A: Vocab + speaking (original flow)
MODE B: Grammar + speaking (new)
  1. Past simple / Future simple  (grammar_past_future.json)
  2. 2nd and 3rd conditional       (grammar_conditionals.json)
  3. Present / past subjunctive   (grammar_subjunctive.json)
"""

import os
import re
import json
import logging
import asyncio
import uuid
import base64
import random
from datetime import datetime
from dotenv import load_dotenv

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from google.oauth2.service_account import Credentials
from google.cloud import texttospeech, speech

load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
GOOGLE_CREDS     = "google-creds.json"
SHEET_ID         = os.getenv("SHEET_ID")

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_ART_URL   = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"

IMAGES_DIR  = "images"
PACKS_FILE  = "word_packs.json"
TAB_VOCAB   = "Spanish A2"
TAB_PACKS   = "Custom Packs"
TAB_GRAMMAR = "Grammar Progress"

# Grammar JSON files (relative to bot working directory)
GRAMMAR_FILES = {
    "1": {"file": "grammar_past_future.json",   "label": "Pasado simple y futuro simple"},
    "2": {"file": "grammar_conditionals.json",  "label": "2ª y 3ª condicional"},
    "3": {"file": "grammar_subjunctive.json",   "label": "Subjuntivo presente y pasado"},
}

# ─── LOAD WORD PACKS ─────────────────────────────────────────────────────────

def load_builtin_packs() -> dict:
    if not os.path.exists(PACKS_FILE):
        logger.error(f"{PACKS_FILE} not found. Run generate_images.py first.")
        return {}
    with open(PACKS_FILE, "r", encoding="utf-8") as f:
        packs = json.load(f)
    logger.info(f"Loaded {len(packs)} built-in packs from {PACKS_FILE}")
    return packs

builtin_packs = load_builtin_packs()
custom_packs  = {}

def all_packs() -> dict:
    return {**builtin_packs, **custom_packs}

# ─── LOAD GRAMMAR DATA ────────────────────────────────────────────────────────

def load_grammar_pack(pack_key: str) -> list:
    info = GRAMMAR_FILES.get(pack_key)
    if not info:
        return []
    path = info["file"]
    if not os.path.exists(path):
        logger.error(f"Grammar file not found: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"Loaded {len(data)} grammar items from {path}")
    return data

# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_sheet_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_tab(sheet, tab_name: str, headers: list):
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=2000, cols=len(headers))
        ws.append_row(headers)
    return ws

def load_custom_packs_from_sheet() -> dict:
    packs = {}
    try:
        client = get_sheet_client()
        sheet  = client.open_by_key(SHEET_ID)
        ws     = get_or_create_tab(sheet, TAB_PACKS, ["pack_name", "word", "english"])
        for row in ws.get_all_records():
            pack = row.get("pack_name", "").strip()
            word = row.get("word", "").strip()
            eng  = row.get("english", "").strip()
            if pack and word:
                packs.setdefault(pack, []).append({"word": word, "english": eng})
        logger.info(f"Loaded custom packs: {list(packs.keys())}")
    except Exception as e:
        logger.error(f"Failed to load custom packs: {e}")
    return packs

def save_pack_to_sheet(pack_name: str, words: list):
    client = get_sheet_client()
    sheet  = client.open_by_key(SHEET_ID)
    ws     = get_or_create_tab(sheet, TAB_PACKS, ["pack_name", "word", "english"])
    for w in words:
        ws.append_row([pack_name, w["word"], w.get("english", "")])

def get_student_tab_name(user: object) -> str:
    if user.username:
        name = user.username
    elif user.first_name:
        name = user.first_name
        if user.last_name:
            name += f"_{user.last_name}"
    else:
        name = str(user.id)
    safe = "".join(c for c in name if c.isalnum() or c in "_- ")[:50]
    return safe.strip() or str(user.id)

def save_vocab_word(word: str, english: str, theme: str, example: str, user: object):
    try:
        client   = get_sheet_client()
        sheet    = client.open_by_key(SHEET_ID)
        tab_name = get_student_tab_name(user)
        ws       = get_or_create_tab(
            sheet, tab_name,
            ["Date", "Word", "English", "Theme", "Example", "Status"]
        )
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d"),
            word, english, theme, example, "new"
        ])
    except Exception as e:
        logger.error(f"Failed to save vocab word: {e}")

# ─── GRAMMAR PROGRESS IN SHEETS ───────────────────────────────────────────────

def load_grammar_progress(user_id: int) -> dict:
    """
    Returns a dict: { question_id: {"correct": bool, "date": str} }
    """
    progress = {}
    try:
        client = get_sheet_client()
        sheet  = client.open_by_key(SHEET_ID)
        ws     = get_or_create_tab(
            sheet, TAB_GRAMMAR,
            ["user_id", "question_id", "tense", "correct", "student_answer",
             "correct_answer", "sentence", "date"]
        )
        for row in ws.get_all_records():
            if str(row.get("user_id")) == str(user_id):
                qid = row.get("question_id", "")
                if qid:
                    progress[qid] = {
                        "correct": row.get("correct", ""),
                        "date":    row.get("date", "")
                    }
    except Exception as e:
        logger.error(f"Failed to load grammar progress: {e}")
    return progress

def save_grammar_result(
    user_id: int, question: dict,
    student_answer: str, correct: bool
):
    try:
        client = get_sheet_client()
        sheet  = client.open_by_key(SHEET_ID)
        ws     = get_or_create_tab(
            sheet, TAB_GRAMMAR,
            ["user_id", "question_id", "tense", "correct", "student_answer",
             "correct_answer", "sentence", "date"]
        )
        ws.append_row([
            str(user_id),
            question["id"],
            question.get("tense_label", question.get("tense", "")),
            "✅" if correct else "❌",
            student_answer,
            question["answer"],
            question["full_sentence"],
            datetime.now().strftime("%Y-%m-%d %H:%M")
        ])
    except Exception as e:
        logger.error(f"Failed to save grammar result: {e}")

# ─── DEEPSEEK ────────────────────────────────────────────────────────────────

async def deepseek(prompt: str, system: str = "You are a friendly Spanish tutor for B2 learners.") -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt}
        ],
        "temperature": 0.7
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                DEEPSEEK_URL, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return "Lo siento, hay un problema técnico."

# ─── GOOGLE TTS ───────────────────────────────────────────────────────────────

async def make_tts(text: str) -> object:
    try:
        creds    = Credentials.from_service_account_file(GOOGLE_CREDS)
        client   = texttospeech.TextToSpeechClient(credentials=creds)
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code="es-US",
                name="es-US-Chirp3-HD-Aoede"
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.OGG_OPUS
            )
        )
        path = f"/tmp/tts_{uuid.uuid4()}.ogg"
        with open(path, "wb") as f:
            f.write(response.audio_content)
        return path
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None

# ─── GOOGLE STT ───────────────────────────────────────────────────────────────

async def transcribe_voice(path: str) -> object:
    try:
        creds  = Credentials.from_service_account_file(GOOGLE_CREDS)
        client = speech.SpeechClient(credentials=creds)
        with open(path, "rb") as f:
            audio_data = f.read()
        response = client.recognize(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
                sample_rate_hertz=48000,
                language_code="es-ES",
                alternative_language_codes=["es-US", "es-MX"]
            ),
            audio=speech.RecognitionAudio(content=audio_data)
        )
        if response.results:
            return response.results[0].alternatives[0].transcript
    except Exception as e:
        logger.error(f"STT error: {e}")
    return None

# ─── YANDEX ART ───────────────────────────────────────────────────────────────

async def generate_image_yandex_async(word: str, english: str) -> object:
    prompt = (
        f"Simple colorful cartoon illustration for Spanish vocabulary learning. "
        f"Word: '{word}' ({english}). "
        f"Clean white background, friendly and educational style."
    )
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "modelUri": f"art://{YANDEX_FOLDER_ID}/yandex-art/latest",
        "generationOptions": {
            "seed": 42,
            "aspectRatio": {"widthRatio": 1, "heightRatio": 1}
        },
        "messages": [{"text": prompt}]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                YANDEX_ART_URL, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json()
                operation_id = data.get("id")
            if not operation_id:
                return None
            result_url = f"https://llm.api.cloud.yandex.net:443/operations/{operation_id}"
            for _ in range(36):
                await asyncio.sleep(5)
                async with session.get(result_url, headers=headers) as r:
                    data = await r.json()
                if data.get("done"):
                    if "error" in data:
                        return None
                    img_b64 = data.get("response", {}).get("image")
                    if img_b64:
                        return base64.b64decode(img_b64)
                    return None
    except Exception as e:
        logger.error(f"Yandex Art error: {e}")
    return None

# ─── IMAGE SENDING ────────────────────────────────────────────────────────────

async def send_word_image(chat_id: int, context: ContextTypes.DEFAULT_TYPE, word_data: dict):
    image_file = word_data.get("image")
    if image_file:
        img_path = os.path.join(IMAGES_DIR, image_file)
        if os.path.exists(img_path):
            with open(img_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f)
            return
    word      = word_data["word"]
    english   = word_data.get("english", "")
    img_bytes = await generate_image_yandex_async(word, english)
    if img_bytes:
        await context.bot.send_photo(chat_id=chat_id, photo=img_bytes)

# ─── SESSION MANAGEMENT ───────────────────────────────────────────────────────

sessions = {}

def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {
            # vocab mode
            "mode": None,           # "vocab" or "grammar"
            "theme": None, "words": [], "index": 0,
            "current_word": None, "current_english": None,
            "current_question": None, "help_level": 0,
            # grammar mode
            "grammar_pack_key": None,
            "grammar_items": [],
            "grammar_index": 0,
            "grammar_current": None,
            "grammar_awaiting_text": False,
            "grammar_awaiting_voice": False,
            "grammar_progress": {},
        }
    return sessions[user_id]

def next_theme(user_id: int) -> str:
    themes  = list(all_packs().keys())
    current = get_session(user_id).get("theme")
    try:
        idx = (themes.index(current) + 1) % len(themes)
    except ValueError:
        idx = 0
    return themes[idx]

# ─── GRAMMAR ANSWER CHECKING ─────────────────────────────────────────────────

def normalize(text: str) -> str:
    """
    Lowercase, strip accents, collapse whitespace, remove punctuation.
    So "Tuviera," == "tuviera" == "TUVIERA" == "túviera" (unlikely but safe).
    Spanish accent map covers all common cases.
    """
    text = text.lower().strip()
    accent_map = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    text = text.translate(accent_map)
    # Replace slashes/commas with space so "tuviera/viajaría" splits correctly
    text = re.sub(r"[/,]", " ", text)
    # Remove remaining punctuation except spaces
    text = re.sub(r"[^\w\s]", "", text)
    # Collapse multiple spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text

def check_grammar_answer(student_input: str, question: dict) -> bool:
    """
    Flexible check: all required verb forms must appear somewhere in the
    student's text. Accent-insensitive, punctuation-insensitive,
    whitespace-insensitive.

    Answer field formats:
      "hice"                                   — single form
      "quiso / pudo"                           — two independent forms (each must appear)
      "tuviera / viajaría"                     — conditional: both slots required
      "hubiera estudiado / habría aprobado"    — 3rd conditional compound forms
    """
    student_norm = normalize(student_input)
    # Build a set of individual words in the student's answer for fast lookup
    student_words = set(student_norm.split())

    # Split answer on "/" — each part is one required verb (possibly compound)
    required_parts = [p.strip() for p in question["answer"].split("/")]

    for part in required_parts:
        words = normalize(part).split()   # e.g. ["hubiera", "estudiado"]
        if len(words) == 1:
            if words[0] not in student_words:
                return False
        else:
            # Compound form: all component words must appear in student answer
            if not all(w in student_words for w in words):
                return False

    return True

# ─── GRAMMAR FLOW ─────────────────────────────────────────────────────────────

async def send_grammar_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    """Send the current grammar conjugation exercise."""
    items = s["grammar_items"]
    idx   = s["grammar_index"]

    if idx >= len(items):
        await grammar_block_complete(chat_id, context, s)
        return

    q = items[idx]
    s["grammar_current"]       = q
    s["grammar_awaiting_text"] = True
    s["grammar_awaiting_voice"] = False

    tense_label = q.get("tense_label", "")
    sentence    = q["sentence"]
    notes       = q.get("notes", "")

    progress_text = f"Pregunta {idx + 1} de {len(items)}"

    msg = (
        f"📝 *Gramática — {tense_label}*\n\n"
        f"Completa el verbo en paréntesis:\n\n"
        f"_{sentence}_\n\n"
        f"✍️ Escribe solo la forma verbal correcta."
    )
    if notes:
        msg += f"\n\n💡 _{notes}_"
    msg += f"\n\n_{progress_text}_"

    await context.bot.send_message(
        chat_id=chat_id, text=msg, parse_mode="Markdown"
    )

async def handle_grammar_text_answer(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE,
    s: dict, user_id: int, text: str
):
    """Process a written grammar answer, give result, then prompt for speaking."""
    q       = s["grammar_current"]
    correct = check_grammar_answer(text, q)

    s["grammar_awaiting_text"] = False

    # Save to Sheets
    save_grammar_result(user_id, q, text, correct)

    result_icon = "✅" if correct else "❌"
    full_sent   = q["full_sentence"]

    if correct:
        result_msg = (
            f"{result_icon} *¡Correcto!*\n\n"
            f"📖 Frase completa:\n_{full_sent}_"
        )
    else:
        result_msg = (
            f"{result_icon} *No exactamente.*\n\n"
            f"La forma correcta es: *{q['answer']}*\n\n"
            f"📖 Frase completa:\n_{full_sent}_"
        )

    await context.bot.send_message(
        chat_id=chat_id, text=result_msg, parse_mode="Markdown"
    )

    # Now prompt for speaking using the pre-written question
    await send_grammar_speaking_prompt(chat_id, context, s, q)

async def send_grammar_speaking_prompt(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict, q: dict
):
    """Send TTS speaking question and wait for voice reply."""
    speaking_q = q.get("speaking_question", "")
    if not speaking_q:
        # No question — move straight on
        await advance_grammar(chat_id, context, s)
        return

    s["grammar_awaiting_voice"] = True

    tts_path = await make_tts(speaking_q)

    kb = [[InlineKeyboardButton("⏭ Siguiente pregunta", callback_data="grammar_next")]]
    markup = InlineKeyboardMarkup(kb)

    caption = (
        f"🎤 *Ahora habla:*\n\n_{speaking_q}_\n\n"
        f"Graba una nota de voz con tu respuesta.\n"
        f"_(Usa la estructura gramatical que acabas de practicar si puedes)_"
    )

    if tts_path:
        with open(tts_path, "rb") as f:
            await context.bot.send_voice(
                chat_id=chat_id, voice=f,
                caption=caption, parse_mode="Markdown",
                reply_markup=markup
            )
        os.remove(tts_path)
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=caption,
            parse_mode="Markdown", reply_markup=markup
        )

async def handle_grammar_voice_answer(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE,
    s: dict, user_id: int, voice_path: str
):
    """Transcribe voice answer and give feedback on grammar structure use."""
    q          = s["grammar_current"]
    user_text  = await transcribe_voice(voice_path)

    if not user_text:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No pude escuchar bien. ¡Vamos a la siguiente pregunta! 🎤"
        )
        await advance_grammar(chat_id, context, s)
        return

    target_structure = q.get("tense_label", "la estructura gramatical")
    full_sent        = q["full_sentence"]

    feedback = await deepseek(
        f"The student is practicing Spanish grammar (B2 level). "
        f"Target structure: {target_structure}.\n"
        f"Example sentence for reference: '{full_sent}'\n"
        f"Speaking question asked: '{q.get('speaking_question', '')}'\n"
        f"Student's spoken answer (transcribed): '{user_text}'\n\n"
        f"Instructions:\n"
        f"1. Check if the student correctly used the target structure ({target_structure}) in their answer.\n"
        f"2. If they used it correctly, say so briefly and positively. One sentence max.\n"
        f"3. If they made a key error in the target structure, give a one-sentence correction ONLY. "
        f"   Ignore minor errors in other parts of the answer.\n"
        f"4. If no grammar errors at all, say NOTHING about grammar — just output: OK\n"
        f"Reply in Spanish. Max 2 sentences total. Be concise and encouraging.",
        system="You are a concise, encouraging Spanish grammar tutor. Give minimal, targeted feedback."
    )

    msg = f"🗣 _{user_text}_\n\n"
    if feedback.strip().upper() != "OK":
        msg += feedback
    else:
        msg += "✅ ¡Muy bien!"

    await context.bot.send_message(
        chat_id=chat_id, text=msg, parse_mode="Markdown"
    )

    await advance_grammar(chat_id, context, s)

async def advance_grammar(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    """Move to the next grammar question."""
    s["grammar_awaiting_voice"] = False
    s["grammar_awaiting_text"]  = False
    s["grammar_index"] += 1

    await asyncio.sleep(1)

    if s["grammar_index"] < len(s["grammar_items"]):
        await send_grammar_question(chat_id, context, s)
    else:
        await grammar_block_complete(chat_id, context, s)

async def grammar_block_complete(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    """Shown when all questions in a grammar block are done."""
    pack_key  = s["grammar_pack_key"]
    label     = GRAMMAR_FILES.get(pack_key, {}).get("label", "este bloque")
    total     = len(s["grammar_items"])

    s["grammar_current"]       = None
    s["grammar_awaiting_text"] = False
    s["grammar_awaiting_voice"]= False

    kb = [
        [InlineKeyboardButton("🔄 Repetir este bloque", callback_data=f"grammar_repeat_{pack_key}")],
        [InlineKeyboardButton("📚 Elegir otro bloque",  callback_data="grammar_menu")],
        [InlineKeyboardButton("🏠 Menú principal",       callback_data="main_menu")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎉 *¡Bloque completado!*\n\n"
            f"Has terminado todas las {total} preguntas de:\n"
            f"_{label}_\n\n"
            f"Tu progreso está guardado en Google Sheets. ¿Qué quieres hacer?"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ─── VOCAB FLOW ───────────────────────────────────────────────────────────────

async def send_word(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    word_data = s["words"][s["index"]]
    word      = word_data["word"]
    english   = word_data.get("english", "")
    s["current_word"]    = word
    s["current_english"] = english
    s["help_level"]      = 0

    explanation = await deepseek(
        f"Explain the Spanish word/phrase '{word}' (English: {english}) to an A2 learner. "
        f"Use simple Spanish. One-line definition + one short example sentence. Max 60 words.",
        system="You are a friendly Spanish tutor for A2 learners."
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📚 *{word}* — _{english}_\n\n{explanation}",
        parse_mode="Markdown"
    )

    asyncio.create_task(send_word_image(chat_id, context, word_data))

    question = await deepseek(
        f"Write ONE short A2-level Spanish conversation question about '{word}'. "
        f"Personal, easy to answer in 1-2 sentences. Question only, no extra text.",
        system="You are a friendly Spanish tutor for A2 learners."
    )
    s["current_question"] = question

    audio_path = await make_tts(question)

    kb = [
        [InlineKeyboardButton("🆘 No entiendo", callback_data="help"),
         InlineKeyboardButton("🔊 Repetir",     callback_data="repeat")],
        [InlineKeyboardButton("⏭ Otra palabra", callback_data="skip")]
    ]
    markup = InlineKeyboardMarkup(kb)

    if audio_path:
        with open(audio_path, "rb") as f:
            await context.bot.send_voice(
                chat_id=chat_id, voice=f,
                caption=f"🎤 {question}\n\nGraba tu respuesta con una nota de voz",
                parse_mode="Markdown", reply_markup=markup
            )
        os.remove(audio_path)
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎤 {question}\n\nGraba tu respuesta con una nota de voz",
            parse_mode="Markdown", reply_markup=markup
        )

# ─── MAIN MENU ────────────────────────────────────────────────────────────────

async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📚 A — Vocabulario + conversación", callback_data="menu_vocab")],
        [InlineKeyboardButton("📝 B — Gramática + conversación",   callback_data="menu_grammar")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "¡Hola! ¿Qué quieres practicar hoy?\n\n"
            "*A* — Vocabulario temático y práctica oral\n"
            "*B* — Ejercicios de gramática y práctica oral"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_grammar_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Show grammar block selection with progress info."""
    progress = load_grammar_progress(user_id)
    lines = []
    for key, info in GRAMMAR_FILES.items():
        items  = load_grammar_pack(key)
        done   = sum(1 for it in items if it["id"] in progress)
        total  = len(items)
        status = f"({done}/{total} hechas)"
        lines.append(f"*{key}.* {info['label']} {status}")

    text = "📝 *Elige un bloque de gramática:*\n\n" + "\n".join(lines)

    kb = [
        [InlineKeyboardButton(f"{k}. {v['label']}", callback_data=f"grammar_start_{k}")]
        for k, v in GRAMMAR_FILES.items()
    ]
    kb.append([InlineKeyboardButton("🏠 Menú principal", callback_data="main_menu")])

    await context.bot.send_message(
        chat_id=chat_id, text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def start_grammar_block(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE,
    s: dict, user_id: int, pack_key: str, repeat: bool = False
):
    items = load_grammar_pack(pack_key)
    if not items:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ No se pudo cargar este bloque. Verifica que el archivo JSON está en el servidor."
        )
        return

    if not repeat:
        # Skip already-done questions
        progress  = load_grammar_progress(user_id)
        remaining = [it for it in items if it["id"] not in progress]
        if not remaining:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 ¡Ya has completado todas las preguntas de este bloque!\n"
                    "Usa *Repetir este bloque* para empezar de nuevo."
                ),
                parse_mode="Markdown"
            )
            return
        items = remaining

    random.shuffle(items)

    s["mode"]               = "grammar"
    s["grammar_pack_key"]   = pack_key
    s["grammar_items"]      = items
    s["grammar_index"]      = 0
    s["grammar_current"]    = None
    s["grammar_awaiting_text"]  = False
    s["grammar_awaiting_voice"] = False

    label = GRAMMAR_FILES[pack_key]["label"]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📝 *Bloque: {label}*\n"
            f"{len(items)} preguntas — ¡vamos!"
        ),
        parse_mode="Markdown"
    )
    await send_grammar_question(chat_id, context, s)

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update.effective_chat.id, context)

async def cmd_temas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    packs   = all_packs()
    builtin = [k for k in packs if k in builtin_packs]
    custom  = [k for k in packs if k not in builtin_packs]
    msg = "Temas de vocabulario:\n\n"
    msg += "Integrados:\n" + "\n".join(
        f"  • {k} ({len(packs[k])} palabras)" for k in builtin
    )
    if custom:
        msg += "\n\nPersonalizados:\n" + "\n".join(
            f"  • {k} ({len(packs[k])} palabras)" for k in custom
        )
    msg += "\n\nUsa /tema [nombre] para elegir."
    await update.message.reply_text(msg)

async def cmd_tema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Escribe el nombre del tema: /tema comida")
        return
    theme = context.args[0].lower().strip()
    if theme not in all_packs():
        await update.message.reply_text(
            f"Tema '{theme}' no encontrado. Usa /temas para ver los disponibles."
        )
        return
    await start_theme(update, context, theme)

async def cmd_nuevas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_theme(update, context, next_theme(update.effective_user.id))

async def cmd_gramatica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_grammar_menu(
        update.effective_chat.id, context, update.effective_user.id
    )

async def start_theme(update: Update, context: ContextTypes.DEFAULT_TYPE, theme: str):
    user_id = update.effective_user.id
    s       = get_session(user_id)
    s["mode"]             = "vocab"
    s["theme"]            = theme
    s["words"]            = all_packs()[theme]
    s["index"]            = 0
    s["current_question"] = None
    await update.message.reply_text(
        f"🗂 Tema: {theme.replace('_', ' ').capitalize()} — {len(s['words'])} palabras. ¡Vamos! 💪"
    )
    await send_word(update.effective_chat.id, context, s)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("Por favor envía un archivo .json")
        return

    await update.message.reply_text("⏳ Procesando tu pack...")
    file     = await doc.get_file()
    tmp_path = f"/tmp/pack_{uuid.uuid4()}.json"
    await file.download_to_drive(tmp_path)

    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Error leyendo el JSON: {e}")
        return

    if not isinstance(data, dict):
        await update.message.reply_text(
            "❌ Formato incorrecto. El JSON debe ser:\n"
            '{\n  "nombre_pack": [\n    {"word": "...", "english": "..."}\n  ]\n}'
        )
        return

    saved  = []
    errors = []

    for pack_name, words in data.items():
        if not isinstance(words, list):
            errors.append(f"'{pack_name}' — debe ser una lista")
            continue
        valid = [w for w in words if isinstance(w, dict) and "word" in w]
        if not valid:
            errors.append(f"'{pack_name}' — no se encontraron palabras válidas")
            continue
        try:
            save_pack_to_sheet(pack_name, valid)
            custom_packs[pack_name] = valid
            saved.append(f"'{pack_name}' ({len(valid)} palabras)")
        except Exception as e:
            errors.append(f"'{pack_name}' — error: {e}")

    msg = ""
    if saved:
        msg += "✅ Packs guardados:\n" + "\n".join(f"  • {s}" for s in saved)
    if errors:
        msg += "\n\n⚠️ Errores:\n" + "\n".join(f"  • {e}" for e in errors)
    if not saved and not errors:
        msg = "❌ No se encontraron packs válidos."
    msg += "\n\nUsa /temas para ver todos los temas."
    await update.message.reply_text(msg)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages — used for grammar written answers."""
    user_id = update.effective_user.id
    s       = get_session(user_id)

    if s.get("mode") == "grammar" and s.get("grammar_awaiting_text"):
        await handle_grammar_text_answer(
            update.effective_chat.id, context, s,
            user_id, update.message.text.strip()
        )
    # Ignore text in vocab mode (bot expects voice there)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s       = get_session(user_id)
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    voice_file = await update.effective_message.voice.get_file()
    voice_path = f"/tmp/voice_{uuid.uuid4()}.ogg"
    await voice_file.download_to_drive(voice_path)

    # ── Grammar speaking answer ──────────────────────────────────────────────
    if s.get("mode") == "grammar" and s.get("grammar_awaiting_voice"):
        await handle_grammar_voice_answer(chat_id, context, s, user_id, voice_path)
        os.remove(voice_path)
        return

    # ── Vocab speaking answer ────────────────────────────────────────────────
    if not s.get("current_question"):
        os.remove(voice_path)
        await update.message.reply_text("Usa /start para comenzar una sesión.")
        return

    user_text = await transcribe_voice(voice_path)
    os.remove(voice_path)

    if not user_text:
        await update.message.reply_text("No pude entender el audio. ¡Inténtalo de nuevo! 🎤")
        return

    feedback = await deepseek(
        f"You are an encouraging A2 Spanish tutor.\n"
        f"Question asked: {s['current_question']}\n"
        f"Student said: {user_text}\n\n"
        f"Reply in Spanish. Max 3 sentences:\n"
        f"1. Short encouraging comment\n"
        f"2. Correct their sentence naturally if needed\n"
        f"3. Give the corrected/model version prefixed with ✅",
        system="You are a friendly Spanish tutor for A2 learners."
    )

    corrected = None
    for line in feedback.split("\n"):
        if "✅" in line:
            corrected = line.replace("✅", "").strip()
            break
    audio_path = await make_tts(corrected or s["current_question"])

    kb = [
        [InlineKeyboardButton("✅ Guardar palabra", callback_data="save"),
         InlineKeyboardButton("⏭ Siguiente",        callback_data="next")]
    ]
    markup = InlineKeyboardMarkup(kb)

    if audio_path:
        with open(audio_path, "rb") as f:
            await context.bot.send_voice(
                chat_id=chat_id, voice=f,
                caption=f"_{user_text}_\n\n{feedback}",
                parse_mode="Markdown", reply_markup=markup
            )
        os.remove(audio_path)
    else:
        await update.message.reply_text(
            f"_{user_text}_\n\n{feedback}",
            parse_mode="Markdown", reply_markup=markup
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    s       = get_session(user_id)
    chat_id = update.effective_chat.id
    data    = query.data

    # ── Main menu ────────────────────────────────────────────────────────────
    if data == "main_menu":
        await send_main_menu(chat_id, context)
        return

    if data == "menu_vocab":
        # Start next vocab theme
        theme = next_theme(user_id)
        s["mode"]             = "vocab"
        s["theme"]            = theme
        s["words"]            = all_packs()[theme]
        s["index"]            = 0
        s["current_question"] = None
        packs = all_packs()
        pack_list = "\n".join(f"  • {k}" for k in packs)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🗂 Empezando con: *{theme.replace('_', ' ').capitalize()}*\n\n"
                f"Puedes también elegir un tema con /temas"
            ),
            parse_mode="Markdown"
        )
        await send_word(chat_id, context, s)
        return

    if data == "menu_grammar":
        await send_grammar_menu(chat_id, context, user_id)
        return

    # ── Grammar block selection ───────────────────────────────────────────────
    if data.startswith("grammar_start_"):
        pack_key = data.replace("grammar_start_", "")
        await start_grammar_block(chat_id, context, s, user_id, pack_key, repeat=False)
        return

    if data.startswith("grammar_repeat_"):
        pack_key = data.replace("grammar_repeat_", "")
        await start_grammar_block(chat_id, context, s, user_id, pack_key, repeat=True)
        return

    if data == "grammar_menu":
        await send_grammar_menu(chat_id, context, user_id)
        return

    if data == "grammar_next":
        # User skipped voice speaking
        s["grammar_awaiting_voice"] = False
        await advance_grammar(chat_id, context, s)
        return

    # ── Vocab callbacks ───────────────────────────────────────────────────────
    if data == "save":
        save_vocab_word(
            word=s["current_word"], english=s["current_english"],
            theme=s["theme"],       example=s["current_question"],
            user=update.effective_user
        )
        try:
            await query.edit_message_caption(
                caption=f"✅ {s['current_word']} guardada en tu lista.",
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ {s['current_word']} guardada en tu lista."
            )
        await advance_vocab(chat_id, context, s)

    elif data in ("next", "skip"):
        await advance_vocab(chat_id, context, s)

    elif data == "repeat":
        if s.get("current_question"):
            audio_path = await make_tts(s["current_question"])
            if audio_path:
                with open(audio_path, "rb") as f:
                    await context.bot.send_voice(chat_id=chat_id, voice=f)
                os.remove(audio_path)

    elif data == "help":
        s["help_level"] = s.get("help_level", 0) + 1
        await send_help(chat_id, context, s)

async def send_help(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    level = s["help_level"]
    q     = s["current_question"]

    if level == 1:
        simplified = await deepseek(
            f"Simplify this A2 Spanish question into shorter, easier words. "
            f"Add a vocabulary hint in brackets. Question: '{q}'. One sentence only.",
            system="You are a friendly Spanish tutor for A2 learners."
        )
        text = f"🆘 Versión más fácil:\n\n{simplified}"
    elif level == 2:
        translated = await deepseek(
            f"Translate this Spanish question to English, then give a simple model answer in Spanish.\n"
            f"Question: '{q}'\nFormat:\nEN: [translation]\nModelo: [simple Spanish answer]",
            system="You are a friendly Spanish tutor for A2 learners."
        )
        text = f"🇬🇧 Traducción + respuesta modelo:\n\n{translated}"
    else:
        model = await deepseek(
            f"Give a very simple 1-sentence model answer to this question for an A2 learner: '{q}'. "
            f"Spanish only.",
            system="You are a friendly Spanish tutor for A2 learners."
        )
        text = f"💡 Ejemplo completo:\n\n{model}\n\nIntenta repetir esta frase 🎤"

    kb = [
        [InlineKeyboardButton("🔊 Escuchar de nuevo", callback_data="repeat"),
         InlineKeyboardButton("🆘 Más ayuda",         callback_data="help")],
        [InlineKeyboardButton("⏭ Siguiente palabra",  callback_data="skip")]
    ]
    await context.bot.send_message(
        chat_id=chat_id, text=text,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def advance_vocab(chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict):
    s["index"] += 1
    if s["index"] < len(s["words"]):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Palabra {s['index'] + 1} de {len(s['words'])}..."
        )
        await send_word(chat_id, context, s)
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎉 ¡Sesión completada! Muy bien.\n\nUsa /nuevas para el siguiente tema o /temas para elegir."
        )
        s["current_question"] = None

# ─── STARTUP ──────────────────────────────────────────────────────────────────

async def on_startup(app):
    global custom_packs
    logger.info("Loading custom packs from Google Sheets...")
    custom_packs = load_custom_packs_from_sheet()
    logger.info(f"Ready. All packs: {list(all_packs().keys())}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_start))
    app.add_handler(CommandHandler("nuevas",     cmd_nuevas))
    app.add_handler(CommandHandler("temas",      cmd_temas))
    app.add_handler(CommandHandler("tema",       cmd_tema))
    app.add_handler(CommandHandler("gramatica",  cmd_gramatica))
    app.add_handler(MessageHandler(filters.Document.MimeType("application/json"), handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("HablaRápido bot starting...")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
