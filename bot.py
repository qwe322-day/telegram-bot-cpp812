import asyncio, os, re, time, json, logging, whisper
from collections import deque
from functools import wraps
from pydub import AudioSegment
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from lyricsgenius import Genius
from tinytag import TinyTag
from deep_translator import GoogleTranslator
import moviepy.editor as mp
import yt_dlp, aiohttp
from bs4 import BeautifulSoup
from groq import Groq

# ── Конфіг ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN        = os.getenv("BOT_TOKEN",    "8616611568:AAHOJ4LiO8I5FezeGh1xhmG_3A69QbZUD64")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN", "9tOOVYXVCesY7oLg3qOgzQI0zwXhS-pFCIK_qucYYD69GYwH8v2KU9Z_ywg93pJn")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "999666214"))
GROQ_KEY     = os.getenv("GROQ_API_KEY", "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")
ANTISPAM_DELAY, MAX_QUEUE = 10, 5
FAVORITES_FILE, BANNED_FILE = "favorites.json", "banned.json"

bot         = Bot(token=TOKEN)
dp          = Dispatcher()
genius      = Genius(GENIUS_TOKEN, verbose=False, timeout=15)
groq_client = Groq(api_key=GROQ_KEY)

logger.info("Завантаження Whisper...")
whisper_model = whisper.load_model("small")
logger.info("Whisper готовий")

# ── Стани ────────────────────────────────────────────────────────────────────
class States(StatesGroup):
    support      = State()
    manual_search = State()

# ── Зберігання ───────────────────────────────────────────────────────────────
user_history, user_last_time, processing_now = {}, {}, set()
user_queues, user_settings, msg_store, user_last_search = {}, {}, {}, {}

def load_json(p): return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
def save_json(p, d): json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

favs_db = load_json(FAVORITES_FILE)
ban_db  = load_json(BANNED_FILE)

# ── Платформи ─────────────────────────────────────────────────────────────────
PLATFORMS = {
    "youtube":    (re.compile(r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+'), "▶️ YouTube"),
    "soundcloud": (re.compile(r'(https?://)?(www\.)?soundcloud\.com/[\w\-]+/[\w\-]+'),            "🔈 SoundCloud"),
    "spotify":    (re.compile(r'(https?://)?open\.spotify\.com/(track|album|playlist)/[\w\-?=&]+'), "💚 Spotify"),
    "apple":      (re.compile(r'(https?://)?music\.apple\.com/[\w\-/]+'),                         "🍎 Apple Music"),
    "tiktok":     (re.compile(r'(https?://)?(www\.|vm\.)?tiktok\.com/[\w\-/@]+'),                 "🎵 TikTok"),
}

def detect_platform(text):
    for name, (rx, _) in PLATFORMS.items():
        m = rx.search(text)
        if m: return name, m.group(0)
    return None, None

# ── Клавіатури ────────────────────────────────────────────────────────────────
def kb(kind):
    rows = {
        "main": [[("Мова","Мова"),("Статистика","Статистика")],[("Улюблені","Улюблені"),("Техпідтримка","Техпідтримка")],[("Очистити історію","Очистити історію"),("Довідка","Довідка")]],
    }
    if kind == "main":
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t,_ in row] for row in rows["main"]], resize_keyboard=True)
    btns = {
        "action":    [[("🌍 Перекласти","smart_translate"),("🎵 Схожі треки","similar_tracks")],[("⭐ Зберегти","save_favorite"),("📄 Зберегти .txt","save_txt")]],
        "not_found": [[("🔍 Шукати вручну","manual_search")],[("🔄 Спробувати ще раз","retry_last")]],
        "voice":     [[("🌍 Перекласти","smart_translate"),("📋 Короткий переказ","summarize_text")],[("📄 Зберегти .txt","save_txt")]],
        "retry":     [[("🔄 Спробувати ще раз","retry_last")]],
        "lang":      [[("Українська","set_lang_uk-UA")],[("English","set_lang_en-US")],[("Авто (Whisper)","set_lang_auto")]],
        "clear_favs":[[("🗑 Очистити всі улюблені","clear_favorites")]],
    }[kind]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=c) for t,c in row] for row in btns])

