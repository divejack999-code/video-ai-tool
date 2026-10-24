# -*- coding: utf-8 -*-
"""
engine.py — محرّك الاستوديو (بدون أي اعتماد على Streamlit)
  • توليد السيناريو (Gemini) مع مشاهد ومدد ومؤثرات لكل مشهد
  • التعليق الصوتي: Edge TTS (مجاني) + Gemini TTS
  • المؤثرات: Freesound CC0 ← Hugging Face Spaces ← توليد محلي بـ numpy
  • الموسيقى: ACE Music (الـ endpoint الصحيح) ← Hugging Face Spaces ← توليد محلي
  • المونتاج: مشهد بمشهد ثم دمج + موسيقى مع ducking
"""
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import time
import wave
import zlib

import imageio_ffmpeg
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 44100
UA = {"User-Agent": "Mozilla/5.0 (AI-Studio-Pro)"}

# ============================================================
# ثوابت
# ============================================================
GEMINI_SCRIPT_FALLBACKS = [
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]
# النماذج التي تقبل مفتاح AI Studio العادي ثم نموذج Vertex كاحتياط
GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts",
    "gemini-3.8-flash-lite-tts",
]
GEMINI_VOICES = ["Fenrir", "Kore", "Puck", "Charon", "Zephyr", "Aoede", "Orus", "Leda", "Algenib", "Sulafat"]

# (أنثى، ذكر)
EDGE_VOICES = {
    "الدارجة المغربية": ("ar-MA-MounaNeural", "ar-MA-JamalNeural"),
    "الأمازيغية": ("ar-MA-MounaNeural", "ar-MA-JamalNeural"),
    "العربية الفصحى": ("ar-SA-ZariyahNeural", "ar-SA-HamedNeural"),
    "المصرية": ("ar-EG-SalmaNeural", "ar-EG-ShakirNeural"),
    "الخليجية": ("ar-KW-NouraNeural", "ar-KW-FahedNeural"),
    "السورية": ("ar-SY-AmanyNeural", "ar-SY-LaithNeural"),
    "اللبنانية": ("ar-LB-LaylaNeural", "ar-LB-RamiNeural"),
    "العراقية": ("ar-IQ-RanaNeural", "ar-IQ-BasselNeural"),
    "الأردنية": ("ar-JO-SanaNeural", "ar-JO-TaimNeural"),
    "الإماراتية": ("ar-AE-FatimaNeural", "ar-AE-HamdanNeural"),
    "السعودية": ("ar-SA-ZariyahNeural", "ar-SA-HamedNeural"),
    "التونسية": ("ar-TN-ReemNeural", "ar-TN-HediNeural"),
    "الجزائرية": ("ar-DZ-AminaNeural", "ar-DZ-IsmaelNeural"),
    "الإنجليزية": ("en-US-JennyNeural", "en-US-GuyNeural"),
    "الفرنسية": ("fr-FR-DeniseNeural", "fr-FR-HenriNeural"),
    "الإسبانية": ("es-ES-ElviraNeural", "es-ES-AlvaroNeural"),
    "الألمانية": ("de-DE-KatjaNeural", "de-DE-ConradNeural"),
    "التركية": ("tr-TR-EmelNeural", "tr-TR-AhmetNeural"),
}

DEFAULT_MUSIC_SPACES = [
    "ACloudCenter/ACE-Music-Generator",
    "fffiloni/ACE-Step-API",
    "WeReCooking/ACE-Step-CPU",
]
DEFAULT_SFX_SPACES = [
    "fffiloni/Stable-Audio-Open-A10",
    "declare-lab/tango2",
    "fffiloni/audioldm2-text2audio-text2music-API",
    "OpenSound/EzAudio",
]

ASPECTS = {
    "9:16 (ريلز / تيك توك)": (9, 16),
    "16:9 (يوتيوب)": (16, 9),
    "1:1 (مربع)": (1, 1),
    "4:5 (إنستغرام)": (4, 5),
    "4:3 (كلاسيكي)": (4, 3),
}


def video_size(aspect_label, short_side):
    a, b = ASPECTS[aspect_label]
    if a < b:
        w, h = short_side, round(short_side * b / a)
    else:
        h, w = short_side, round(short_side * a / b)
    return int(w) // 2 * 2, int(h) // 2 * 2


# ============================================================
# صوتيات أساسية
# ============================================================
def write_wav(path, x, sr=SR):
    x = np.asarray(x, dtype=np.float32)
    x = np.clip(x, -1.0, 1.0)
    ch = 1 if x.ndim == 1 else x.shape[1]
    pcm = (x * 32767).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


def ensure_wav_bytes(data, rate=24000):
    """Gemini (AI Studio) يرجع PCM خام، و Vertex يرجع WAV كاملاً. نتعامل مع الحالتين."""
    if data[:4] == b"RIFF":
        return data
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data)
    return buf.getvalue()


def media_duration(path):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def atempo(src, dst, factor):
    factor = max(0.5, min(2.0, float(factor)))
    r = subprocess.run([FFMPEG, "-y", "-i", src, "-filter:a", f"atempo={factor:.4f}", "-vn", dst],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    return dst


def download_file(url, dest, headers=None, timeout=60):
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest
    with requests.get(url, headers=headers or UA, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)
    os.replace(tmp, dest)
    return dest


# ============================================================
# توليد صوتي محلي (يشتغل دائماً بدون إنترنت ولا مفاتيح)
# ============================================================
def _rng(key):
    return np.random.default_rng(zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF)


def _bandpass(x, lo, hi):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, n=len(x))


def _add(buf, sig, start):
    start = int(start)
    if start >= len(buf) or start < 0:
        return
    end = min(len(buf), start + len(sig))
    buf[start:end] += sig[: end - start]


def _fade(x, fin=0.05, fout=0.2):
    n = len(x)
    a, b = int(fin * SR), int(fout * SR)
    if a > 0:
        x[:a] *= np.linspace(0, 1, min(a, n))
    if b > 0:
        x[-b:] *= np.linspace(1, 0, min(b, n))
    return x


