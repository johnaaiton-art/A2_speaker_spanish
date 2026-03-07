"""
generate_images.py
==================
Run this ONCE on the VM to pre-generate images for all built-in word packs.
Images are saved to ./images/ and word_packs.json is updated with filenames.

Usage:
    python generate_images.py

Run again any time you add new words to BUILTIN_PACKS (it skips already-generated images).
"""

import os
import json
import time
import uuid
import base64
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
IMAGES_DIR       = "images"
PACKS_FILE       = "word_packs.json"

# ─── BUILT-IN PACKS (source of truth) ────────────────────────────────────────
# These are duplicated here so this script is self-contained.
# bot.py loads from word_packs.json which this script generates.

BUILTIN_PACKS = {
    "comida": [
        {"word": "el desayuno",      "english": "breakfast"},
        {"word": "la cena",          "english": "dinner"},
        {"word": "rico",             "english": "delicious / tasty"},
        {"word": "tener hambre",     "english": "to be hungry"},
        {"word": "pedir",            "english": "to order (food)"},
    ],
    "ciudad": [
        {"word": "la calle",         "english": "street"},
        {"word": "cerca de",         "english": "near / close to"},
        {"word": "a la derecha",     "english": "on the right"},
        {"word": "el supermercado",  "english": "supermarket"},
        {"word": "cruzar",           "english": "to cross"},
    ],
    "tiempo_libre": [
        {"word": "salir con amigos", "english": "to go out with friends"},
        {"word": "ver una película", "english": "to watch a film"},
        {"word": "dar un paseo",     "english": "to go for a walk"},
        {"word": "el fin de semana", "english": "weekend"},
        {"word": "divertirse",       "english": "to have fun"},
    ],
    "familia": [
        {"word": "el hermano",       "english": "brother"},
        {"word": "los abuelos",      "english": "grandparents"},
        {"word": "vivir con",        "english": "to live with"},
        {"word": "mayor",            "english": "older / eldest"},
        {"word": "estar casado",     "english": "to be married"},
    ],
    "emociones": [
        {"word": "estar contento",   "english": "to be happy"},
        {"word": "estar cansado",    "english": "to be tired"},
        {"word": "tener miedo",      "english": "to be afraid"},
        {"word": "estar enfadado",   "english": "to be angry"},
        {"word": "sentirse bien",    "english": "to feel well"},
    ],
    "trabajo": [
        {"word": "trabajar",         "english": "to work"},
        {"word": "el horario",       "english": "timetable / schedule"},
        {"word": "el jefe",          "english": "boss"},
        {"word": "estudiar",         "english": "to study"},
        {"word": "la reunión",       "english": "meeting"},
    ],
    "viajes": [
        {"word": "el billete",       "english": "ticket"},
        {"word": "el aeropuerto",    "english": "airport"},
        {"word": "hacer la maleta",  "english": "to pack a suitcase"},
        {"word": "el hotel",         "english": "hotel"},
        {"word": "llegar",           "english": "to arrive"},
    ],
    "casa": [
        {"word": "el dormitorio",    "english": "bedroom"},
        {"word": "limpiar",          "english": "to clean"},
        {"word": "vivir en",         "english": "to live in"},
        {"word": "el piso",          "english": "flat / apartment"},
        {"word": "mudarse",          "english": "to move house"},
    ],
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def word_to_filename(word: str) -> str:
    """Convert a Spanish word/phrase to a safe filename."""
    safe = word.lower()
    safe = safe.replace(" ", "_")
    # Remove accents and special chars
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ñ": "n", "ü": "u", "¿": "", "¡": "",
    }
    for orig, repl in replacements.items():
        safe = safe.replace(orig, repl)
    safe = "".join(c for c in safe if c.isalnum() or c == "_")
    return f"{safe}.png"

def build_prompt(word: str, english: str) -> str:
    return (
        f"Simple colorful cartoon illustration for Spanish vocabulary learning. "
        f"Word: '{word}' ({english}). "
        f"Clean white background, friendly and educational style, suitable for language learners."
    )