# ── Форматування тексту ───────────────────────────────────────────────────────
SECTION_RE = re.compile(r'\[(Verse|Chorus|Pre-Chorus|Bridge|Outro|Intro|Hook|Refrain|Куплет|Приспів|Бридж|(?:Verse|Chorus|Part) \d+)[^\]]*\]', re.IGNORECASE)
SECTION_EMOJI = {"verse":"🎤","chorus":"🎶","pre-chorus":"🎵","bridge":"🌉","outro":"🏁","intro":"🎬","hook":"🪝","refrain":"🔁","куплет":"🎤","приспів":"🎶","бридж":"🌉"}

def format_lyrics(text):
    lines = text.strip().split('\n')
    if lines and re.search(r'Lyrics$', lines[0].strip(), re.IGNORECASE): lines = lines[1:]
    lines = [l for l in lines if not re.search(r'^(You might also like|Embed|\d+$)', l.strip())]
    text  = SECTION_RE.sub(lambda m: f"\n{SECTION_EMOJI.get(m.group(1).lower().split()[0],'🎵')} *[{m.group(1).strip()}]*\n", '\n'.join(lines))
    return re.sub(r'\n{3,}', '\n\n', text).strip()

def smart_truncate(text, limit=3800):
    if len(text) <= limit: return text
    lines, result, length = text.split('\n'), [], 0
    for l in lines:
        if length + len(l) + 1 > limit: break
        result.append(l); length += len(l) + 1
    return '\n'.join(result) + '\n\n_(текст скорочено)_'

def store(chat_id, msg_id, artist, title, text):
    if len(msg_store) > 200:
        for k in list(msg_store)[:100]: del msg_store[k]
    msg_store[f"{chat_id}_{msg_id}"] = {"artist": artist, "title": title, "text": text}

def get_store(chat_id, msg_id):
    return msg_store.get(f"{chat_id}_{msg_id}", {})

# ── Хелпери ───────────────────────────────────────────────────────────────────
def _clean(t):
    return re.sub(r'\(.*?\)|\[.*?\]', '', str(t or "")).strip() or "Unknown"

def _clean_genius(t):
    if not t or t == "Unknown": return t
    t = re.sub(r'\(.*?\)|\[.*?\]', '', t)
    for p in [r'\bofficial\b',r'\bvideo\b',r'\baudio\b',r'\blyrics\b',r'\bhd\b',r'\b4k\b',r'\bmv\b',r'\bm/v\b',r'\bfull\b',r'\bpremiere\b',r'\bvisualize[rd]?\b',r'\blyric\b']:
        t = re.sub(p, '', t, flags=re.IGNORECASE)
    return re.sub(r'\s*-\s*(topic|vevo|official)\s*$', '', re.sub(r'\s+', ' ', t), flags=re.IGNORECASE).strip()

async def progress_bar(msg, label, steps=5, delay=1.2):
    for i in range(1, steps + 1):
        n = int(i / steps * 10)
        try: await msg.edit_text(f"🎙 *{label}*\n\n`{'▓'*n}{'░'*(10-n)}` {i*100//steps}%", parse_mode="Markdown")
        except: pass
        await asyncio.sleep(delay)

# ── Антиспам / бан guard ──────────────────────────────────────────────────────
def guard(fn):
    @wraps(fn)
    async def wrapper(message: Message, *args, **kwargs):
        uid = message.from_user.id
        if str(uid) in ban_db:
            return await message.reply("🚫 Вас заблоковано.")
        now = time.time()
        wait = ANTISPAM_DELAY - (now - user_last_time.get(uid, 0))
        if wait > 0:
            return await message.reply(f"⏳ Зачекайте ще *{int(wait)} сек.*", parse_mode="Markdown")
        user_last_time[uid] = now
        user_history[uid]   = user_history.get(uid, 0) + 1
        return await fn(message, *args, **kwargs)
    return wrapper