def _norm(x, peak=0.8):
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def synth_sfx(keyword, dur=3.0):
    """مؤثر صوتي تقريبي بالكلمة المفتاحية (احتياطي مضمون)."""
    k = (keyword or "").lower()
    rng = _rng(k)
    n = int(SR * dur)
    t = np.arange(n) / SR
    buf = np.zeros(n)

    def has(*words):
        return any(w in k for w in words)

    if has("wind", "storm", "breeze", "howl", "desert", "ريح"):
        lfo = 0.55 + 0.45 * np.sin(2 * np.pi * 0.35 * t + rng.uniform(0, 6))
        buf = _bandpass(rng.standard_normal(n), 80, 900) * lfo
    elif has("rain", "drizzle", "shower", "مطر"):
        buf = _bandpass(rng.standard_normal(n), 1500, 9000) * 0.7
        for _ in range(int(dur * 25)):
            _add(buf, np.exp(-np.arange(400) / 60.0) * rng.uniform(0.2, 0.6), rng.integers(0, n))
    elif has("drop", "drip", "water", "splash", "ماء"):
        for s in np.arange(0.1, dur - 0.3, rng.uniform(0.5, 0.9)):
            m = int(0.25 * SR)
            tt = np.arange(m) / SR
            f = 1100 * np.exp(-tt * 6) + 350
            _add(buf, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 14) * 0.8, s * SR)
    elif has("foot", "step", "walk", "run", "jog", "خطو"):
        for s in np.arange(0.15, dur - 0.2, 0.55):
            m = int(0.16 * SR)
            tt = np.arange(m) / SR
            thump = _bandpass(rng.standard_normal(m), 60, 500) * np.exp(-tt * 28)
            thump += np.sin(2 * np.pi * 85 * tt) * np.exp(-tt * 30) * 0.8
            _add(buf, thump * rng.uniform(0.7, 1.0), s * SR)
    elif has("fire", "flame", "crackle", "burn", "campfire", "نار"):
        buf = _bandpass(rng.standard_normal(n), 150, 1800) * 0.25
        for _ in range(int(dur * 40)):
            m = int(rng.uniform(0.002, 0.012) * SR)
            _add(buf, rng.standard_normal(m) * np.exp(-np.arange(m) / (m / 4)) * rng.uniform(0.3, 1.0), rng.integers(0, n))
    elif has("explo", "blast", "bomb", "boom", "crash", "impact", "انفجار"):
        env = np.exp(-t * 2.2)
        buf = _bandpass(rng.standard_normal(n), 20, 700) * env
        buf += np.sin(2 * np.pi * (55 * np.exp(-t * 1.5) + 25) * t) * np.exp(-t * 3) * 0.9
    elif has("thunder", "rumble", "رعد"):
        env = (0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 0.6 * t))) * np.exp(-t * 0.8)
        buf = _bandpass(rng.standard_normal(n), 20, 250) * env
    elif has("whoosh", "swoosh", "swish", "swipe", "transition", "pass"):
        m = int(min(dur, 1.2) * SR)
        tt = np.linspace(0, 1, m)
        noise = rng.standard_normal(m)
        sweep = np.zeros(m)
        for i, (lo, hi) in enumerate([(200, 600), (500, 1500), (1200, 3500), (2500, 6000)]):
            seg = _bandpass(noise, lo, hi) * np.exp(-((tt - (0.2 + i * 0.18)) ** 2) / 0.02)
            sweep += seg
        _add(buf, sweep * np.sin(np.pi * tt) ** 0.8, 0.05 * SR)
    elif has("heart", "pulse", "beat", "قلب"):
        for s in np.arange(0.1, dur - 0.4, 0.95):
            for off, amp in ((0, 1.0), (0.28, 0.7)):
                m = int(0.18 * SR)
                tt = np.arange(m) / SR
                _add(buf, np.sin(2 * np.pi * 58 * tt) * np.exp(-tt * 18) * amp, (s + off) * SR)
    elif has("door", "knock", "slam", "باب"):
        for s in (0.1, 0.45, 0.8):
            m = int(0.14 * SR)
            tt = np.arange(m) / SR
            _add(buf, (np.sin(2 * np.pi * 140 * tt) + 0.5 * _bandpass(rng.standard_normal(m), 100, 1200)) * np.exp(-tt * 35), s * SR)
    elif has("alarm", "siren", "beep", "alert", "warning", "إنذار"):
        f = np.where((t * 2.5).astype(int) % 2 == 0, 880, 660)
        buf = np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * 0.35
    elif has("laser", "zap", "shoot", "gun", "pew"):
        m = int(0.35 * SR)
        tt = np.arange(m) / SR
        _add(buf, np.sin(2 * np.pi * np.cumsum(2400 * np.exp(-tt * 9) + 180) / SR) * np.exp(-tt * 7), 0.1 * SR)
    elif has("bird", "chirp", "forest", "nature", "طير"):
        for s in np.arange(0.2, dur - 0.3, rng.uniform(0.6, 1.1)):
            m = int(0.12 * SR)
            tt = np.arange(m) / SR
            f = 3200 + 1200 * np.sin(2 * np.pi * 18 * tt)
            _add(buf, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.sin(np.pi * tt / 0.12) * 0.5, s * SR)
    elif has("crowd", "people", "market", "talk", "chatter", "city", "street", "زحام"):
        lfo = 0.6 + 0.4 * np.sin(2 * np.pi * 3.1 * t + rng.uniform(0, 6)) * np.sin(2 * np.pi * 0.7 * t)
        buf = _bandpass(rng.standard_normal(n), 250, 3200) * lfo
    elif has("engine", "car", "motor", "truck", "machine", "محرك"):
        f0 = 48 + 4 * np.sin(2 * np.pi * 0.5 * t)
        ph = 2 * np.pi * np.cumsum(f0) / SR
        buf = (np.sin(ph) + 0.5 * np.sin(2 * ph) + 0.3 * np.sin(3 * ph)) * 0.6
        buf += _bandpass(rng.standard_normal(n), 60, 600) * 0.2
    else:  # أجواء عامة
        lfo = 0.7 + 0.3 * np.sin(2 * np.pi * 0.2 * t)
        buf = _bandpass(rng.standard_normal(n), 60, 1400) * lfo * 0.6

    return _norm(_fade(buf, 0.05, min(0.4, dur / 3)), 0.8)


def _has_any(text, words):
    return any(w in text for w in words)


