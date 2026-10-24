# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import streamlit as st

import engine as E

st.set_page_config(page_title="AI Studio Pro", page_icon="🎬", layout="wide",
                   initial_sidebar_state="expanded")

# ============================================================
# إعدادات عامة + حفظ المشروع (لكل زائر مشروعه عبر ?p=…)
# ============================================================
DATA_DIR = "persistent_data"
FONT_DIR = os.path.join(DATA_DIR, "fonts")
os.makedirs(os.path.join(DATA_DIR, "projects"), exist_ok=True)

LANGS = list(E.EDGE_VOICES.keys())
TONES = ["الكوميدية", "الأكشن والرسوم المتحركة", "الأفلام الموسيقية والرقص",
         "ما بعد الكارثة / النجاة المستقبلية", "الفانتازيا والأسطوريات", "الجريمة والسرقة",
         "التشويق", "الرعب", "الإثارة", "الدراما", "إعلاني (تجاري)"]
PACES = {"سريع (≈4 ث / مشهد)": 4, "متوسط (≈7 ث / مشهد)": 7, "هادئ (≈10 ث / مشهد)": 10}
VOICE_ENGINES = ["Gemini TTS (صوت بشري · الأفضل)", "Chatterbox (Hugging Face · تجريبي)", "Edge TTS (سريع · أقل طبيعية)"]
SFX_MODES = ["تلقائي (Freesound ← ذكاء اصطناعي ← توليد محلي)", "Freesound فقط",
             "ذكاء اصطناعي (Hugging Face) فقط", "توليد محلي فقط"]
MUSIC_MODES = ["تلقائي (ACE-Step ← مكتبة CC0 ← توليد محلي)", "ACE-Step (ذكاء اصطناعي · استعمال تجاري)",
               "مكتبة موسيقى مجانية بلا حقوق (CC0)", "Hugging Face Spaces", "توليد محلي (بدون إنترنت)",
               "رفع ملف موسيقى", "بدون موسيقى"]
M_AUTO, M_ACE, M_LIB, M_HF, M_LOCAL, M_UP, M_NONE = MUSIC_MODES

CFG = {
    "cfg_language": "الدارجة المغربية", "cfg_tone": "الكوميدية", "cfg_duration": 60,
    "cfg_pace": "متوسط (≈7 ث / مشهد)", "cfg_aspect": "9:16 (ريلز / تيك توك)", "cfg_quality": "720p",
    "cfg_fps": 24, "cfg_voice_engine": VOICE_ENGINES[0], "cfg_gender": "ذكر", "cfg_voice_rate": 0,
    "cfg_gemini_voice": "Fenrir", "cfg_script_model": E.GEMINI_SCRIPT_FALLBACKS[0],
    "cfg_tts_model": E.GEMINI_TTS_MODELS[0],
    "cfg_vol_voice": 100, "cfg_vol_sfx": 55, "cfg_vol_music": 18, "cfg_duck": True,
    "cfg_captions": True, "cfg_cap_size": 56, "cfg_cap_color": "#FFFFFF", "cfg_cap_pos": "أسفل",
    "cfg_watermark": "",
    "cfg_tts_style": E.DEFAULT_STYLE, "cfg_tts_spaces": "\n".join(E.DEFAULT_TTS_SPACES),
    "cfg_music_spaces": "\n".join(E.DEFAULT_MUSIC_SPACES),
    "cfg_sfx_spaces": "\n".join(E.DEFAULT_SFX_SPACES),
}
STATE_KEYS = list(CFG) + ["project", "assets", "step", "gen_id", "last_custom_script", "topic",
                          "music_prompt", "music_path", "music_info", "voice_mode", "master_audio_path",
                          "sfx_mode", "music_mode", "final_path", "music_query", "music_results"]

ss = st.session_state


def secret(name):
    try:
        return str(st.secrets.get(name, "")) or os.environ.get(name, "")
    except Exception:
        return os.environ.get(name, "")


def key(name):
    """مفتاح من خانة الشريط الجانبي أو من st.secrets (لا يُحفظ أبداً في الملفات)."""
    return (ss.get(f"key_{name}") or "").strip() or secret(name.upper() + "_API_KEY") or secret(name.upper() + "_TOKEN")


# --- معرّف المشروع ---
pid = st.query_params.get("p", "")
if not re.fullmatch(r"[a-f0-9]{12}", str(pid)):
    pid = uuid.uuid4().hex[:12]
    st.query_params["p"] = pid
PDIR = os.path.join(DATA_DIR, "projects", pid)
ADIR = os.path.join(PDIR, "audio")
VDIR = os.path.join(PDIR, "clips")
WDIR = os.path.join(PDIR, "work")
for d in (PDIR, ADIR, VDIR, WDIR):
    os.makedirs(d, exist_ok=True)
STATE_FILE = os.path.join(PDIR, "state.json")


def save_state():
    try:
        data = {k: ss.get(k) for k in STATE_KEYS}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def cleanup_old_projects(days=3):
    root = os.path.join(DATA_DIR, "projects")
    now = time.time()
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if name != pid and now - os.path.getmtime(p) > days * 86400:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


if "_loaded" not in ss:
    for k, v in CFG.items():
        ss.setdefault(k, v)
    ss.setdefault("project", None)
    ss.setdefault("assets", {})
    ss.setdefault("step", 1)
    ss.setdefault("gen_id", 0)
    ss.setdefault("last_custom_script", "")
    ss.setdefault("topic", "")
    ss.setdefault("music_prompt", "")
    ss.setdefault("music_path", None)
    ss.setdefault("music_info", "")
    ss.setdefault("voice_mode", "per_scene")
    ss.setdefault("master_audio_path", None)
    ss.setdefault("sfx_mode", SFX_MODES[0])
    ss.setdefault("music_mode", MUSIC_MODES[0])
    ss.setdefault("final_path", None)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in STATE_KEYS:
                if saved.get(k) is not None:
                    ss[k] = saved[k]
        except Exception:
            pass
    # قيم قديمة لم تعد موجودة في الخيارات → ارجع للافتراضي (وإلا يتعطل selectbox)
    for _k, _opts in (("cfg_voice_engine", VOICE_ENGINES), ("cfg_pace", list(PACES)), ("cfg_aspect", list(E.ASPECTS)),
                      ("cfg_language", LANGS), ("cfg_tone", TONES), ("sfx_mode", SFX_MODES), ("music_mode", MUSIC_MODES)):
        if ss.get(_k) not in _opts:
            ss[_k] = CFG.get(_k, _opts[0])
    ss.setdefault("music_query", "")
    ss.setdefault("music_results", [])
    cleanup_old_projects()
    ss["_loaded"] = True