# ── Genius пошук ──────────────────────────────────────────────────────────────
async def genius_search(title_clean, artist_clean, title_raw="", artist_raw=""):
    def try_(t, a=None):
        try: return genius.search_song(t, a) if a else genius.search_song(t)
        except Exception as e: logger.warning(f"Genius [{t!r},{a!r}]: {e}")

    for t, a in [
        (title_clean, artist_clean if artist_clean != "Unknown" else None),
        (title_clean, None),
        (title_raw,   None) if title_raw != title_clean else (None, None),
    ]:
        if t:
            song = try_(t, a)
            if song: return song

    if " - " in title_raw:
        da, dt = [_clean_genius(p.strip()) for p in title_raw.split(" - ", 1)]
        for t, a in [(dt, da), (dt, None)]:
            song = try_(t, a)
            if song: return song

    words = title_clean.split()
    if len(words) > 4:
        short = " ".join(words[:4])
        for t, a in [(short, artist_clean if artist_clean != "Unknown" else None), (short, None)]:
            song = try_(t, a)
            if song: return song

    no_feat = re.sub(r'\s*(ft\.?|feat\.?|with|&)\s+[\w\s]+$', '', title_clean, flags=re.IGNORECASE).strip()
    if no_feat and no_feat != title_clean:
        for a in [artist_clean if artist_clean != "Unknown" else None, None]:
            song = try_(no_feat, a)
            if song: return song

    no_topic = re.sub(r'\s*-\s*topic\s*$', '', artist_raw, flags=re.IGNORECASE).strip()
    if no_topic and no_topic != artist_clean:
        song = try_(title_clean, no_topic)
        if song: return song

    return None

# ── Метадані ──────────────────────────────────────────────────────────────────
def get_meta_ytdlp(url):
    with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True,'skip_download':True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return {k: info.get(k,'') for k in ('title','uploader','artist','track','creator')}