def synth_music(prompt="", dur=40.0, tone=""):
    """موسيقى خلفية آلية بسيطة حسب المزاج (احتياطي مضمون). تعيد مصفوفة ستيريو."""
    text = f"{prompt} {tone}".lower()
    dark = _has_any(text, ["dark", "sad", "lonely", "horror", "thriller", "tense", "apocal", "melanch",
                           "ambient", "mystery", "suspense", "sombre", "الرعب", "التشويق", "الكارثة", "الدراما"])
    epic = _has_any(text, ["epic", "cinematic", "action", "orchestral", "hero", "fantasy", "battle",
                           "أكشن", "الفانتازيا", "الأسطور", "الجريمة"])
    happy = _has_any(text, ["happy", "upbeat", "comedy", "funny", "pop", "fun", "bright", "summer",
                            "cheerful", "dance", "الكوميد", "إعلاني", "الإثارة"])
    if epic and not happy:
        bpm, minor = 96, True
    elif dark and not happy:
        bpm, minor = 66, True
    elif happy:
        bpm, minor = 112, False
    else:
        bpm, minor = 84, False

    rng = _rng(text)
    shift = int(rng.choice([0, 2, 3, 5, 7]))
    root = 110.0 * 2 ** (shift / 12.0)
    beat = 60.0 / bpm
    bar = 4 * beat
    n = int(dur * SR)
    buf = np.zeros(n)

    if minor:
        prog = [(0, [0, 3, 7]), (8, [0, 4, 7]), (3, [0, 4, 7]), (10, [0, 4, 7])]
    else:
        prog = [(0, [0, 4, 7]), (7, [0, 4, 7]), (9, [0, 3, 7]), (5, [0, 4, 7])]

    nbars = int(dur / bar) + 2
    for b in range(nbars):
        off, triad = prog[b % 4]
        t0 = b * bar
        base = root * 2 ** (off / 12.0)
        m = int(bar * SR)
        tt = np.arange(m) / SR
        # pad
        env = np.minimum(1.0, tt / (bar * 0.3)) * np.minimum(1.0, (bar - tt) / (bar * 0.35) + 0.0)
        env = np.clip(env, 0, 1)
        pad = np.zeros(m)
        for iv in triad:
            f = base * 2 ** (iv / 12.0) * 2
            pad += np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * 2 * f * tt)
        _add(buf, pad * env * 0.07, t0 * SR)
        # bass
        for bt in (0, 2) if (epic or happy) else (0,):
            mm = int(beat * 2 * SR)
            t2 = np.arange(mm) / SR
            _add(buf, np.sin(2 * np.pi * (base / 2) * t2) * np.exp(-t2 * 2.5) * 0.28, (t0 + bt * beat) * SR)
        # arpeggio
        seq = [triad[0], triad[1], triad[2], triad[1] + 12, triad[2], triad[1], triad[0] + 12, triad[1]]
        step = beat / 2
        for i, iv in enumerate(seq):
            if dark and not epic and i % 2:
                continue
            f = base * 2 ** (iv / 12.0) * 2
            mm = int(0.6 * SR)
            t2 = np.arange(mm) / SR
            note = (np.sin(2 * np.pi * f * t2) + 0.4 * np.sin(2 * np.pi * 2 * f * t2)) * np.exp(-t2 * (4 if not dark else 2.2))
            _add(buf, note * (0.1 if not dark else 0.08), (t0 + i * step) * SR)
        # drums
        if epic or happy:
            for bt in range(4):
                if happy or bt in (0, 2):
                    mm = int(0.25 * SR)
                    t2 = np.arange(mm) / SR
                    f = 50 + 110 * np.exp(-t2 * 25)
                    _add(buf, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t2 * 12) * (0.5 if happy else 0.6), (t0 + bt * beat) * SR)
            if happy:
                for bt in range(8):
                    mm = int(0.04 * SR)
                    hat = _bandpass(rng.standard_normal(mm), 6000, 15000) * np.exp(-np.arange(mm) / (mm / 4))
                    _add(buf, hat * 0.06, (t0 + bt * step + step / 2 * (bt % 2)) * SR)

    # صدى بسيط
    for d, g in ((0.29, 0.35), (0.53, 0.22)):
        k = int(d * SR)
        buf[k:] += buf[:-k] * g
    buf = _fade(buf, 1.2, 2.5)
    buf = _norm(buf, 0.7)
    d = int(0.015 * SR)
    left = buf
    right = np.concatenate([np.zeros(d), buf[:-d]])
    return np.stack([left, right], axis=1)


# ============================================================
# Gemini: السيناريو
# ============================================================
def parse_json_loose(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


SCHEMA_EXAMPLE = (
    '{"title":"عنوان قصير","music_prompt":"English description of instrumental background music",'
    '"scenes":[{"scene_number":1,"location":"المكان","visual_description":"وصف اللقطة",'
    '"search_keyword":"empty street","duration":7,'
    '"sfx":[{"keyword":"wind","description":"soft desert wind howling in the distance","at":0.0},'
    '{"keyword":"footsteps","description":"slow footsteps on dry gravel","at":0.3}],'
    '"dialogue":"الحوار أو التعليق الصوتي"}]}'
)


def gemini_json(api_key, models, prompt, log=lambda m: None):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    last = None
    for m in models:
        try:
            r = client.models.generate_content(
                model=m, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.9))
            return parse_json_loose(r.text), m
        except Exception as e:
            last = e
            log(f"النموذج {m} ما خدمش: {str(e)[:120]}")
            continue
    raise RuntimeError(f"فشلت كل نماذج Gemini. آخر خطأ: {last}")


def build_script_prompt(language, tone, topic, custom_script, target, pace):
    n_scenes = max(2, round(target / pace))
    words = max(6, int(pace * 2.3))
    source = (f"الفكرة: {topic}" if topic else
              f"هذا سيناريو جاهز، حافظ على معناه وقسّمه إلى مشاهد ويمكنك تحسين صياغته:\n{custom_script}")
    script_hint = ""
    if "الأمازيغية" in language:
        script_hint = "\nاكتب الحوار بالأمازيغية المغربية بالحرف اللاتيني (ليظهر في الترجمة النصية بشكل صحيح)."
    return f"""أنت كاتب سيناريو ومخرج ومهندس صوت سينمائي محترف.
المطلوب فيديو مدته الإجمالية {target} ثانية بالضبط، باللغة/اللهجة: {language}، وبأسلوب: {tone}.
{source}{script_hint}

القواعد الصارمة:
1) عدد المشاهد حوالي {n_scenes}، ومجموع قيم duration لكل المشاهد = {target} بالضبط (كل مشهد بين 3 و 15 ثانية).
2) حوار كل مشهد (dialogue) يُقرأ بصوت عالٍ داخل مدة المشهد: حوالي {words} كلمة لكل {pace} ثوانٍ (≈2.3 كلمة في الثانية). لا تتجاوز هذا العدد.
3) search_keyword: من 1 إلى 3 كلمات إنجليزية بسيطة تصف لقطة فيديو واقعية موجودة في مكتبات الفيديو المجانية (مثل: "empty street", "man walking", "dark room"). ممنوع الجمل الطويلة.
4) sfx: لكل مشهد من 1 إلى 3 مؤثرات صوتية تناسب ما يحدث فعلاً في المشهد وما يقوله الحوار. كل مؤثر فيه:
   keyword (كلمة أو كلمتان إنجليزيتان للبحث)، description (وصف إنجليزي دقيق للصوت من 6 إلى 14 كلمة لأدوات التوليد)،
   at (رقم بين 0 و 0.9 = لحظة بدء المؤثر كنسبة من مدة المشهد). لا تضع مؤثرات عشوائية، كل مؤثر مرتبط بحدث في المشهد.
5) music_prompt: وصف إنجليزي للموسيقى الخلفية الآلية بدون غناء تناسب الأسلوب: النوع، الآلات، الإيقاع BPM، المزاج (30 إلى 45 كلمة).
6) أعد JSON صالحاً فقط، بدون شرح وبدون علامات ```، بهذا الهيكل بالضبط:
{SCHEMA_EXAMPLE}"""