def generate_image_yandex(prompt: str) -> bytes | None:
    """Synchronous Yandex Art generation (fine for a one-off script)."""
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
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

    # Step 1: Submit
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            logger.error(f"Submit error {resp.status_code}: {resp.text}")
            return None
        operation_id = resp.json()["id"]
        logger.info(f"  Operation ID: {operation_id}")
    except Exception as e:
        logger.error(f"  Submit exception: {e}")
        return None

    # Step 2: Poll
    result_url = f"https://llm.api.cloud.yandex.net:443/operations/{operation_id}"
    for attempt in range(36):          # up to 3 minutes (36 x 5s)
        time.sleep(5)
        try:
            r = requests.get(result_url, headers=headers, timeout=30)
            data = r.json()
        except Exception as e:
            logger.warning(f"  Poll attempt {attempt+1} failed: {e}")
            continue

        if data.get("done"):
            if "error" in data:
                logger.error(f"  Generation error: {data['error']}")
                return None
            image_b64 = data.get("response", {}).get("image")
            if image_b64:
                return base64.b64decode(image_b64)
            else:
                logger.error("  Done but no image in response")
                return None

        if attempt % 6 == 5:
            logger.info(f"  Still waiting... ({(attempt+1)*5}s)")

    logger.error("  Timed out after 3 minutes")
    return None

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(IMAGES_DIR, exist_ok=True)

    # Load existing word_packs.json if it exists (to preserve any image refs already set)
    if os.path.exists(PACKS_FILE):
        with open(PACKS_FILE, "r", encoding="utf-8") as f:
            packs = json.load(f)
        logger.info(f"Loaded existing {PACKS_FILE}")
    else:
        # First run — copy from BUILTIN_PACKS
        packs = {
            theme: [dict(w) for w in words]
            for theme, words in BUILTIN_PACKS.items()
        }
        logger.info("No word_packs.json found — creating from BUILTIN_PACKS")

    total   = sum(len(v) for v in packs.values())
    done    = 0
    skipped = 0
    failed  = 0

    for theme, words in packs.items():
        logger.info(f"\n{'='*50}")
        logger.info(f"Theme: {theme} ({len(words)} words)")
        logger.info(f"{'='*50}")

        for word_data in words:
            word    = word_data["word"]
            english = word_data.get("english", "")
            fname   = word_to_filename(word)
            fpath   = os.path.join(IMAGES_DIR, fname)

            # Skip if image already exists
            if os.path.exists(fpath):
                logger.info(f"  [SKIP] '{word}' → {fname} (already exists)")
                word_data["image"] = fname
                skipped += 1
                done += 1
                continue

            logger.info(f"  [GEN]  '{word}' ({english}) → {fname}")
            prompt    = build_prompt(word, english)
            img_bytes = generate_image_yandex(prompt)

            if img_bytes:
                with open(fpath, "wb") as f:
                    f.write(img_bytes)
                word_data["image"] = fname
                logger.info(f"  [OK]   Saved {fname} ({len(img_bytes)//1024}KB)")
                done += 1
            else:
                logger.error(f"  [FAIL] Could not generate image for '{word}'")
                word_data.pop("image", None)   # remove stale ref if any
                failed += 1

            # Small pause between requests to be polite to the API
            time.sleep(2)

    # Save updated word_packs.json
    with open(PACKS_FILE, "w", encoding="utf-8") as f:
        json.dump(packs, f, ensure_ascii=False, indent=2)

    logger.info(f"\n{'='*50}")
    logger.info(f"DONE. {done}/{total} words processed.")
    logger.info(f"  Skipped (already existed): {skipped}")
    logger.info(f"  Failed: {failed}")
    logger.info(f"Updated {PACKS_FILE} with image references.")
    if failed:
        logger.info(f"Re-run this script to retry failed images.")

if __name__ == "__main__":
    main()