async def get_spotify_meta(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://open.spotify.com/oembed?url={url}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                t = (await r.json()).get("title","Unknown")
                p = t.split(" - ", 1)
                return {"title": p[0].strip(), "artist": p[1].strip()} if len(p)==2 else {"title":t,"artist":"Unknown"}
    return {"title":"Unknown","artist":"Unknown"}

async def get_apple_meta(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            soup = BeautifulSoup(await r.text(), "html.parser")
    og = lambda p: (soup.find("meta", property=p) or {}).get("content","Unknown")
    return {"title": og("og:title"), "artist": og("og:description").split("·")[0].strip()}

# ── Відправка результату Genius ───────────────────────────────────────────────
async def send_lyrics(status_msg, orig_msg, artist, title):
    song = await genius_search(_clean_genius(title), _clean_genius(artist), title, artist)
    if song:
        await status_msg.edit_text(
            f"📝 *{artist} — {title}:*\n\n{format_lyrics(smart_truncate(song.lyrics))}",
            reply_markup=kb("action"), parse_mode="Markdown"
        )
        store(orig_msg.chat.id, status_msg.message_id, artist, title, song.lyrics)
    else:
        user_last_search[orig_msg.from_user.id] = {"msg_id": status_msg.message_id, "chat_id": orig_msg.chat.id}
        await status_msg.edit_text(
            f"🤷 Текст для *{artist} — {title}* не знайдено на Genius.\n\nНатисни *Шукати вручну*.",
            reply_markup=kb("not_found"), parse_mode="Markdown"
        )

# ── Обробка URL ───────────────────────────────────────────────────────────────
async def _process_url(message, platform, url):
    label = PLATFORMS[platform][1]
    s     = await message.reply(f"⏬ *Обробляю {label}...*", parse_mode="Markdown")
    try:
        if platform in ("spotify", "apple"):
            await s.edit_text(f"🔍 *{label}: отримую інформацію...*", parse_mode="Markdown")
            meta   = await (get_spotify_meta(url) if platform=="spotify" else get_apple_meta(url))
        else:
            await s.edit_text(f"🔍 *Отримую метадані з {label}...*", parse_mode="Markdown")
            raw  = await asyncio.get_event_loop().run_in_executor(None, lambda: get_meta_ytdlp(url))
            t    = raw.get("track") or raw.get("title") or "Unknown"
            a    = raw.get("creator") or raw.get("artist") or raw.get("uploader") or "Unknown"
            meta = {"title": _clean(t), "artist": _clean(a)}

        await s.edit_text(f"🎵 *{meta['artist']} — {meta['title']}*\n\n🔍 Шукаю на Genius...", parse_mode="Markdown")
        await send_lyrics(s, message, meta["artist"], meta["title"])
    except Exception as e:
        logger.exception("URL error")
        await s.edit_text(f"❌ Помилка: {e}", reply_markup=kb("retry"))

# ── Обробка медіафайлів ───────────────────────────────────────────────────────
async def _process_media(message):
    s = await message.reply("⚙️ *Обробка...*", parse_mode="Markdown")
    tmp_wav, extra = f"tmp_{message.message_id}.wav", None
    try:
        if message.audio:
            extra = f"track_{message.audio.file_id}.mp3"
            await bot.download_file((await bot.get_file(message.audio.file_id)).file_path, extra)
            tag = TinyTag.get(extra)
            artist, title = _clean(tag.artist or message.audio.performer), _clean(tag.title or message.audio.title)
            await s.edit_text(f"🔍 *Шукаю текст для {artist} — {title}...*", parse_mode="Markdown")
            await send_lyrics(s, message, artist, title)
            return

        if message.video:
            extra = f"v_{message.message_id}.mp4"
            await bot.download_file((await bot.get_file(message.video.file_id)).file_path, extra)
            clip = mp.VideoFileClip(extra); clip.audio.write_audiofile(tmp_wav, verbose=False, logger=None); clip.close()
        elif message.voice:
            extra = f"v_{message.message_id}.ogg"
            await bot.download_file((await bot.get_file(message.voice.file_id)).file_path, extra)
            AudioSegment.from_ogg(extra).export(tmp_wav, format="wav")
        else:
            return await s.edit_text("❌ Непідтримуваний тип файлу.")

        lang = user_settings.get(message.from_user.id, "auto")
        await s.edit_text("🎙 *Запускаю Whisper...*", parse_mode="Markdown")
        pt = asyncio.create_task(progress_bar(s, "Розпізнавання мовлення (Whisper)", steps=8, delay=1.5))
        try:
            language = None if lang == "auto" else lang.split('-')[0]
            result   = await asyncio.get_event_loop().run_in_executor(
                None, lambda: whisper_model.transcribe(tmp_wav, language=language, fp16=False)
            )
            text, detected = result.get("text","").strip(), result.get("language","?")
            if not text: raise ValueError("Whisper не розпізнав жодного слова")
        finally:
            pt.cancel(); await asyncio.sleep(0.1)

        lang_info = f"виявлено: {detected}" if lang == "auto" else lang
        await s.edit_text(f"📝 *Результат Whisper ({lang_info}):*\n\n{text}", reply_markup=kb("voice"), parse_mode="Markdown")
        store(message.chat.id, s.message_id, "Unknown", "Unknown", text)

    except ValueError as e: await s.edit_text(f"🤷 {e}", reply_markup=kb("retry"))
    except Exception as e:  logger.exception("Media error"); await s.edit_text(f"❌ Помилка: {e}", reply_markup=kb("retry"))
    finally:
        for f in [tmp_wav, extra]:
            if f and os.path.exists(f):
                try: os.remove(f)
                except: pass

# ── Черга ─────────────────────────────────────────────────────────────────────
async def run_task(uid, message, platform, url):
    processing_now.add(uid)
    try:
        if platform: await _process_url(message, platform, url)
        else:        await _process_media(message)
    finally:
        processing_now.discard(uid)
        q = user_queues.get(uid)
        if q:
            try:
                nm, np, nu = q.popleft()
                await nm.reply("▶️ Ваш файл з черги обробляється...")
                asyncio.create_task(run_task(uid, nm, np, nu))
            except IndexError: pass

async def enqueue(message, platform=None, url=None):
    uid = message.from_user.id
    if uid in processing_now:
        q = user_queues.setdefault(uid, deque())
        if len(q) >= MAX_QUEUE: return await message.reply("🚫 Черга переповнена.")
        q.append((message, platform, url))
        return await message.reply(f"⏳ Додано в чергу. Позиція: *{len(q)}*", parse_mode="Markdown")
    asyncio.create_task(run_task(uid, message, platform, url))

# ── Хендлери повідомлень ──────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Вітаю, {message.from_user.first_name}!\n\nНадішли голосове, відео, аудіо або посилання:\n▶️ YouTube  🔈 SoundCloud  💚 Spotify  🍎 Apple Music  🎵 TikTok",
        reply_markup=kb("main")
    )

@dp.message(F.text.func(lambda t: detect_platform(t)[0] is not None))
@guard
async def handle_url(message: Message):
    platform, url = detect_platform(message.text)
    await enqueue(message, platform, url)

@dp.message(F.voice | F.video | F.audio)
@guard
async def handle_media(message: Message):
    await enqueue(message)

@dp.message(F.text == "Мова")
async def cmd_lang(message: Message):
    cur = user_settings.get(message.from_user.id, "auto")
    await message.answer(f"🌐 Поточна мова: *{cur}*\nОберіть нову:", reply_markup=kb("lang"), parse_mode="Markdown")

@dp.message(F.text == "Статистика")
async def cmd_stats(message: Message):
    uid = message.from_user.id
    await message.answer(
        f"📈 *Ваша статистика:*\n\n🎵 Оброблено: *{user_history.get(uid,0)}*\n⭐ Збережено: *{len(favs_db.get(str(uid),[]))}*\n⏳ В черзі: *{len(user_queues.get(uid,[]))}*",
        parse_mode="Markdown"
    )

@dp.message(F.text == "Улюблені")
async def cmd_favs(message: Message):
    uid, items = str(message.from_user.id), favs_db.get(str(message.from_user.id), [])
    if not items: return await message.answer("📭 Немає збережених пісень. Натисни ⭐ під результатом щоб зберегти!")
    lines = ["⭐ *Ваші збережені пісні:*\n"] + [f"{i}. *{x['artist']}* — {x['title']}" for i,x in enumerate(items,1)] + [f"\nВсього: {len(items)}"]
    await message.answer('\n'.join(lines), parse_mode="Markdown", reply_markup=kb("clear_favs"))

@dp.message(F.text == "Техпідтримка")
async def cmd_support(message: Message, state: FSMContext):
    await message.answer("📝 Напишіть ваше повідомлення для адміністратора:")
    await state.set_state(States.support)

@dp.message(States.support)
async def support_send(message: Message, state: FSMContext):
    u = message.from_user
    await bot.send_message(ADMIN_ID, f"📩 Від @{u.username or u.first_name} (`{u.id}`):\n\n{message.text}", parse_mode="Markdown")
    await message.answer("✅ Повідомлення надіслано!")
    await state.clear()

@dp.message(F.text == "Очистити історію")
async def cmd_clear(message: Message):
    user_history[message.from_user.id] = 0
    await message.answer("🧹 Лічильник скинуто!")

@dp.message(F.text == "Довідка")
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Як користуватись:*\n\n🎙 Голосове → текст (Whisper)\n🎵 MP3 → текст пісні (Genius)\n🎬 Відео → мовлення\n\n"
        "🔗 *Посилання:* YouTube / SoundCloud / TikTok / Spotify / Apple → Genius\n\n"
        "🔍 Не знайдено → кнопка *Шукати вручну*\n📋 Переказ — для голосових/відео\n"
        f"🌍 Переклад UA↔EN  ⭐ Улюблені  📄 .txt\n\n🛡 Антиспам: {ANTISPAM_DELAY} сек.",
        parse_mode="Markdown"
    )