def normalize_script(data, target):
    scenes = data.get("scenes") or []
    out = []
    for i, s in enumerate(scenes, 1):
        try:
            dur = float(s.get("duration") or 0)
        except Exception:
            dur = 0.0
        sfx = []
        for e in (s.get("sfx") or s.get("sfx_keywords") or []):
            if isinstance(e, str):
                e = {"keyword": e, "description": e}
            kw = str(e.get("keyword") or "").strip()
            if not kw:
                continue
            try:
                at = min(0.9, max(0.0, float(e.get("at", 0.0))))
            except Exception:
                at = 0.0
            sfx.append({"keyword": kw, "description": str(e.get("description") or kw),
                        "at": at, "enabled": True})
        out.append({
            "scene_number": i,
            "location": str(s.get("location") or ""),
            "visual_description": str(s.get("visual_description") or ""),
            "search_keyword": str(s.get("search_keyword") or "nature").strip(),
            "duration": dur,
            "sfx": sfx[:4],
            "dialogue": str(s.get("dialogue") or "").strip(),
        })
    if not out:
        raise ValueError("السيناريو المُرجَع فارغ.")
    # اجعل مجموع المدد = المدة المطلوبة
    missing = [s for s in out if s["duration"] <= 0]
    for s in missing:
        s["duration"] = target / len(out)
    tot = sum(s["duration"] for s in out)
    k = target / tot if tot else 1
    for s in out:
        s["duration"] = round(max(2.0, s["duration"] * k), 1)
    return {"title": str(data.get("title") or "مشروع جديد"),
            "music_prompt": str(data.get("music_prompt") or "calm cinematic instrumental background music, soft pads, 80 BPM"),
            "scenes": out}


# ============================================================
# التعليق الصوتي
# ============================================================
def tts_edge(text, voice, rate_pct, out_path):
    import edge_tts

    async def _run():
        com = edge_tts.Communicate(text, voice, rate=f"{int(rate_pct):+d}%")
        await com.save(out_path)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 500:
        raise RuntimeError("Edge TTS رجّع ملفاً فارغاً")
    return out_path


LANG_EN = {
    "الدارجة المغربية": "Moroccan Arabic (Darija)", "الأمازيغية": "Moroccan Tamazight",
    "العربية الفصحى": "Modern Standard Arabic", "المصرية": "Egyptian Arabic", "الخليجية": "Gulf Arabic",
    "السورية": "Syrian Arabic", "اللبنانية": "Lebanese Arabic", "العراقية": "Iraqi Arabic",
    "الأردنية": "Jordanian Arabic", "الإماراتية": "Emirati Arabic", "السعودية": "Saudi Arabic",
    "التونسية": "Tunisian Arabic", "الجزائرية": "Algerian Arabic", "الإنجليزية": "English",
    "الفرنسية": "French", "الإسبانية": "Spanish", "الألمانية": "German", "التركية": "Turkish",
}
LANG_CODE = {"الإنجليزية": "en", "الفرنسية": "fr", "الإسبانية": "es", "الألمانية": "de", "التركية": "tr"}  # الباقي = ar
DEFAULT_TTS_SPACES = ["ResembleAI/Chatterbox"]
DEFAULT_STYLE = "natural, warm, expressive storytelling voice with realistic human pacing and emotion"


def gemini_tts_prompt(text, language, style, multi=False):
    lang = LANG_EN.get(language, language)
    p = f"Read aloud in {lang}, in a {style or DEFAULT_STYLE}."
    if multi:
        p += " Leave a clear pause of about one second between paragraphs."
    return p + "\n\n" + text


def tts_gemini(api_key, models, text, voice, language, out_path, style="", multi=False, log=lambda m: None):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    prompt = gemini_tts_prompt(text, language, style, multi)
    last = None
    for m in models:
        for attempt in range(3):
            try:
                res = client.models.generate_content(
                    model=m, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
                part = res.candidates[0].content.parts[0].inline_data
                wav = ensure_wav_bytes(part.data)
                with open(out_path, "wb") as f:
                    f.write(wav)
                return out_path
            except Exception as e:
                last = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    # حصة يومية منتهية → لا فائدة من الانتظار
                    if "per_day" in msg.lower() or "PerDay" in msg or "free_tier_requests" in msg:
                        log("⚠️ الحصة اليومية المجانية لـ Gemini TTS انتهت.")
                        raise RuntimeError("Gemini TTS: الحصة اليومية المجانية انتهت (حوالي 10 طلبات في اليوم)")
                    wait = 15 * (attempt + 1)
                    log(f"حد الطلبات وصل، نعاود بعد {wait} ثانية…")
                    time.sleep(wait)
                    continue
                log(f"نموذج الصوت {m}: {msg[:110]}")
                break
    raise RuntimeError(f"Gemini TTS فشل: {str(last)[:200]}")


def tts_hf(spaces, text, language, out_dir, token=None, log=lambda m: None):
    """Chatterbox Multilingual (MIT) عبر Hugging Face Space. يدعم العربية الفصحى (ليس الدارجة)."""
    code = LANG_CODE.get(language, "ar")
    path, used = hf_audio(spaces, text, None, out_dir, token, timeout=180, log=log,
                          extra={"lang": code})
    return path, used


# ---------- تقسيم صوت طويل إلى مشاهد (طلب Gemini واحد لعدة مشاهد) ----------
def detect_silences(path, noise_db=-32, min_d=0.22):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-af", f"silencedetect=noise={noise_db}dB:d={min_d}",
                        "-f", "null", "-"], capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return [(max(0.0, a), b) for a, b in zip(starts, ends)]