# ============================================================
# التصميم (CSS)
# ============================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap');
:root{
  --bg:#0C0F16; --surface:#141924; --surface2:#1B2130; --line:#272E40;
  --text:#E9EDF6; --muted:#8C96AD; --brand:#7B61FF; --brand2:#4F8CFF; --ok:#2BD4A0; --warn:#FFB020;
}
.stApp, .stApp p, .stApp label, .stApp textarea, .stApp input, .stApp button, .stApp h1, .stApp h2, .stApp h3, .stApp h4{
  font-family:'Tajawal',sans-serif;
}
.stApp{background:var(--bg);}
header[data-testid="stHeader"]{background:transparent;}
.block-container{padding-top:1.4rem; max-width:1500px;}
/* اتجاه النص تلقائي حسب اللغة */
.stApp textarea, .stApp input, .stMarkdown p, .stMarkdown li, label p, .stAlert p{unicode-bidi:plaintext; text-align:start;}
section[data-testid="stSidebar"]{background:var(--surface); border-right:1px solid var(--line);}
section[data-testid="stSidebar"] .block-container{padding-top:1rem;}
section[data-testid="stSidebar"] [data-testid="stExpander"]{background:var(--surface2); border:1px solid var(--line); border-radius:12px;}

/* الشريط العلوي */
.topbar{display:flex; align-items:center; gap:14px; padding:6px 2px 14px 2px;}
.logo{width:44px; height:44px; border-radius:12px; display:grid; place-items:center; font-size:22px;
  background:linear-gradient(135deg,var(--brand),var(--brand2)); box-shadow:0 6px 22px rgba(123,97,255,.35);}
.brand{display:flex; flex-direction:column; gap:2px;}
.brand-t{font-size:26px; font-weight:800; line-height:1.2; color:var(--text); unicode-bidi:plaintext;}
.brand span{font-size:13px; color:var(--muted); direction:ltr; text-align:start;}
.chip{margin-inline-start:auto; padding:6px 12px; border:1px solid var(--line); border-radius:999px;
  color:var(--muted); font-size:13px; background:var(--surface);}

/* الخطوات */
.st-key-stepper button{border-radius:12px; border:1px solid var(--line); background:var(--surface);
  color:var(--muted); height:52px; font-weight:700; transition:border-color .15s;}
.st-key-stepper button:hover{border-color:var(--brand); color:var(--text);}
.st-key-stepper button[kind="primary"]{background:linear-gradient(135deg,var(--brand),var(--brand2));
  color:#fff; border:none; box-shadow:0 8px 24px rgba(79,140,255,.28);}

/* البطاقات */
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:14px;}
div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div > .card-mark){background:var(--surface);}
.h-title{font-size:22px; font-weight:800; color:var(--text); margin:2px 0 2px 0; direction:rtl;}
.h-sub{font-size:14px; color:var(--muted); margin-bottom:12px; direction:rtl;}
.stButton>button[kind="primary"]{background:linear-gradient(135deg,var(--brand),var(--brand2)); border:none; font-weight:700;}
.stButton>button{border-radius:10px;}