# ── Callbacks ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "smart_translate")
async def cb_translate(callback: CallbackQuery):
    text = get_store(callback.message.chat.id, callback.message.message_id).get("text","")
    if not text: return await callback.answer("⚠️ Текст не знайдено", show_alert=True)
    try:
        target = 'en' if re.search('[а-яА-ЯіїєґІЇЄҐ]', text) else 'uk'
        tr = GoogleTranslator(source='auto', target=target).translate(text[:4500])
        await callback.message.answer(f"🌍 *Переклад ({target.upper()}):*\n\n{tr}", parse_mode="Markdown")
        await callback.answer()
    except Exception as e: await callback.answer(f"⚠️ Помилка: {e}", show_alert=True)

@dp.callback_query(F.data == "summarize_text")
async def cb_summarize(callback: CallbackQuery):
    text = get_store(callback.message.chat.id, callback.message.message_id).get("text","")
    if not text: return await callback.answer("⚠️ Текст не знайдено", show_alert=True)
    await callback.answer("📋 Генерую переказ...")
    try:
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: groq_client.chat.completions.create(
            model="llama-3.1-8b-instant", max_tokens=200,
           messages=[{"role":"user","content":f"Зроби дуже короткий переказ (2-3 речення максимум, не більше 100 слів). Відповідай тією ж мовою. Виділи лише головну думку.\n\n{text[:4000]}"}]
        ))
        await callback.message.answer(f"📋 *Короткий переказ:*\n\n{r.choices[0].message.content}", parse_mode="Markdown")
    except Exception as e: logger.exception("Groq"); await callback.answer(f"❌ {e}", show_alert=True)