def split_audio_by_text(path, texts, out_prefix):
    """يقطع ملفاً صوتياً واحداً إلى أجزاء بعدد النصوص، عند أقرب فراغ صامت للحدود المتوقعة (حسب طول كل نص)."""
    total = media_duration(path)
    weights = [max(1, len(re.sub(r"\s+", "", t))) for t in texts]
    wsum = float(sum(weights))
    acc, expected = 0.0, []
    for w in weights:
        acc += w
        expected.append(acc / wsum * total)
    sil = detect_silences(path)
    bounds = [0.0]
    for i in range(len(texts) - 1):
        span = weights[i] / wsum * total
        window = max(1.2, 0.35 * span)
        best, best_score = None, None
        for a, b in sil:
            mid = (a + b) / 2
            if abs(mid - expected[i]) <= window and mid > bounds[-1] + 0.8:
                score = abs(mid - expected[i]) - 1.5 * (b - a)  # نفضّل الفراغ الأطول والأقرب
                if best_score is None or score < best_score:
                    best, best_score = mid, score
        bounds.append(best if best is not None else expected[i])
    bounds.append(total)
    outs = []
    for i in range(len(texts)):
        a, b = bounds[i], max(bounds[i] + 0.5, bounds[i + 1])
        out = f"{out_prefix}_{i}.wav"
        r = subprocess.run([FFMPEG, "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", path,
                            "-af", "afade=t=in:d=0.015", "-ar", "24000", "-ac", "1", out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-300:])
        outs.append(out)
    return outs


# ============================================================
# المؤثرات والموسيقى من الخدمات المجانية
# ============================================================
def search_freesound(query, api_key):
    if not api_key:
        return None
    r = requests.get(
        "https://freesound.org/apiv2/search/text/",
        headers={"Authorization": f"Token {api_key}"},
        params={"query": query, "fields": "id,name,previews,license,duration",
                "filter": 'license:"Creative Commons 0" duration:[0.3 TO 15]',
                "page_size": 5, "sort": "score"},
        timeout=15)
    if r.status_code == 401:
        raise RuntimeError("مفتاح Freesound غير صالح (401)")
    r.raise_for_status()
    for it in r.json().get("results", []):
        pv = it.get("previews", {})
        url = pv.get("preview-hq-mp3") or pv.get("preview-lq-mp3")
        if url:
            return {"url": url, "name": it.get("name", "")}
    return None


ACE_BASE = "https://api.acemusic.ai"