/* ملخص المشروع */
.st-key-summary{position:sticky; top:3.8rem;}
.sum-card{background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:14px 16px; direction:rtl;}
.sum-card h4{margin:0 0 8px 0; font-size:15px; color:var(--text);}
.sum-row{display:flex; justify-content:space-between; font-size:13.5px; color:var(--muted); padding:4px 0;}
.sum-row b{color:var(--text); font-weight:700;}
.ok{color:var(--ok)!important;} .no{color:var(--muted)!important;} .warn{color:var(--warn)!important;}
.tl{display:flex; gap:3px; height:34px; margin:6px 0 4px 0; direction:ltr;}
.tl i{flex:1 1 0; border-radius:6px; font-style:normal; font-size:11px; display:grid; place-items:center; color:#fff;
  background:linear-gradient(180deg,#5A47D6,#3F66C9); min-width:14px; overflow:hidden;}
.tl i.off{background:#2A3148; color:var(--muted);}
.pill{display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; background:var(--surface2);
  border:1px solid var(--line); color:var(--muted); margin-inline-end:6px;}
.scene-n{width:30px; height:30px; border-radius:9px; display:inline-grid; place-items:center; font-weight:800;
  background:linear-gradient(135deg,var(--brand),var(--brand2)); color:#fff; margin-inline-end:8px;}
</style>
""", unsafe_allow_html=True)

# ============================================================
# الشريط الجانبي
# ============================================================
with st.sidebar:
    st.markdown("### ⚙️ إعدادات المشروع")
    with st.expander("🎞️ الفيديو", expanded=True):
        st.selectbox("اللغة / اللهجة", LANGS, key="cfg_language")
        st.selectbox("الأسلوب", TONES, key="cfg_tone")
        st.slider("مدة الفيديو النهائي (ثانية)", 10, 600, step=5, key="cfg_duration",
                  help="الفيديو النهائي يخرج بهذه المدة (السيناريو والتركيب كيتكيّفو معها).")
        st.selectbox("إيقاع المونتاج", list(PACES), key="cfg_pace")
        st.selectbox("الأبعاد", list(E.ASPECTS), key="cfg_aspect")
        c1, c2 = st.columns(2)
        c1.selectbox("الجودة", ["480p", "720p", "1080p"], key="cfg_quality")
        c2.selectbox("FPS", [24, 30], key="cfg_fps")
    with st.expander("🎙️ الصوت"):
        st.selectbox("محرك التعليق الصوتي", VOICE_ENGINES, key="cfg_voice_engine")
        _k = "gemini" if ss.cfg_voice_engine.startswith("Gemini") else "hf" if ss.cfg_voice_engine.startswith("Chatter") else "edge"
        st.selectbox("صوت Gemini", E.GEMINI_VOICES, key="cfg_gemini_voice", disabled=_k != "gemini")
        st.text_input("أسلوب الأداء (Gemini)", key="cfg_tts_style", disabled=_k != "gemini",
                      help="مثال: playful comedic voice / calm documentary narrator / dramatic and tense")
        st.radio("صوت Edge", ["ذكر", "أنثى"], horizontal=True, key="cfg_gender", disabled=_k != "edge")
        st.slider("سرعة Edge (%)", -30, 40, key="cfg_voice_rate", disabled=_k != "edge")
    with st.expander("🎚️ المزج"):
        st.slider("الصوت %", 0, 150, key="cfg_vol_voice")
        st.slider("المؤثرات %", 0, 150, key="cfg_vol_sfx")
        st.slider("الموسيقى %", 0, 80, key="cfg_vol_music")
        st.toggle("خفض الموسيقى تلقائياً أثناء الكلام (Ducking)", key="cfg_duck")
    with st.expander("💬 الترجمة النصية"):
        st.toggle("إظهار الترجمة على الفيديو", key="cfg_captions")
        st.slider("حجم الخط", 28, 96, key="cfg_cap_size")
        c1, c2 = st.columns(2)
        c1.color_picker("اللون", key="cfg_cap_color")
        c2.selectbox("الموضع", ["أسفل", "وسط", "أعلى"], key="cfg_cap_pos")
        st.text_input("علامة مائية (اختياري)", key="cfg_watermark", placeholder="@اسم_القناة")
    with st.expander("🔑 المفاتيح (مجانية)"):
        st.caption("المفاتيح لا تُحفظ في أي ملف. الأفضل وضعها في Secrets (انظر الشرح).")

        def key_field(label, name, env):
            has = bool(secret(env))
            st.text_input(label + ("  ✅ مفعّل من السيرفر" if has else ""), type="password", key=f"key_{name}",
                          placeholder="اتركه فارغاً لاستعمال مفتاح السيرفر" if has else "")

        key_field("Google Gemini", "gemini", "GEMINI_API_KEY")
        key_field("ACE Music", "ace", "ACE_API_KEY")
        key_field("Pexels (فيديو)", "pexels", "PEXELS_API_KEY")
        key_field("Pixabay (فيديو)", "pixabay", "PIXABAY_API_KEY")
        key_field("Freesound (مؤثرات)", "freesound", "FREESOUND_API_KEY")
        key_field("Hugging Face Token (اختياري)", "hf", "HF_TOKEN")
    with st.expander("🧪 متقدم"):
        st.text_input("نموذج السيناريو", key="cfg_script_model")
        st.text_input("نموذج Gemini TTS", key="cfg_tts_model")
        st.text_area("Spaces للموسيقى (سطر لكل Space)", key="cfg_music_spaces", height=90)
        st.text_area("Spaces للمؤثرات (سطر لكل Space)", key="cfg_sfx_spaces", height=90)
        st.text_area("Spaces للصوت Chatterbox (سطر لكل Space)", key="cfg_tts_spaces", height=70)
    with st.expander("🩺 فحص الخدمات"):
        st.caption("كيجرب كل مفتاح/خدمة ويقول ليك شكون خدام وشكون لا، مع سبب الخطأ.")
        if st.button("تشغيل الفحص", width="stretch"):
            with st.spinner("…"):
                _res = E.check_services({n: key(n) for n in ("gemini", "ace", "freesound", "pexels", "pixabay")})
            for _n, _ok, _m in _res:
                st.markdown(f"{'✅' if _ok else '❌'} **{_n}** — {_m}")
    st.divider()
    if st.button("🗑️ مشروع جديد (مسح كل شيء)", width="stretch"):
        shutil.rmtree(PDIR, ignore_errors=True)
        st.query_params.clear()
        ss.clear()
        st.rerun()

# ============================================================
# دوال مساعدة
# ============================================================
STEPS = [("السيناريو", "📝"), ("التعليق الصوتي", "🎙️"), ("المؤثرات", "🔊"), ("الموسيقى", "🎵"), ("المونتاج", "🎬")]


def go(n):
    ss.step = n


def scenes():
    return (ss.project or {}).get("scenes", [])


def A(sn):
    return ss.assets.setdefault(str(sn), {})


def target():
    return int(ss.cfg_duration)


def size():
    return E.video_size(ss.cfg_aspect, int(ss.cfg_quality[:-1]))


def sfx_spaces():
    return [x.strip() for x in ss.cfg_sfx_spaces.splitlines() if x.strip()]


def music_spaces():
    return [x.strip() for x in ss.cfg_music_spaces.splitlines() if x.strip()]


def head(title, sub):
    st.markdown(f"<div class='h-title'>{title}</div><div class='h-sub'>{sub}</div>", unsafe_allow_html=True)


def nav(prev=None, nxt=None, nxt_label="التالي"):
    c1, c2, c3 = st.columns([1, 2, 1])
    if prev:
        c1.button("← السابق", on_click=go, args=(prev,), key=f"prev{prev}", width="stretch")
    if nxt:
        c3.button(f"{nxt_label} →", on_click=go, args=(nxt,), key=f"next{nxt}", type="primary", width="stretch")


def master_dur():
    if ss.voice_mode == "master" and ss.master_audio_path and os.path.exists(ss.master_audio_path):
        return E.media_duration(ss.master_audio_path)
    return None


def timeline():
    return E.build_timeline(scenes(), ss.assets, target(), master_dur())


# ============================================================
# التركيب في الخلفية (يستمر حتى لو تحدّثت الصفحة)
# ============================================================
STATUS_FILE = os.path.join(PDIR, "render.json")


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_status(d):
    try:
        d["ts"] = time.time()
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def render_alive():
    return any(t.name == f"render-{pid}" and t.is_alive() for t in threading.enumerate())


def _render_worker(project, assets, cfg, tl, music_path, out, work_dir, font_dir, master_audio, clip_jobs, vdir, status_file):
    def put(state, **kw):
        kw["state"] = state
        write_status(kw)

    try:
        n = max(1, len(clip_jobs))
        for i, (sn, url) in enumerate(clip_jobs.items()):
            put("running", progress=0.05 * i / n, msg=f"تحميل مقاطع الفيديو {i + 1}/{n}")
            try:
                dest = os.path.join(vdir, hashlib.md5(url.encode()).hexdigest()[:12] + ".mp4")
                assets.setdefault(sn, {})["clip_path"] = E.download_file(url, dest)
            except Exception:
                assets.setdefault(sn, {})["clip_path"] = None
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)
        E.render_project(project, assets, cfg, tl, music_path, out, work_dir, font_dir, master_audio_path=master_audio,
                         progress=lambda f, m: put("running", progress=0.05 + 0.95 * f, msg=m))
        put("done", progress=1.0, out=out, msg="الفيديو جاهز")
    except Exception as e:
        put("error", msg=str(e)[:600])


def render_panel():
    stt = read_status()
    state = stt.get("state")
    if state == "running" and not render_alive() and time.time() - stt.get("ts", 0) > 60:
        state = "stalled"
    if state == "running":
        st.progress(min(1.0, float(stt.get("progress", 0))), text=stt.get("msg", "…"))
        st.caption("التركيب خدام فالخلفية. تقدر تنتظر هنا أو تسد الصفحة وترجع من بعد بنفس الرابط.")
    elif state == "done" and stt.get("out") and os.path.exists(stt["out"]):
        if ss.get("final_path") != stt["out"]:
            ss.final_path = stt["out"]
            save_state()
            st.rerun()
        show_final(stt["out"])
    elif state == "error":
        st.error(f"وقع مشكل أثناء التركيب: {stt.get('msg', '')}")
    elif state == "stalled":
        st.error("توقف التركيب فجأة، غالباً السيرفر المجاني فرغت ذاكرتو. نقّص الجودة لـ 720p أو 480p، أو قصّر الفيديو، وعاود.")
    elif ss.get("final_path") and os.path.exists(ss.final_path):
        show_final(ss.final_path)


def show_final(path):
    st.success(f"✅ الفيديو النهائي جاهز · {E.media_duration(path):.0f} ثانية · {os.path.getsize(path) / 1e6:.1f} MB")
    st.video(path)
    with open(path, "rb") as f:
        st.download_button("⬇️ تحميل الفيديو (MP4)", f, file_name="ai_studio_video.mp4", mime="video/mp4",
                           width="stretch", key=f"dl_{os.path.basename(path)}")


# ============================================================
# الشريط العلوي + الخطوات
# ============================================================
st.markdown(
    f"""<div class="topbar"><div class="logo">🎬</div>
    <div class="brand"><div class="brand-t">استوديو الذكاء الاصطناعي</div><span>Darija · Amazigh · Video Studio</span></div>
    <div class="chip">{ss.cfg_language} · {target()} ث · {ss.cfg_aspect.split(' ')[0]}</div></div>""",
    unsafe_allow_html=True)

with st.container(key="stepper"):
    cols = st.columns(5)
    for i, (name, ico) in enumerate(STEPS, 1):
        done = "✓ " if i < ss.step else ""
        cols[i - 1].button(f"{ico}  {done}{name}", key=f"st{i}", on_click=go, args=(i,),
                           type="primary" if ss.step == i else "secondary", width="stretch")

st.write("")
main, side = st.columns([3.1, 1.15], gap="large")

# ============================================================
# لوحة الملخص (يمين)
# ============================================================
with side:
    with st.container(key="summary"):
        sc = scenes()
        if sc:
            tl = timeline()
            ok_v = sum(1 for s in sc if A(s["scene_number"]).get("audio_path")) if ss.voice_mode == "per_scene" else (len(sc) if master_dur() else 0)
            ok_s = sum(1 for s in sc if any(e.get("path") for e in A(s["scene_number"]).get("sfx", [])))
            ok_c = sum(1 for s in sc if A(s["scene_number"]).get("clip_path") or A(s["scene_number"]).get("clip_idx", -1) >= 0)
            segs = "".join(
                f"<i style='flex:{max(it['dur'], 0.5):.2f}' title='المشهد {it['n']} · {it['dur']:.1f}s'>{it['n']}</i>"
                for it in tl["items"])
            tot_cls = "warn" if tl["overflow"] else "ok"
            st.markdown(f"""<div class="sum-card"><h4>{ss.project.get('title', '')}</h4>
            <div class="tl">{segs}</div>
            <div class="sum-row"><span>المدة النهائية</span><b class="{tot_cls}">{tl['total']:.0f} ث / {target()} ث</b></div>
            <div class="sum-row"><span>المشاهد</span><b>{len(sc)}</b></div>
            <div class="sum-row"><span>التعليق الصوتي</span><b class="{'ok' if ok_v == len(sc) else 'no'}">{ok_v}/{len(sc)}</b></div>
            <div class="sum-row"><span>المؤثرات</span><b class="{'ok' if ok_s else 'no'}">{ok_s}/{len(sc)}</b></div>
            <div class="sum-row"><span>الموسيقى</span><b class="{'ok' if ss.music_path else 'no'}">{'✓' if ss.music_path else '—'}</b></div>
            <div class="sum-row"><span>مقاطع الفيديو</span><b class="{'ok' if ok_c == len(sc) else 'no'}">{ok_c}/{len(sc)}</b></div>
            </div>""", unsafe_allow_html=True)
            if tl["overflow"]:
                st.warning("الحوار أطول من المدة المطلوبة، قصّر النصوص أو زد المدة.")
        else:
            st.markdown("""<div class="sum-card"><h4>ملخص المشروع</h4>
            <div class="sum-row"><span>ابدأ بكتابة فكرة الفيديو، والمشاهد غادي تظهر هنا.</span></div></div>""",
                        unsafe_allow_html=True)

with main:
    # ========================================================
    # 1) السيناريو
    # ========================================================
    if ss.step == 1:
        head("السيناريو", "اكتب فكرة أو ألصق سيناريو جاهز، والذكاء الاصطناعي كيقسّمه لمشاهد بمدد ومؤثرات مناسبة لكل مشهد.")
        mode = st.radio("طريقة العمل", ["فكرة جديدة", "سيناريو جاهز"], horizontal=True, label_visibility="collapsed")
        with st.container(border=True):
            if mode == "فكرة جديدة":
                ss.topic = st.text_area("فكرة الفيديو", ss.topic, key="w_topic", height=130,
                                        placeholder="مثال: ناجي وحيد كيتجول فالصحراء بعد نهاية العالم وكيلقى رسالة غريبة…")
            else:
                ss.last_custom_script = st.text_area("السيناريو", ss.last_custom_script, key="w_custom", height=200)
            topic = ss.topic if mode == "فكرة جديدة" else ""
            custom = ss.last_custom_script if mode == "سيناريو جاهز" else ""
            go_btn = st.button("🚀 توليد السيناريو", type="primary", width="stretch")
        if go_btn:
            if not key("gemini"):
                st.error("دخل Google Gemini API Key من الشريط الجانبي (🔑 المفاتيح).")
            elif not (topic.strip() or custom.strip()):
                st.error("اكتب الفكرة أو ألصق السيناريو أولاً.")
            else:
                with st.spinner("جاري كتابة السيناريو وتحديد المؤثرات لكل مشهد…"):
                    try:
                        prompt = E.build_script_prompt(ss.cfg_language, ss.cfg_tone, topic, custom,
                                                       target(), PACES[ss.cfg_pace])
                        models = [ss.cfg_script_model] + [m for m in E.GEMINI_SCRIPT_FALLBACKS if m != ss.cfg_script_model]
                        data, used = E.gemini_json(key("gemini"), models, prompt)
                        ss.project = E.normalize_script(data, target())
                        ss.assets = {}
                        ss.gen_id += 1
                        ss.music_prompt = ss.project["music_prompt"]
                        ss.music_query, ss.music_results = "", []
                        ss.music_path = None
                        ss.master_audio_path = None
                        ss.final_path = None
                        st.toast(f"السيناريو جاهز ({used})", icon="✅")
                    except Exception as e:
                        st.error(f"خطأ: {e}")

        if ss.project:
            sc = scenes()
            st.markdown(f"#### 🎬 {ss.project['title']}")
            tot = sum(s["duration"] for s in sc)
            st.caption(f"{len(sc)} مشهد · المجموع {tot:.0f} ثانية (المطلوب {target()}). تقدر تعدّل أي شيء قبل ما تكمل.")
            gid = ss.gen_id
            for s in sc:
                n = s["scene_number"]
                with st.container(border=True):
                    top = st.columns([0.7, 4, 1.3])
                    top[0].markdown(f"<span class='scene-n'>{n}</span>", unsafe_allow_html=True)
                    top[1].markdown(f"**{s['location'] or '—'}** — {s['visual_description']}")
                    s["duration"] = top[2].number_input("المدة (ث)", 2.0, 30.0, float(s["duration"]), 0.5,
                                                         key=f"dur_{gid}_{n}", label_visibility="collapsed")
                    s["dialogue"] = st.text_area("الحوار / التعليق", s["dialogue"], key=f"dlg_{gid}_{n}", height=90)
                    c1, c2 = st.columns(2)
                    s["search_keyword"] = c1.text_input("كلمة البحث عن الفيديو (EN)", s["search_keyword"], key=f"kw_{gid}_{n}")
                    cur = ", ".join(e["keyword"] for e in s["sfx"])
                    new = c2.text_input("المؤثرات الصوتية (EN، مفصولة بفاصلة)", cur, key=f"sfx_{gid}_{n}")
                    if new != cur:
                        old = {e["keyword"]: e for e in s["sfx"]}
                        kws = [k.strip() for k in new.split(",") if k.strip()][:4]
                        s["sfx"] = [old.get(k) or {"keyword": k, "description": k, "at": round(i / max(1, len(kws)) * 0.8, 2),
                                                   "enabled": True} for i, k in enumerate(kws)]
            ss.music_prompt = st.text_area("وصف الموسيقى الخلفية (يُستعمل في الخطوة 4)", ss.music_prompt, height=80)
            nav(nxt=2, nxt_label="التعليق الصوتي")

    # ========================================================
    # 2) التعليق الصوتي
    # ========================================================
    elif ss.step == 2:
        head("التعليق الصوتي", "صوت لكل مشهد على حدة لتزامن مضبوط، أو ارفع ملفاً صوتياً كاملاً.")
        if not ss.project:
            st.info("ابدأ من الخطوة 1 لتوليد السيناريو.")
            nav(prev=1)
        else:
            vm = st.radio("المصدر", ["توليد بالذكاء الاصطناعي (لكل مشهد)", "رفع ملف صوتي كامل"], horizontal=True,
                          index=0 if ss.voice_mode == "per_scene" else 1, label_visibility="collapsed")
            ss.voice_mode = "per_scene" if vm.startswith("توليد") else "master"
            if ss.voice_mode == "per_scene":
                kind = "gemini" if ss.cfg_voice_engine.startswith("Gemini") else \
                    "hf" if ss.cfg_voice_engine.startswith("Chatter") else "edge"
                notes = {
                    "gemini": "Gemini TTS: أقرب صوت لـ Google AI Studio، يفهم اللهجة والأسلوب. الحصة المجانية صغيرة "
                              "(حوالي 10 طلبات في اليوم)، لذلك التطبيق يجمّع عدة مشاهد في طلب واحد ويقطّع الصوت تلقائياً.",
                    "hf": "Chatterbox (رخصة MIT) عبر Hugging Face: مجاني، يدعم العربية الفصحى (ماشي الدارجة)، وعلى حصة GPU محدودة.",
                    "edge": "Edge TTS: سريع وبلا مفتاح، لكن صوته أقل طبيعية، وهو واجهة غير رسمية (الأفضل تفاديه للاستعمال التجاري).",
                }
                st.caption(f"المحرك الحالي: **{ss.cfg_voice_engine}**. {notes[kind]} (تغيّره من الشريط الجانبي)")
                if kind == "gemini" and not key("gemini"):
                    st.warning("ما كاين مفتاح Gemini، غادي نرجعو لـ Edge TTS تلقائياً.")
                cA, cB = st.columns([2, 1])
                run_all = cA.button("🎙️ توليد أصوات المشاهد الناقصة", type="primary", width="stretch")
                redo_all = cB.button("إعادة توليد الكل", width="stretch")

                def set_voice(s, path, eng):
                    a = A(s["scene_number"])
                    a["audio_path"] = path
                    a["audio_dur"] = E.media_duration(path)
                    a["audio_engine"] = eng

                def edge_one(s):
                    fem, mal = E.EDGE_VOICES[ss.cfg_language]
                    out = os.path.join(ADIR, f"voice_{s['scene_number']}_{int(time.time() * 1000)}.mp3")
                    E.tts_edge(s["dialogue"].strip(), fem if ss.cfg_gender == "أنثى" else mal, ss.cfg_voice_rate, out)
                    set_voice(s, out, "edge")

                def group_scenes(scs, max_chars=1300, max_n=8):
                    groups, cur, chars = [], [], 0
                    for s in scs:
                        n = len(s["dialogue"])
                        if cur and (chars + n > max_chars or len(cur) >= max_n):
                            groups.append(cur)
                            cur, chars = [], 0
                        cur.append(s)
                        chars += n
                    if cur:
                        groups.append(cur)
                    return groups

                def gen_voices(todo, stt):
                    todo = [s for s in todo if s["dialogue"].strip()]
                    errors = []
                    remaining = list(todo)
                    if kind == "gemini" and key("gemini"):
                        models = [ss.cfg_tts_model] + [m for m in E.GEMINI_TTS_MODELS if m != ss.cfg_tts_model]
                        failed, quota_out = [], False
                        for g in group_scenes(remaining):
                            if quota_out:
                                failed += g
                                continue
                            try:
                                stt.write(f"Gemini: المشاهد {g[0]['scene_number']}–{g[-1]['scene_number']} في طلب واحد")
                                raw = os.path.join(ADIR, f"gem_{int(time.time() * 1000)}.wav")
                                E.tts_gemini(key("gemini"), models, "\n\n".join(s["dialogue"].strip() for s in g),
                                             ss.cfg_gemini_voice, ss.cfg_language, raw, style=ss.cfg_tts_style,
                                             multi=len(g) > 1, log=stt.write)
                                parts = [raw] if len(g) == 1 else E.split_audio_by_text(
                                    raw, [s["dialogue"] for s in g], raw[:-4])
                                for s, p in zip(g, parts):
                                    set_voice(s, p, "gemini")
                            except Exception as e:
                                stt.write(f"⚠️ {str(e)[:200]}")
                                quota_out = "الحصة اليومية" in str(e)
                                failed += g
                        remaining = failed
                    elif kind == "hf":
                        failed = []
                        for s in remaining:
                            try:
                                stt.write(f"Chatterbox: المشهد {s['scene_number']}")
                                p, used = E.tts_hf([x.strip() for x in ss.cfg_tts_spaces.splitlines() if x.strip()],
                                                   s["dialogue"].strip(), ss.cfg_language, ADIR,
                                                   key("hf") or None, log=stt.write)
                                set_voice(s, p, "chatterbox")
                            except Exception as e:
                                stt.write(f"⚠️ {str(e)[:160]}")
                                failed.append(s)
                        remaining = failed
                    if remaining:
                        stt.write(f"Edge TTS للمشاهد المتبقية ({len(remaining)})…")
                    for s in remaining:
                        try:
                            edge_one(s)
                        except Exception as e:
                            errors.append(f"المشهد {s['scene_number']}: {str(e)[:120]}")
                    return errors

                if run_all or redo_all:
                    todo = [s for s in scenes() if redo_all or not A(s["scene_number"]).get("audio_path")]
                    with st.status("جاري توليد الأصوات…", expanded=True) as stt:
                        errs = gen_voices(todo, stt)
                        for e in errs:
                            stt.write(f"❌ {e}")
                        stt.update(label="تم" if not errs else f"انتهى مع {len(errs)} أخطاء",
                                   state="complete" if not errs else "error")
                for s in scenes():
                    n = s["scene_number"]
                    a = A(n)
                    with st.container(border=True):
                        c1, c2 = st.columns([3, 1])
                        c1.markdown(f"<span class='scene-n'>{n}</span> {s['dialogue']}", unsafe_allow_html=True)
                        if c2.button("↻ إعادة", key=f"re_{n}"):
                            with st.status("…", expanded=True) as stt:
                                errs = gen_voices([s], stt)
                                for e in errs:
                                    st.error(e)
                            if not errs:
                                st.rerun()
                        if a.get("audio_path") and os.path.exists(a["audio_path"]):
                            st.audio(a["audio_path"])
                            st.caption(f"{a['audio_dur']:.1f} ث · {a.get('audio_engine', '')}")
            else:
                st.caption("الملف الصوتي كيتقسّم على المشاهد حسب طول نص كل مشهد، والفيديو كيخرج بنفس مدة الملف.")
                up = st.file_uploader("ملف الصوت (MP3 / WAV)", type=["mp3", "wav", "m4a"])
                if up:
                    p = os.path.join(ADIR, "master" + os.path.splitext(up.name)[1])
                    with open(p, "wb") as f:
                        f.write(up.getbuffer())
                    ss.master_audio_path = p
                if ss.master_audio_path and os.path.exists(ss.master_audio_path):
                    st.audio(ss.master_audio_path)
                    st.caption(f"المدة: {E.media_duration(ss.master_audio_path):.1f} ث")
            nav(prev=1, nxt=3, nxt_label="المؤثرات")

    # ========================================================
    # 3) المؤثرات الصوتية
    # ========================================================
    elif ss.step == 3:
        head("المؤثرات الصوتية", "كل مشهد فيه المؤثرات اللي حددها السيناريو، وكتتحط في اللحظة المناسبة.")
        if not ss.project:
            st.info("ابدأ من الخطوة 1.")
            nav(prev=2)
        else:
            ss.sfx_mode = st.radio("مصدر المؤثرات", SFX_MODES, index=SFX_MODES.index(ss.sfx_mode)
                                   if ss.sfx_mode in SFX_MODES else 0)
            st.caption("المكتبة الحقيقية (مفتاح مجاني)، أو توليد بالذكاء الاصطناعي، أو توليد محلي مضمون يخدم دائماً.")
            run = st.button("🔊 جلب / توليد المؤثرات لكل المشاهد", type="primary", width="stretch")

            def get_sfx(e, n, i, status):
                mode = ss.sfx_mode
                order = (["fs", "ai", "local"] if mode.startswith("تلقائي") else
                         ["fs"] if mode.startswith("Freesound") else
                         ["ai"] if mode.startswith("ذكاء") else ["local"])
                for src in order:
                    try:
                        if src == "fs":
                            r = E.search_freesound(e["keyword"], key("freesound"))
                            if r:
                                dest = os.path.join(ADIR, f"sfx_{n}_{i}.mp3")
                                E.download_file(r["url"], dest)
                                return dest, "Freesound"
                        elif src == "ai":
                            sp = sfx_spaces()
                            if sp:
                                p, used = E.hf_audio(sp, e["description"] or e["keyword"], 3, ADIR, key("hf") or None,
                                                     log=status.write)
                                return p, "AI · " + used.split("/")[-1]
                        else:
                            dest = os.path.join(ADIR, f"sfx_{n}_{i}.wav")
                            E.write_wav(dest, E.synth_sfx(e["keyword"] + " " + e["description"], 3.0))
                            return dest, "توليد محلي"
                    except Exception as ex:
                        status.write(f"⚠️ {src}: {str(ex)[:150]}")
                return None, ""

            if run:
                with st.status("جاري تجهيز المؤثرات…", expanded=True) as stt:
                    sc = scenes()
                    total = sum(len(s["sfx"]) for s in sc) or 1
                    done = 0
                    prog = st.progress(0.0)
                    for s in sc:
                        n = s["scene_number"]
                        a = A(n)
                        for i, e in enumerate(s["sfx"]):
                            stt.write(f"المشهد {n}: {e['keyword']}")
                            p, src = get_sfx(e, n, i, stt)
                            e["path"], e["source"] = p, src
                            done += 1
                            prog.progress(done / total)
                        a["sfx"] = s["sfx"]
                    stt.update(label="المؤثرات جاهزة", state="complete")
            for s in scenes():
                n = s["scene_number"]
                with st.container(border=True):
                    st.markdown(f"<span class='scene-n'>{n}</span> {s['location'] or ''}  "
                                f"<span class='pill'>{s['duration']:.0f} ث</span>", unsafe_allow_html=True)
                    if not s["sfx"]:
                        st.caption("بلا مؤثرات في هذا المشهد.")
                    for i, e in enumerate(s["sfx"]):
                        c = st.columns([2.2, 1.4, 1, 1])
                        c[0].markdown(f"**{e['keyword']}**  \n<small>{e['description']}</small>", unsafe_allow_html=True)
                        if e.get("path") and os.path.exists(e["path"]):
                            c[1].audio(e["path"])
                            c[1].caption(e.get("source", ""))
                        else:
                            c[1].caption("لم يُجلب بعد")
                        e["at"] = c[2].slider("البداية", 0.0, 0.9, float(e["at"]), 0.05, key=f"at_{ss.gen_id}_{n}_{i}")
                        e["enabled"] = c[3].checkbox("استعمال", e.get("enabled", True), key=f"en_{ss.gen_id}_{n}_{i}")
                A(n)["sfx"] = s["sfx"]
            nav(prev=2, nxt=4, nxt_label="الموسيقى")

    # ========================================================
    # 4) الموسيقى
    # ========================================================
    elif ss.step == 4:
        head("الموسيقى الخلفية", "موسيقى بلا حقوق وآمنة للاستعمال التجاري: بالذكاء الاصطناعي، أو من مكتبة CC0، أو ملفك.")
        notes = {
            M_AUTO: "كيجرب ACE-Step، وإلا فمكتبة CC0، وإلا التوليد المحلي. ديما كتخرج موسيقى.",
            M_ACE: "ACE-Step 1.5 (رخصة MIT): مجاني، يسمح باستعمال الموسيقى المولّدة تجارياً. يحتاج مفتاح مجاني من acemusic.ai.",
            M_LIB: "Freesound (CC0) و Openverse (CC0 / Public Domain): بلا إسناد ولا حقوق، آمنة للنشر والاستعمال التجاري.",
            M_HF: "Spaces على Hugging Face (غير مضمونة، تتغير بزاف). تأكد من رخصة الموديل قبل الاستعمال التجاري.",
            M_LOCAL: "توليد محلي بسيط (بدون إنترنت). حقوقو ديالك 100%.",
            M_UP: "ارفع موسيقى تملك حقوقها أو بلا حقوق (مثل Pixabay Music أو YouTube Audio Library).",
            M_NONE: "الفيديو غادي يخرج بدون موسيقى.",
        }
        with st.container(border=True):
            ss.music_mode = st.radio("المصدر", MUSIC_MODES, index=MUSIC_MODES.index(ss.music_mode))
            m = ss.music_mode
            st.caption(notes[m])
            if m == M_UP:
                up = st.file_uploader("ملف الموسيقى (MP3 / WAV)", type=["mp3", "wav", "m4a"])
                if up:
                    p = os.path.join(ADIR, "music_upload" + os.path.splitext(up.name)[1])
                    with open(p, "wb") as f:
                        f.write(up.getbuffer())
                    ss.music_path, ss.music_info = p, "ملف مرفوع"
            elif m == M_NONE:
                ss.music_path, ss.music_info = None, ""
            else:
                if m in (M_AUTO, M_ACE, M_HF, M_LOCAL):
                    ss.music_prompt = st.text_area("وصف الموسيقى (EN أحسن)", ss.music_prompt, height=100,
                                                   placeholder="calm lo-fi beat, soft piano, 80 BPM, warm and nostalgic")
                if m in (M_AUTO, M_LIB):
                    if not ss.music_query:
                        ss.music_query = E.suggest_music_query(ss.music_prompt)
                    ss.music_query = st.text_input("كلمات البحث في المكتبة (EN)", ss.music_query)
                go_music = st.button("🔎 البحث في المكتبة" if m == M_LIB else "✨ توليد الموسيقى", type="primary",
                                     width="stretch")
                if go_music:
                    prompt = ss.music_prompt.strip() or "calm cinematic instrumental background music"
                    dur = min(120, max(30, target() + 5))
                    order = {M_AUTO: ["ace", "lib", "local"], M_ACE: ["ace"], M_LIB: ["lib_list"],
                             M_HF: ["hf"], M_LOCAL: ["local"]}[m]
                    result = None
                    with st.status("جاري التحضير…", expanded=True) as stt:
                        for src in order:
                            try:
                                if src == "ace":
                                    stt.write("ACE-Step (سحابي)… قد ياخد دقيقة")
                                    data, ext = E.ace_music(prompt, dur, key("ace"))
                                    p = os.path.join(ADIR, f"music_{int(time.time())}.{ext}")
                                    with open(p, "wb") as f:
                                        f.write(data)
                                    result = (p, "ACE-Step (MIT)")
                                elif src in ("lib", "lib_list"):
                                    stt.write("البحث في مكتبة CC0…")
                                    res, errs = E.search_music_library(ss.music_query, key("freesound"))
                                    ss.music_results = res
                                    for er in errs:
                                        stt.write(f"⚠️ {er}")
                                    if src == "lib":
                                        pick = next((r for r in res if r["duration"] >= 20), None)
                                        if not pick:
                                            raise RuntimeError("ما لقينا مسارات")
                                        p = E.download_file(pick["url"], os.path.join(ADIR, f"music_lib_{int(time.time())}.mp3"))
                                        result = (p, f"{pick['source']} · {pick['license']} · {pick['creator']}")
                                    else:
                                        if not res:
                                            raise RuntimeError("ما لقينا مسارات، جرّب كلمات أخرى")
                                        result = "listed"
                                elif src == "hf":
                                    p, used = E.hf_audio(music_spaces(), prompt, 30, ADIR, key("hf") or None, log=stt.write)
                                    result = (p, "Hugging Face · " + used)
                                else:
                                    stt.write("توليد محلي…")
                                    p = os.path.join(ADIR, f"music_{int(time.time())}.wav")
                                    E.write_wav(p, E.synth_music(prompt, 45, ss.cfg_tone))
                                    result = (p, "توليد محلي")
                                break
                            except Exception as e:
                                stt.write(f"⚠️ {src}: {str(e)[:240]}")
                        if result == "listed":
                            stt.update(label="اختر مساراً من النتائج تحت", state="complete")
                        elif result:
                            ss.music_path, ss.music_info = result
                            stt.update(label=f"جاهزة · {result[1]}", state="complete")
                        else:
                            stt.update(label="فشل التوليد، جرّب مصدراً آخر أو افحص الخدمات من الشريط الجانبي", state="error")
        if ss.music_results and ss.music_mode in (M_AUTO, M_LIB):
            st.markdown("##### 🎼 نتائج المكتبة (CC0 / Public Domain)")
            for i, r in enumerate(ss.music_results[:8]):
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1])
                    c1.markdown(f"**{r['title'] or '—'}**  \n<small>{r['source']} · {r['license']} · {r['creator']} · "
                                f"{r['duration']:.0f} ث</small>", unsafe_allow_html=True)
                    if c2.button("استعمال", key=f"use_{i}", width="stretch"):
                        try:
                            ext = r["url"].split("?")[0].rsplit(".", 1)[-1].lower()
                            ext = ext if ext in ("mp3", "wav", "ogg", "flac", "m4a") else "mp3"
                            p = E.download_file(r["url"], os.path.join(ADIR, f"music_lib_{i}.{ext}"))
                            ss.music_path, ss.music_info = p, f"{r['source']} · {r['license']} · {r['creator']}"
                            st.rerun()
                        except Exception as e:
                            st.error(f"تعذر التحميل: {e}")
                    st.audio(r["url"])
        if ss.music_path and os.path.exists(ss.music_path):
            st.success("🎵 الموسيقى المختارة")
            st.audio(ss.music_path)
            st.caption(f"{ss.music_info} · {E.media_duration(ss.music_path):.0f} ث (كتتكرر تلقائياً إلى نهاية الفيديو)")
        nav(prev=3, nxt=5, nxt_label="المونتاج")

    # ========================================================
    # 5) المونتاج
    # ========================================================
    else:
        head("المونتاج النهائي", "ابدأ التركيب، والفيديو كيبان هنا (فوق) مع زر التحميل. بعدها تقدر تبدّل المقاطع.")
        if not ss.project:
            st.info("ابدأ من الخطوة 1.")
            nav(prev=4)
        else:
            orient = {"9:16": "portrait", "4:5": "portrait", "16:9": "landscape", "4:3": "landscape", "1:1": "square"}[
                ss.cfg_aspect.split(" ")[0]]
            has_stock_key = bool(key("pexels") or key("pixabay"))
            if not has_stock_key:
                st.warning("ما كاين حتى مفتاح للفيديو. دخل Pexels أو Pixabay (مجانيين) وإلا غادي تكون الخلفيات متدرجة.")

            def thumbs_for(scs):
                tasks = [o for s in scs for o in A(s["scene_number"]).get("stock", [])[:4]]

                def work(o):
                    if o["source"] == "Pexels" and o.get("thumb"):
                        return
                    try:
                        src = o.get("preview") or o["url"]
                        dest = os.path.join(VDIR, "th_" + hashlib.md5(src.encode()).hexdigest()[:12] + ".jpg")
                        o["thumb"] = E.make_thumb(src, dest)
                    except Exception:
                        pass

                with ThreadPoolExecutor(6) as ex:
                    list(ex.map(work, tasks))

            def fetch_stock(scs, q_override=None):
                msgs = []
                for s in scs:
                    a = A(s["scene_number"])
                    res, errs = E.find_clips(q_override or s["search_keyword"], orient, key("pexels"), key("pixabay"), 6)
                    a["stock"], a["clip_idx"], a["clip_path"] = res, (0 if res else -1), None
                    msgs += errs
                thumbs_for(scs)
                return msgs

            need = [s for s in scenes() if "stock" not in A(s["scene_number"])]
            if need and has_stock_key:
                with st.status("جاري البحث عن المقاطع…", expanded=False) as stt:
                    for er in fetch_stock(need):
                        stt.write(er)
                    stt.update(label="المقاطع جاهزة", state="complete")

            # ---------- بطاقة التصدير (فوق) ----------
            tl = timeline()
            with st.container(border=True):
                st.markdown(f"**الملخص:** {len(scenes())} مشهد · مدة الفيديو **{tl['total']:.0f} ث** · "
                            f"{ss.cfg_quality} · {ss.cfg_aspect.split(' ')[0]} · {'مع موسيقى' if ss.music_path else 'بدون موسيقى'}")
                if tl["speed"] > 1.001:
                    st.caption(f"سيتم تسريع التعليق الصوتي بنسبة {tl['speed']:.2f}× ليناسب المدة المطلوبة.")
                if tl["overflow"]:
                    st.warning(f"الحوار يحتاج {tl['total']:.0f} ث وهو أطول من {target()} ث. قصّر النصوص أو زد المدة.")
                if ss.cfg_quality == "1080p" and tl["total"] > 60:
                    st.warning("1080p لفيديو طويل ثقيل على السيرفر المجاني وقد يتوقف. الأضمن 720p.")
                busy = read_status().get("state") == "running" and render_alive()
                render = st.button("🎬 بدء التركيب النهائي", type="primary", width="stretch", disabled=busy)
                if render:
                    voice_missing = [s["scene_number"] for s in scenes() if ss.voice_mode == "per_scene"
                                     and not A(s["scene_number"]).get("audio_path") and s["dialogue"].strip()]
                    if ss.voice_mode == "master" and not master_dur():
                        st.error("ما رفعتيش الملف الصوتي (الخطوة 2).")
                    elif voice_missing:
                        st.error(f"ما كاينش تعليق صوتي للمشاهد: {voice_missing}. ولّده من الخطوة 2.")
                    else:
                        W, H = size()
                        cfg = dict(W=W, H=H, fps=int(ss.cfg_fps), captions=ss.cfg_captions, cap_size=int(ss.cfg_cap_size),
                                   cap_color=ss.cfg_cap_color, cap_pos=ss.cfg_cap_pos, watermark=ss.cfg_watermark.strip(),
                                   vol_voice=ss.cfg_vol_voice / 100, vol_sfx=ss.cfg_vol_sfx / 100,
                                   vol_music=ss.cfg_vol_music / 100, duck=ss.cfg_duck)
                        assets_c = copy.deepcopy(ss.assets)
                        jobs = {}
                        for s in scenes():
                            sn = str(s["scene_number"])
                            a = assets_c.setdefault(sn, {})
                            a["sfx"] = copy.deepcopy(s["sfx"])
                            idx, opts = a.get("clip_idx", -1), a.get("stock", [])
                            a["clip_path"] = None
                            if idx is not None and 0 <= idx < len(opts):
                                jobs[sn] = opts[idx]["url"]
                        out = os.path.join(PDIR, f"final_{int(time.time())}.mp4")
                        write_status({"state": "running", "progress": 0.0, "msg": "بدء التحضير…"})
                        threading.Thread(
                            target=_render_worker, name=f"render-{pid}", daemon=True,
                            args=(copy.deepcopy(ss.project), assets_c, cfg, tl, ss.music_path, out, WDIR, FONT_DIR,
                                  ss.master_audio_path if tl["master"] else None, jobs, VDIR, STATUS_FILE)).start()
                        st.rerun()
                running = read_status().get("state") == "running" and render_alive()
                st.fragment(render_panel, run_every=2 if running else None)()

            # ---------- المقاطع ----------
            hc1, hc2 = st.columns([3, 1])
            hc1.markdown("#### 🎞️ مقاطع الفيديو لكل مشهد")
            if hc2.button("↻ إعادة البحث للكل", width="stretch", disabled=not has_stock_key):
                with st.spinner("…"):
                    fetch_stock(scenes())
                st.rerun()
            for s in scenes():
                n = s["scene_number"]
                a = A(n)
                opts = a.get("stock", [])[:4]
                with st.container(border=True):
                    h = st.columns([4, 2])
                    h[0].markdown(f"<span class='scene-n'>{n}</span> **{s['search_keyword']}** "
                                  f"<span class='pill'>{s['duration']:.0f} ث</span>", unsafe_allow_html=True)
                    q = h[1].text_input("بحث", "", key=f"q_{n}", placeholder="كلمة أخرى…", label_visibility="collapsed")
                    if q and q != a.get("last_q") and has_stock_key:
                        a["last_q"] = q
                        fetch_stock([s], q)
                        opts = a.get("stock", [])[:4]
                    if opts:
                        cols = st.columns(len(opts))
                        for i, o in enumerate(opts):
                            if o.get("thumb"):
                                cols[i].image(o["thumb"], width="stretch")
                            else:
                                cols[i].caption("🎞️ بلا صورة")
                            cols[i].caption(f"{i + 1} · {o['source']}" + (f" · {o['duration']}s" if o.get("duration") else ""))
                        labels = ["بدون فيديو"] + [str(i + 1) for i in range(len(opts))]
                        cur = a.get("clip_idx", 0)
                        sel = st.radio("المقطع", labels, index=(cur + 1) if 0 <= cur < len(opts) else 0,
                                       horizontal=True, key=f"sel_{n}", label_visibility="collapsed")
                        a["clip_idx"] = -1 if sel == "بدون فيديو" else int(sel) - 1
                        if a["clip_idx"] >= 0 and st.toggle("▶ معاينة المقطع", key=f"pv_{n}"):
                            st.video(opts[a["clip_idx"]].get("preview") or opts[a["clip_idx"]]["url"])
                    else:
                        st.caption("ما لقيناش مقطع. غادي نستعملو خلفية متدرجة (جرّب كلمة بحث أخرى).")
                        a["clip_idx"] = -1
            nav(prev=4)

save_state()