@dp.callback_query(F.data == "similar_tracks")
async def cb_similar(callback: CallbackQuery):
    data   = get_store(callback.message.chat.id, callback.message.message_id)
    artist = data.get("artist","Unknown")
    if artist == "Unknown": return await callback.answer("⚠️ Не вдалося визначити виконавця", show_alert=True)
    await callback.answer("🔍 Шукаю...")
    try:
        res = genius.search_songs(artist, per_page=6)
        if not res or not res.get("hits"): return await callback.message.answer(f"🤷 Не вдалося знайти треки *{artist}*.", parse_mode="Markdown")
        seen, lines, count = set(), [f"🎵 *Інші треки — {artist}:*\n"], 0
        for hit in res["hits"]:
            t = hit.get("result",{}).get("title","")
            if t and t != data.get("title") and t not in seen:
                seen.add(t); count += 1; lines.append(f"{count}. {t}")
                if count >= 5: break
        lines.append("\n💡 Надішли назву щоб отримати текст!")
        await callback.message.answer('\n'.join(lines), parse_mode="Markdown")
    except Exception as e: await callback.answer(f"❌ {e}", show_alert=True)

@dp.callback_query(F.data == "save_favorite")
async def cb_save_fav(callback: CallbackQuery):
    uid  = str(callback.from_user.id)
    data = get_store(callback.message.chat.id, callback.message.message_id)
    a, t = data.get("artist","Unknown"), data.get("title","Unknown")
    favs_db.setdefault(uid, [])
    if any(x["title"]==t and x["artist"]==a for x in favs_db[uid]):
        return await callback.answer("⭐ Вже збережено!")
    favs_db[uid].append({"title":t,"artist":a,"text":data.get("text","")[:500]})
    save_json(FAVORITES_FILE, favs_db)
    await callback.answer("⭐ Збережено в улюблені!")