def ace_models(api_key):
    try:
        r = requests.get(f"{ACE_BASE}/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
        if r.status_code == 200:
            data = r.json().get("data") or []
            if data:
                return data[0].get("id")
    except Exception:
        pass
    return "acemusic/acestep-v15-turbo"


def ace_music(prompt, duration, api_key, timeout=180):
    """ACE-Step السحابي (رخصة MIT/Apache، استعمال تجاري مسموح). endpoint متوافق مع OpenAI:
    POST /v1/chat/completions مع حقل model إجباري، والصوت يرجع base64."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("لا يوجد مفتاح ACE Music (مجاني من acemusic.ai)")
    model = ace_models(api_key)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"<prompt>{prompt}, instrumental, no vocals</prompt>"}],
        "stream": False,
        "audio_config": {"duration": int(duration), "instrumental": True},
    }
    r = requests.post(f"{ACE_BASE}/v1/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json=payload, timeout=(10, timeout))
    if r.status_code == 401:
        raise RuntimeError("مفتاح ACE Music غير صالح (401)")
    if r.status_code >= 400:
        raise RuntimeError(f"ACE Music {r.status_code}: {r.text[:240]}")
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"رد غير متوقع من ACE: {str(data)[:200]}")
    msg = data["choices"][0]["message"]
    url = None
    for a in (msg.get("audio") or []):
        url = (a.get("audio_url") or {}).get("url")
        if url:
            break
    if not url:
        m = re.search(r"(data:audio/[\w+.-]+;base64,[A-Za-z0-9+/=]+|https?://\S+\.(?:mp3|wav))", msg.get("content") or "")
        url = m.group(1) if m else None
    if not url:
        raise RuntimeError("ACE Music ما رجّع أي ملف صوتي")
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1]), "mp3"
    return requests.get(url, timeout=120).content, "mp3"


MUSIC_WORDS = ["ambient", "cinematic", "epic", "lofi", "piano", "guitar", "electronic", "dark", "upbeat", "calm",
               "orchestral", "acoustic", "jazz", "funk", "hip hop", "synth", "drone", "tense", "happy", "sad"]


def suggest_music_query(prompt, tone=""):
    t = (prompt or "").lower()
    found = [w for w in MUSIC_WORDS if w in t][:3]
    return " ".join(found) if found else "ambient background"


def search_music_library(query, freesound_key=None, n=6):
    """موسيقى بلا حقوق: Freesound (CC0) + Openverse (CC0 / Public Domain). كلاهما آمن للاستعمال التجاري بلا إسناد."""
    res, errs = [], []
    if freesound_key:
        for q in (query, " ".join(query.split()[:2])):
            try:
                r = requests.get("https://freesound.org/apiv2/search/text/",
                                 headers={"Authorization": f"Token {freesound_key}"},
                                 params={"query": q, "fields": "id,name,previews,duration,username,license",
                                         "filter": 'tag:music license:"Creative Commons 0" duration:[20 TO 300]',
                                         "page_size": n, "sort": "rating_desc"}, timeout=15)
                if r.status_code == 401:
                    errs.append("Freesound: مفتاح غير صالح")
                    break
                r.raise_for_status()
                for it in r.json().get("results", []):
                    pv = it.get("previews", {})
                    url = pv.get("preview-hq-mp3") or pv.get("preview-lq-mp3")
                    if url:
                        res.append({"title": it.get("name", ""), "url": url, "duration": float(it.get("duration") or 0),
                                    "source": "Freesound", "license": "CC0", "creator": it.get("username", "")})
                if res:
                    break
            except Exception as e:
                errs.append(f"Freesound: {str(e)[:100]}")
                break
    try:
        r = requests.get("https://api.openverse.org/v1/audio/", headers=UA,
                         params={"q": query, "license": "cc0,pdm", "category": "music", "page_size": n}, timeout=15)
        r.raise_for_status()
        for it in r.json().get("results", []):
            if it.get("url"):
                res.append({"title": it.get("title") or "", "url": it["url"],
                            "duration": (it.get("duration") or 0) / 1000.0, "source": "Openverse · " + str(it.get("source", "")),
                            "license": str(it.get("license", "")).upper(), "creator": it.get("creator") or ""})
    except Exception as e:
        errs.append(f"Openverse: {str(e)[:100]}")
    return res[: n * 2], errs


_AUDIO_EXT = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def _find_audio(obj):
    if isinstance(obj, str):
        if obj.lower().endswith(_AUDIO_EXT) and os.path.exists(obj):
            return obj
        if obj.startswith("http") and obj.lower().split("?")[0].endswith(_AUDIO_EXT):
            return obj
    elif isinstance(obj, dict):
        for k in ("path", "value", "url", "name"):
            if k in obj:
                f = _find_audio(obj[k])
                if f:
                    return f
        for v in obj.values():
            f = _find_audio(v)
            if f:
                return f
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            f = _find_audio(v)
            if f:
                return f
    return None


def _hf_kwargs(params, prompt, duration, extra=None):
    kw, used = {}, False
    for p in params:
        key = p.get("parameter_name")
        if not key:
            continue
        label = (p.get("label") or "").lower()
        blob = f"{key.lower()} {label}"
        comp = (p.get("component") or "").lower()
        ptype = str((p.get("python_type") or {}).get("type", "")).lower()
        has_def = p.get("parameter_has_default", False)
        if "negative" in blob:
            kw[key] = ""
        elif "lyric" in blob:
            kw[key] = "[Instrumental]"
        elif comp in ("audio", "file", "image", "video"):
            if not has_def:
                kw[key] = None
        elif extra and any(k in blob for k in extra):
            kw[key] = next(v for k, v in extra.items() if k in blob)
        elif duration is not None and any(w in blob for w in ("duration", "seconds", "length")) and ptype in ("float", "int", "number", ""):
            kw[key] = float(duration) if "int" not in ptype else int(duration)
        elif (not used) and (comp == "textbox" or ptype == "str") and \
                any(w in blob for w in ("prompt", "text", "caption", "description", "tags", "style", "query")):
            kw[key] = prompt
            used = True
        elif not has_def:
            if "seed" in blob:
                kw[key] = 0
            elif "step" in blob:
                kw[key] = 25
            elif "cfg" in blob or "guidance" in blob:
                kw[key] = 7
            elif ptype == "bool":
                kw[key] = False
    if not used:
        for p in params:
            key = p.get("parameter_name")
            blob = f"{key} {p.get('label') or ''}".lower()
            if key and (p.get("component") or "").lower() == "textbox" and "lyric" not in blob and "negative" not in blob:
                kw[key] = prompt
                used = True
                break
    return kw if used else None


def hf_audio(spaces, prompt, duration, out_dir, token=None, timeout=240, log=lambda m: None, extra=None):
    """يجرّب Hugging Face Spaces واحداً واحداً، ويكتشف تلقائياً الـ endpoint والمعاملات."""
    from gradio_client import Client
    errors = []
    for sp in spaces:
        sp = sp.strip()
        if not sp:
            continue
        try:
            log(f"جاري الاتصال بـ {sp} …")
            client = Client(sp, **({"hf_token": token} if token else {}))
            api = client.view_api(return_format="dict", print_info=False)
            eps = (api or {}).get("named_endpoints", {}) if isinstance(api, dict) else {}
            if not eps:
                raise RuntimeError("لا توجد endpoints مسماة")
            last = None
            for name, info in eps.items():
                if any(w in name.lower() for w in ("load", "clear", "reset", "stop", "lambda", "example")):
                    continue
                kw = _hf_kwargs(info.get("parameters", []), prompt, duration, extra)
                if not kw:
                    continue
                try:
                    res = client.submit(api_name=name, **kw).result(timeout=timeout)
                    found = _find_audio(res)
                    if found:
                        if found.startswith("http"):
                            ext = found.lower().split("?")[0].rsplit(".", 1)[-1]
                            dest = os.path.join(out_dir, f"hf_{hashlib.md5(found.encode()).hexdigest()[:10]}.{ext}")
                            download_file(found, dest)
                            return dest, sp
                        ext = found.rsplit(".", 1)[-1]
                        dest = os.path.join(out_dir, f"hf_{hashlib.md5((sp + prompt + str(time.time())).encode()).hexdigest()[:10]}.{ext}")
                        with open(found, "rb") as a, open(dest, "wb") as b:
                            b.write(a.read())
                        return dest, sp
                    last = "لا يوجد ملف صوتي في النتيجة"
                except Exception as e:
                    last = str(e)[:160]
            raise RuntimeError(last or "لا يوجد endpoint مناسب")
        except Exception as e:
            errors.append(f"{sp}: {str(e)[:140]}")
            log(f"⚠️ {errors[-1]}")
    raise RuntimeError(" | ".join(errors) if errors else "لا توجد Spaces")


# ============================================================
# الفيديوهات الجاهزة (Stock)
# ============================================================
def search_pexels(query, key, orient, n=6):
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                     params={"query": query, "per_page": n, "orientation": orient}, timeout=15)
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
        if not files:
            continue
        best = min(files, key=lambda f: abs(min(f.get("width") or 0, f.get("height") or 0) - 720))
        out.append({"source": "Pexels", "thumb": v.get("image"), "url": best["link"], "preview": best["link"],
                    "duration": v.get("duration")})
    return out


def search_pixabay(query, key, n=6):
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": query, "per_page": max(3, n), "safesearch": "true"}, timeout=15)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        vids = h.get("videos", {})
        pick = vids.get("medium") or vids.get("small") or vids.get("large") or vids.get("tiny")
        prev = vids.get("tiny") or vids.get("small") or pick
        if not pick or not pick.get("url"):
            continue
        thumb = (pick.get("thumbnail") or prev.get("thumbnail") or None) if isinstance(pick, dict) else None
        out.append({"source": "Pixabay", "thumb": thumb, "url": pick["url"],
                    "preview": (prev or pick).get("url"), "duration": h.get("duration")})
    return out


def make_thumb(video_url, dest, timeout=25):
    """يلتقط صورة مصغّرة من الفيديو مباشرة (بدون الاعتماد على أي CDN للصور)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 500:
        return dest
    ua = ["-user_agent", "Mozilla/5.0"] if str(video_url).startswith("http") else []
    r = subprocess.run([FFMPEG, "-y", *ua, "-ss", "0.6", "-i", video_url,
                        "-frames:v", "1", "-vf", "scale=360:-2", dest],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError(r.stderr[-200:])
    return dest


def find_clips(query, orient, pexels_key, pixabay_key, n=6):
    res, errs = [], []
    for fn, args in ((search_pexels, (query, pexels_key, orient, n)), (search_pixabay, (query, pixabay_key, n))):
        if not args[1]:
            continue
        try:
            res += fn(*args)
        except Exception as e:
            errs.append(f"{fn.__name__}: {str(e)[:100]}")
    if not res and " " in query.strip():  # جرّب أول كلمة فقط
        return find_clips(query.split()[0], orient, pexels_key, pixabay_key, n)
    return res[:n], errs


# ============================================================
# الخط العربي + الترجمة المحروقة
# ============================================================
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/tajawal/Tajawal-Bold.ttf"


def ensure_font(font_dir):
    os.makedirs(font_dir, exist_ok=True)
    p = os.path.join(font_dir, "Tajawal-Bold.ttf")
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return p
    try:
        download_file(FONT_URL, p, timeout=30)
        return p
    except Exception:
        pass
    for c in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
              "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"):
        if os.path.exists(c):
            return c
    return None


def shape(text):
    if re.search(r"[\u0600-\u06FF]", text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text


def _load_font(path, size):
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def text_image(text, max_w, font_path, size, color="#FFFFFF", stroke="#000000"):
    font = _load_font(font_path, size)
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if font.getlength(shape(t)) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    sw = max(2, size // 11)
    lh = int(size * 1.35)
    img = Image.new("RGBA", (int(max_w) + 2 * sw + 8, lh * len(lines) + 2 * sw + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        s = shape(ln)
        w = font.getlength(s)
        d.text(((img.width - w) / 2, sw + 4 + i * lh), s, font=font, fill=color,
               stroke_width=sw, stroke_fill=stroke)
    return np.array(img)


def caption_chunks(text, max_words):
    parts = [p.strip() for p in re.split(r"(?<=[\.\!\?\؟\،\,\:\;…])\s+", text) if p.strip()]
    chunks = []
    for p in parts:
        w = p.split()
        for i in range(0, len(w), max_words):
            chunks.append(" ".join(w[i:i + max_words]))
    return chunks or [text]


# ============================================================
# الجدول الزمني
# ============================================================
VOICE_PAD = 0.7     # فراغ بعد الحوار
VOICE_START = 0.25  # بداية الصوت داخل المشهد


def build_timeline(scenes, assets, target, master_dur=None):
    n = len(scenes)
    plan = [max(2.0, float(s.get("duration") or target / n)) for s in scenes]
    if master_dur:
        chars = [max(12, len(s.get("dialogue", ""))) for s in scenes]
        tot = sum(chars)
        durs = [master_dur * c / tot for c in chars]
        t, items = 0.0, []
        for s, d in zip(scenes, durs):
            items.append({"n": s["scene_number"], "start": t, "dur": d, "voice_dur": d, "master_off": t})
            t += d
        return {"items": items, "total": master_dur, "speed": 1.0, "overflow": False, "master": True}

    vd = [float((assets.get(str(s["scene_number"]), {}) or {}).get("audio_dur") or 0) for s in scenes]

    def base(speed):
        return [max(p, v / speed + VOICE_PAD) if v else p for p, v in zip(plan, vd)]

    speed = 1.0
    durs = base(1.0)
    if sum(durs) > target + 0.5:
        f = 1.0
        while f < 1.3:
            f = round(f + 0.02, 2)
            if sum(base(f)) <= target + 0.5:
                break
        speed = f
        durs = base(speed)
    total = sum(durs)
    overflow = total > target + 0.5
    if not overflow and total < target:
        k = target / total
        durs = [d * k for d in durs]
        total = target
    t, items = 0.0, []
    for s, d, v in zip(scenes, durs, vd):
        items.append({"n": s["scene_number"], "start": t, "dur": d, "voice_dur": v / speed if v else 0.0})
        t += d
    return {"items": items, "total": total, "speed": speed, "overflow": overflow, "master": False}


# ============================================================
# المونتاج
# ============================================================
PALETTES = [((18, 22, 48), (96, 70, 220)), ((10, 38, 56), (20, 160, 170)), ((46, 18, 40), (225, 96, 110)),
            ((28, 30, 20), (150, 160, 60)), ((20, 20, 24), (90, 100, 130))]


def gradient_frame(W, H, idx):
    c1, c2 = PALETTES[idx % len(PALETTES)]
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    k = np.clip(0.65 * y + 0.35 * x, 0, 1)[..., None]
    arr = (np.array(c1) * (1 - k) + np.array(c2) * k).astype("uint8")
    return arr


def _fit_cover(clip, W, H):
    scale = max(W / clip.w, H / clip.h)
    clip = clip.resized(scale)
    return clip.cropped(x_center=clip.w / 2, y_center=clip.h / 2, width=W, height=H)


def render_scene(scene, idx, item, assets, cfg, tl, work_dir, font_path, master_audio_path=None):
    from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip, ImageClip,
                         VideoFileClip, afx, vfx)
    W, H, fps = cfg["W"], cfg["H"], cfg["fps"]
    d = float(item["dur"])
    sn = str(scene["scene_number"])
    a = assets.get(sn, {}) or {}
    closers, layers = [], []

    # --- الخلفية ---
    clip_path = a.get("clip_path")
    if clip_path and os.path.exists(clip_path):
        try:
            base = VideoFileClip(clip_path, audio=False)
            closers.append(base)
            base = _fit_cover(base, W, H)
            base = base.subclipped(0, d) if base.duration >= d else base.with_effects([vfx.Loop(duration=d)])
            layers.append(base.with_duration(d))
        except Exception:
            clip_path = None
    if not layers:
        bg = ImageClip(gradient_frame(W, H, idx)).with_duration(d)
        layers.append(bg)
        loc = scene.get("location") or ""
        if loc:
            im = text_image(loc, W * 0.8, font_path, max(24, W // 18), "#FFFFFF")
            layers.append(ImageClip(im).with_duration(d).with_position(("center", "center")).with_opacity(0.85))

    # --- الترجمة ---
    voice_start = VOICE_START
    voice_len = item["voice_dur"] if item["voice_dur"] else d - 0.6
    if cfg.get("captions") and scene.get("dialogue"):
        maxw = 5 if H > W else 8
        chunks = caption_chunks(scene["dialogue"], maxw)
        tot_chars = sum(len(c) for c in chunks) or 1
        t0 = voice_start if not tl["master"] else 0.0
        span = min(voice_len, d - t0)
        for c in chunks:
            seg = span * len(c) / tot_chars
            if seg <= 0.05:
                continue
            im = text_image(c, W * 0.86, font_path, cfg["cap_size"], cfg["cap_color"])
            y = {"أسفل": H * 0.74, "وسط": H * 0.45, "أعلى": H * 0.12}.get(cfg["cap_pos"], H * 0.74)
            y = min(y, H - im.shape[0] - 10)
            layers.append(ImageClip(im).with_start(t0).with_duration(seg).with_position(("center", y)))
            t0 += seg

    # --- علامة مائية ---
    if cfg.get("watermark"):
        im = text_image(cfg["watermark"], W * 0.5, font_path, max(18, W // 30), "#FFFFFF")
        layers.append(ImageClip(im).with_duration(d).with_opacity(0.55)
                      .with_position((W - im.shape[1] - 12, H - im.shape[0] - 12)))

    video = CompositeVideoClip(layers, size=(W, H)).with_duration(d)
    video = video.with_effects([vfx.FadeIn(0.2), vfx.FadeOut(0.2)])

    # --- الصوت ---
    sil = os.path.join(work_dir, "silence.wav")
    if not os.path.exists(sil):
        write_wav(sil, np.zeros(int(SR * 1.0)))
    tracks = []
    tracks.append(AudioFileClip(sil, fps=SR).with_effects([afx.AudioLoop(duration=d)]))
    if tl["master"] and master_audio_path:
        ma = AudioFileClip(master_audio_path, fps=SR)
        closers.append(ma)
        s0 = item["master_off"]
        tracks.append(ma.subclipped(s0, min(ma.duration, s0 + d)).with_volume_scaled(cfg["vol_voice"]))
    elif a.get("audio_path") and os.path.exists(a["audio_path"]):
        vp = a["audio_path"]
        if tl["speed"] > 1.001:
            vp2 = os.path.join(work_dir, f"voice_fast_{sn}.wav")
            vp = atempo(vp, vp2, tl["speed"])
        va = AudioFileClip(vp, fps=SR)
        closers.append(va)
        va = va.subclipped(0, min(va.duration, d - voice_start - 0.05))
        tracks.append(va.with_start(voice_start).with_volume_scaled(cfg["vol_voice"]))
    for e in (a.get("sfx") or []):
        if not e.get("enabled", True) or not e.get("path") or not os.path.exists(e["path"]):
            continue
        try:
            st_ = float(e.get("at", 0)) * d
            sc = AudioFileClip(e["path"], fps=SR)
            closers.append(sc)
            ln = min(sc.duration, d - st_ - 0.05, 6.0)
            if ln < 0.2:
                continue
            sc = sc.subclipped(0, ln).with_effects([afx.AudioFadeOut(min(0.4, ln / 3))])
            tracks.append(sc.with_start(st_).with_volume_scaled(cfg["vol_sfx"]))
        except Exception:
            continue
    audio = CompositeAudioClip(tracks).with_duration(d)
    video = video.with_audio(audio)

    out = os.path.join(work_dir, f"scene_{idx:03d}.mp4")
    video.write_videofile(out, fps=fps, codec="libx264", audio_codec="aac", audio_fps=SR,
                          preset="ultrafast", threads=2, logger=None,
                          ffmpeg_params=["-pix_fmt", "yuv420p"])
    for c in closers + [video, audio]:
        try:
            c.close()
        except Exception:
            pass
    return out


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])
    return r


def concat_scenes(paths, out_path, work_dir):
    lst = os.path.join(work_dir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path])
    return out_path


def mix_music(video_in, music_path, out_path, total, vol, duck):
    fade = 2.0
    st_out = max(0.0, total - fade)
    mv = min(1.5, vol * (2.0 if duck else 1.0))
    mus = f"[1:a]volume={mv:.3f},afade=t=in:d=1.5,afade=t=out:st={st_out:.2f}:d={fade}[m]"
    variants = []
    if duck:
        variants.append(mus + ";[0:a]asplit=2[vo][sc];[m][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[d];"
                              "[vo][d]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    variants.append(mus + ";[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    mus2 = f"[1:a]volume={vol * 2:.3f},afade=t=in:d=1.5,afade=t=out:st={st_out:.2f}:d={fade}[m]"
    variants.append(mus2 + ";[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
    last = None
    for fc in variants:
        try:
            _run([FFMPEG, "-y", "-i", video_in, "-stream_loop", "-1", "-i", music_path,
                  "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.2f}", "-movflags", "+faststart", out_path])
            return out_path
        except Exception as e:
            last = e
    raise RuntimeError(f"فشل دمج الموسيقى: {last}")


def render_project(project, assets, cfg, timeline, music_path, out_path, work_dir, font_dir,
                   master_audio_path=None, progress=lambda f, m: None):
    os.makedirs(work_dir, exist_ok=True)
    font_path = ensure_font(font_dir)
    scenes = project["scenes"]
    n = len(scenes)
    scene_files = []
    for i, (sc, item) in enumerate(zip(scenes, timeline["items"])):
        progress(i / n * 0.88, f"تركيب المشهد {i + 1} من {n}")
        scene_files.append(render_scene(sc, i, item, assets, cfg, timeline, work_dir, font_path, master_audio_path))
    progress(0.9, "دمج المشاهد")
    merged = os.path.join(work_dir, "merged.mp4")
    concat_scenes(scene_files, merged, work_dir)
    if music_path and os.path.exists(music_path):
        progress(0.95, "إضافة الموسيقى")
        mix_music(merged, music_path, out_path, timeline["total"], cfg["vol_music"], cfg["duck"])
    else:
        os.replace(merged, out_path)
    for f in scene_files:
        try:
            os.remove(f)
        except Exception:
            pass
    progress(1.0, "الفيديو جاهز")
    return out_path


# ============================================================
# فحص الخدمات (تشخيص)
# ============================================================
def check_services(keys):
    out = []

    def run(name, fn):
        t = time.time()
        try:
            msg = fn()
            out.append((name, True, f"{msg} · {time.time() - t:.1f}s"))
        except Exception as e:
            out.append((name, False, str(e)[:220]))

    def t_gemini():
        if not keys.get("gemini"):
            raise RuntimeError("لا يوجد مفتاح")
        from google import genai
        c = genai.Client(api_key=keys["gemini"])
        n = sum(1 for _ in zip(range(3), c.models.list()))
        return f"المفتاح صالح ({n}+ نماذج)"

    def t_ace():
        if not keys.get("ace"):
            raise RuntimeError("لا يوجد مفتاح")
        r = requests.get(f"{ACE_BASE}/v1/models", headers={"Authorization": f"Bearer {keys['ace']}"}, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("مفتاح غير صالح (401)")
        r.raise_for_status()
        return "المفتاح صالح · " + str((r.json().get("data") or [{}])[0].get("id", ""))

    def t_fs():
        if not keys.get("freesound"):
            raise RuntimeError("لا يوجد مفتاح")
        r = search_freesound("rain", keys["freesound"])
        return "المفتاح صالح" if r is not None else "المفتاح صالح (لا نتائج)"

    def t_pexels():
        if not keys.get("pexels"):
            raise RuntimeError("لا يوجد مفتاح")
        return f"{len(search_pexels('nature', keys['pexels'], 'landscape', 1))} نتيجة"

    def t_pix():
        if not keys.get("pixabay"):
            raise RuntimeError("لا يوجد مفتاح")
        return f"{len(search_pixabay('nature', keys['pixabay'], 3))} نتيجة"

    def t_ov():
        res, errs = search_music_library("ambient", None, 2)
        if not res:
            raise RuntimeError("; ".join(errs) or "لا نتائج")
        return f"{len(res)} مسار CC0"

    def t_edge():
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "edge_probe.mp3")
        tts_edge("مرحبا", "ar-MA-JamalNeural", 0, p)
        return "يعمل"

    run("Gemini", t_gemini)
    run("ACE Music", t_ace)
    run("Freesound", t_fs)
    run("Pexels", t_pexels)
    run("Pixabay", t_pix)
    run("Openverse (موسيقى CC0)", t_ov)
    run("Edge TTS", t_edge)
    return out