@dp.callback_query(F.data == "save_txt")
async def cb_save_txt(callback: CallbackQuery):
    text = get_store(callback.message.chat.id, callback.message.message_id).get("text","")
    if not text: return await callback.answer("⚠️ Текст не знайдено", show_alert=True)
    fname = f"result_{callback.from_user.id}.txt"
    try:
        open(fname,"w",encoding="utf-8").write(text)
        await callback.message.answer_document(types.FSInputFile(fname), caption="📄 Ваш файл готовий!")
        await callback.answer()
    except Exception as e: await callback.answer(f"❌ {e}", show_alert=True)
    finally:
        if os.path.exists(fname): os.remove(fname)

@dp.callback_query(F.data == "retry_last")
async def cb_retry(callback: CallbackQuery):
    await callback.message.answer("🔄 *Надішли посилання або файл ще раз.*", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "clear_favorites")
async def cb_clear_favs(callback: CallbackQuery):
    favs_db[str(callback.from_user.id)] = []
    save_json(FAVORITES_FILE, favs_db)
    await callback.message.edit_text("🗑 Список улюблених очищено.")
    await callback.answer()

@dp.callback_query(F.data.startswith("set_lang_"))
async def cb_set_lang(callback: CallbackQuery):
    lang = callback.data.replace("set_lang_","")
    user_settings[callback.from_user.id] = lang
    label = {"uk-UA":"🇺🇦 Українська","en-US":"🇬🇧 English","auto":"🤖 Авто"}.get(lang, lang)
    await callback.message.edit_text(f"✅ Мову змінено: *{label}*", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "manual_search")
async def cb_manual_search(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🔍 Введи назву пісні та виконавця для пошуку.")
    await state.set_state(States.manual_search)

@dp.message(States.manual_search)
async def manual_search(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    s = await message.reply(f"🔍 *Шукаю: {query}...*", parse_mode="Markdown")
    try:
        song = await asyncio.get_event_loop().run_in_executor(None, lambda: genius.search_song(query))
        if song:
            await s.edit_text(f"📝 *{song.artist} — {song.title}:*\n\n{format_lyrics(smart_truncate(song.lyrics))}", reply_markup=kb("action"), parse_mode="Markdown")
            store(message.chat.id, s.message_id, song.artist, song.title, song.lyrics)
        else:
            await s.edit_text(f"🤷 Текст для *{query}* не знайдено. Спробуй інший запит.", reply_markup=kb("retry"), parse_mode="Markdown")
    except Exception as e: await s.edit_text(f"❌ Помилка: {e}", reply_markup=kb("retry"))

# ── Адмін ─────────────────────────────────────────────────────────────────────
def admin_only(fn):
    @wraps(fn)
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID: return
        return await fn(message, *args, **kwargs)
    return wrapper

@dp.message(Command("ban"))
@admin_only
async def cmd_ban(message: Message):
    args = message.text.split()
    if len(args) < 2: return await message.reply("Використання: /ban USER_ID")
    ban_db[args[1]] = True; save_json(BANNED_FILE, ban_db)
    await message.reply(f"🚫 *{args[1]}* заблоковано.", parse_mode="Markdown")

@dp.message(Command("unban"))
@admin_only
async def cmd_unban(message: Message):
    args = message.text.split()
    if len(args) < 2: return await message.reply("Використання: /unban USER_ID")
    ban_db.pop(args[1], None); save_json(BANNED_FILE, ban_db)
    await message.reply(f"✅ *{args[1]}* розблоковано.", parse_mode="Markdown")

@dp.message(Command("banned"))
@admin_only
async def cmd_banned(message: Message):
    if not ban_db: return await message.reply("Список банів порожній.")
    await message.reply("🚫 *Заблоковані:*\n" + '\n'.join(f"• {u}" for u in ban_db), parse_mode="Markdown")

# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    logger.info("Бот запущено ")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())