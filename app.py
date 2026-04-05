"""
AI Trading Journal — Streamlit Prototype
Analyzes your crypto futures trades with AI and shows you what you're doing wrong.
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import calendar
import uuid
import os
import json
import html as _html
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
try:
    import extra_streamlit_components as stx
    _COOKIES_AVAILABLE = True
except ImportError:
    _COOKIES_AVAILABLE = False

# --- Config ---
st.set_page_config(
    page_title="AI Trading Coach",
    page_icon="https://em-content.zobj.net/source/apple/391/chart-increasing_1f4c8.png",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load .env locally (ignored on Streamlit Cloud)
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

def _get_secret(key, fallback=""):
    """Read from Streamlit Secrets (cloud) or env (local)."""
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, fallback)

# --- Cookie Manager (session persistence) ---
if _COOKIES_AVAILABLE:
    _cookie_mgr = stx.CookieManager(key="tcj_cookies")
else:
    _cookie_mgr = None

def _save_session_cookies(access_token, refresh_token, user_id, user_email):
    if _cookie_mgr is None:
        return
    try:
        _cookie_mgr.set("tcj_access_token", access_token, key="set_at")
        _cookie_mgr.set("tcj_refresh_token", refresh_token, key="set_rt")
        _cookie_mgr.set("tcj_user_id", user_id, key="set_uid")
        _cookie_mgr.set("tcj_user_email", user_email, key="set_em")
    except Exception:
        pass

def _clear_session_cookies():
    if _cookie_mgr is None:
        return
    for name, key in [("tcj_access_token","del_at"),("tcj_refresh_token","del_rt"),("tcj_user_id","del_uid"),("tcj_user_email","del_em")]:
        try:
            _cookie_mgr.delete(name, key=key)
        except Exception:
            pass

def _restore_session_from_cookies():
    """Try to restore session from cookies. Returns True if successful."""
    if _cookie_mgr is None or 'sb_access_token' in st.session_state:
        return 'sb_access_token' in st.session_state
    try:
        cookies = _cookie_mgr.get_all()
        token = cookies.get("tcj_access_token", "")
        uid = cookies.get("tcj_user_id", "")
        email = cookies.get("tcj_user_email", "")
        if token and uid:
            st.session_state.sb_access_token = token
            st.session_state.sb_refresh_token = cookies.get("tcj_refresh_token", "")
            st.session_state.sb_user_id = uid
            st.session_state.sb_user_email = email
            return True
    except Exception:
        pass
    return False

# --- Supabase REST helpers (no SDK, avoids httpx/h2 issues on Python 3.14) ---
_SB_URL = _get_secret("SUPABASE_URL").strip().rstrip("/")
_SB_KEY = _get_secret("SUPABASE_KEY").strip()
if not _SB_URL or not _SB_KEY:
    st.error("Supabase credentials not configured. Add SUPABASE_URL and SUPABASE_KEY to Streamlit Secrets.")
    st.stop()

def _sb_headers(token=None, extra=None):
    h = {"apikey": _SB_KEY, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if extra:
        h.update(extra)
    return h

def _http(method, url, headers=None, body=None, timeout=15):
    """Pure stdlib HTTP — works on every Python version, no dependencies."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            try:
                return json.loads(content), resp.status
            except Exception:
                return {}, resp.status
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return json.loads(content), e.code
        except Exception:
            return {"error_description": str(e)}, e.code
    except Exception as e:
        return {"error_description": f"Cannot reach Supabase: {e}"}, 503

def _sb_signup(email, password):
    return _http("POST", f"{_SB_URL}/auth/v1/signup", _sb_headers(),
                 {"email": email, "password": password})

def _sb_login(email, password):
    return _http("POST", f"{_SB_URL}/auth/v1/token?grant_type=password", _sb_headers(),
                 {"email": email, "password": password})

def _sb_logout(token):
    try:
        _http("POST", f"{_SB_URL}/auth/v1/logout", _sb_headers(token), timeout=10)
    except Exception:
        pass

def _sb_refresh_token(refresh_token):
    """Get a new access token using the refresh token."""
    data, code = _http("POST", f"{_SB_URL}/auth/v1/token?grant_type=refresh_token",
                       _sb_headers(), {"refresh_token": refresh_token})
    if code == 200 and "access_token" in data:
        return data["access_token"], data.get("refresh_token", refresh_token)
    return None, None

def _ensure_valid_token():
    """Refresh token if needed. Returns True if session is valid."""
    token = st.session_state.get("sb_access_token", "")
    refresh = st.session_state.get("sb_refresh_token", "")
    if not token:
        return False
    # Test token with a lightweight call
    _, code = _http("GET", f"{_SB_URL}/auth/v1/user", _sb_headers(token))
    if code == 200:
        return True
    # Token expired — try refresh
    if refresh:
        new_token, new_refresh = _sb_refresh_token(refresh)
        if new_token:
            st.session_state.sb_access_token = new_token
            st.session_state.sb_refresh_token = new_refresh
            _save_session_cookies(new_token, new_refresh,
                                  st.session_state.get("sb_user_id", ""),
                                  st.session_state.get("sb_user_email", ""))
            return True
    return False

def _valid_uuid(val):
    """Reject non-UUID values before they reach DB URLs."""
    import re
    return bool(re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', str(val or ''), re.I))

def _sb_get_trades(user_id, token):
    if not _valid_uuid(user_id):
        return []
    data, code = _http("GET",
        f"{_SB_URL}/rest/v1/journal_trades?user_id=eq.{user_id}&order=sort_order.asc",
        _sb_headers(token, {"Accept": "application/json"}))
    return data if isinstance(data, list) else []

def _sb_delete_trades(user_id, token):
    if not _valid_uuid(user_id):
        return
    _http("DELETE", f"{_SB_URL}/rest/v1/journal_trades?user_id=eq.{user_id}",
          _sb_headers(token))

def _sb_insert_trades(rows, token):
    _http("POST", f"{_SB_URL}/rest/v1/journal_trades",
          _sb_headers(token, {"Prefer": "return=minimal"}), rows)

def _sb_upload_export(file_bytes, filename, user_id, token):
    """Upload a trade export file to Supabase Storage, return (path, error_msg)."""
    import mimetypes
    safe_filename = filename.replace(" ", "_")
    content_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
    path = f"{user_id}/{safe_filename}"
    url = f"{_SB_URL}/storage/v1/object/Trade%20export/{urllib.parse.quote(path)}"
    headers = {
        "apikey": _SB_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return path, None
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            msg = json.loads(body).get("message") or json.loads(body).get("error") or str(body)
        except Exception:
            msg = str(body)
        return None, f"HTTP {e.code}: {msg}"
    except Exception as e:
        return None, str(e)

def _sb_list_exports(user_id, token):
    """List trade export files for this user. Returns list of {name, path} dicts."""
    url = f"{_SB_URL}/storage/v1/object/list/Trade%20export"
    body = {"prefix": f"{user_id}/", "limit": 100}
    data, code = _http("POST", url, _sb_headers(token), body)
    if isinstance(data, list):
        return [{"name": item["name"], "path": f"{user_id}/{item['name']}"} for item in data if "name" in item]
    return []

def _sb_download_export(path, token):
    """Download a trade export file, return bytes or None."""
    url = f"{_SB_URL}/storage/v1/object/Trade%20export/{urllib.parse.quote(path)}"
    headers = {"apikey": _SB_KEY, "Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None

def _sb_delete_export(path, token):
    """Delete a trade export file from storage."""
    url = f"{_SB_URL}/storage/v1/object/Trade%20export/{urllib.parse.quote(path)}"
    headers = {"apikey": _SB_KEY, "Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception:
        pass

def _sb_upload_screenshot(file_bytes, filename, trade_id, user_id, token):
    """Upload image to Supabase Storage, return public URL or None."""
    import mimetypes
    safe_filename = filename.replace(" ", "_")
    content_type = mimetypes.guess_type(safe_filename)[0] or "image/jpeg"
    path = f"{user_id}/{trade_id}/{safe_filename}"
    url = f"{_SB_URL}/storage/v1/object/Trade%20screenshot/{urllib.parse.quote(path)}"
    headers = {
        "apikey": _SB_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return f"{_SB_URL}/storage/v1/object/public/Trade%20screenshot/{urllib.parse.quote(path)}"
    except Exception:
        return None

def _download_image_bytes(url):
    """Download image from URL, return (bytes, mime_type) or (None, None)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            return resp.read(), content_type
    except Exception:
        return None, None

def _collect_trade_screenshots(trades):
    """Download all screenshots from journal trades, return list of (bytes, mime_type, trade_name)."""
    images = []
    for t in trades:
        for url in t.get("screenshots", []):
            img_bytes, mime = _download_image_bytes(url)
            if img_bytes:
                images.append((img_bytes, mime, t.get("name", "Trade")))
    return images


# --- Color Palette ---
COLORS = {
    'bg_dark': '#0a0e17',
    'bg_card': '#111827',
    'bg_card_hover': '#1a2332',
    'border': '#1e293b',
    'accent_blue': '#3b82f6',
    'accent_cyan': '#06b6d4',
    'accent_purple': '#8b5cf6',
    'green': '#10b981',
    'red': '#ef4444',
    'yellow': '#f59e0b',
    'text': '#e2e8f0',
    'text_dim': '#94a3b8',       # slate-400 — gut lesbar auf dunklem BG
    'text_bright': '#f8fafc',
}

# --- Futuristic CSS ---
st.markdown(f"""
<style>
    /* === GLOBAL === */
    .stApp {{
        background: linear-gradient(180deg, {COLORS['bg_dark']} 0%, #0d1321 50%, {COLORS['bg_dark']} 100%);
        color: {COLORS['text']};
    }}

    /* Main content area */
    .main .block-container {{
        padding-top: 2rem;
        max-width: 1400px;
    }}

    /* === HEADER === */
    .hero-title {{
        font-size: 2.4rem;
        font-weight: 800;
        background: linear-gradient(135deg, {COLORS['accent_cyan']} 0%, {COLORS['accent_blue']} 50%, {COLORS['accent_purple']} 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.02em;
        margin-bottom: 0;
        line-height: 1.2;
    }}
    .hero-subtitle {{
        color: {COLORS['text']};
        font-size: 1rem;
        margin-top: 4px;
        margin-bottom: 1.5rem;
    }}

    /* === METRIC CARDS === */
    [data-testid="stMetric"] {{
        background: linear-gradient(145deg, {COLORS['bg_card']} 0%, {COLORS['bg_card_hover']} 100%);
        border: 1px solid {COLORS['border']};
        border-radius: 16px;
        padding: 20px 24px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.03);
        transition: all 0.3s ease;
    }}
    [data-testid="stMetric"]:hover {{
        border-color: rgba(59, 130, 246, 0.35);
        box-shadow: 0 4px 32px rgba(59, 130, 246, 0.15), inset 0 1px 0 rgba(255, 255, 255, 0.05);
        transform: translateY(-2px);
    }}
    [data-testid="stMetric"] label {{
        color: {COLORS['text']} !important;
        font-size: 0.8rem !important;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 600;
    }}
    [data-testid="stMetric"] [data-testid="stMetricValue"] {{
        color: {COLORS['text_bright']} !important;
        font-size: 1.6rem !important;
        font-weight: 700;
    }}
    [data-testid="stMetric"] [data-testid="stMetricDelta"] {{
        font-size: 0.8rem !important;
    }}

    /* === TABS === */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 0;
        background: {COLORS['bg_card']};
        border-radius: 14px;
        padding: 4px;
        border: 1px solid {COLORS['border']};
    }}
    .stTabs [data-baseweb="tab"] {{
        border-radius: 10px;
        color: {COLORS['text']};
        font-weight: 500;
        padding: 10px 20px;
        font-size: 0.9rem;
    }}
    .stTabs [data-baseweb="tab"]:hover {{
        color: {COLORS['text_bright']} !important;
        background: rgba(255, 255, 255, 0.05);
    }}
    .stTabs [aria-selected="true"] {{
        background: linear-gradient(135deg, rgba(59, 130, 246, 0.15) 0%, rgba(139, 92, 246, 0.15) 100%) !important;
        color: {COLORS['text_bright']} !important;
        border: 1px solid rgba(59, 130, 246, 0.3) !important;
    }}
    .stTabs [data-baseweb="tab-highlight"] {{
        display: none;
    }}
    .stTabs [data-baseweb="tab-border"] {{
        display: none;
    }}

    /* === SIDEBAR === */
    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, #0d1117 0%, #0a0e17 100%);
        border-right: 1px solid {COLORS['border']};
    }}
    [data-testid="stSidebar"] .stMarkdown h2 {{
        color: {COLORS['text_bright']};
        font-size: 1rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }}

    /* === BUTTONS === */
    .stButton > button {{
        color: {COLORS['text_bright']} !important;
        background: {COLORS['bg_card']} !important;
        border: 1px solid {COLORS['border']} !important;
        border-radius: 12px;
    }}
    .stButton > button:hover {{
        color: {COLORS['text_bright']} !important;
        background: {COLORS['bg_card_hover']} !important;
        border-color: rgba(59, 130, 246, 0.3) !important;
    }}
    .stButton > button[kind="primary"] {{
        background: linear-gradient(135deg, {COLORS['accent_blue']} 0%, {COLORS['accent_purple']} 100%);
        color: #ffffff !important;
        border: none;
        border-radius: 12px;
        font-weight: 600;
        font-size: 1rem;
        padding: 14px 28px;
        transition: all 0.3s ease;
        box-shadow: 0 4px 16px rgba(59, 130, 246, 0.3);
    }}
    .stButton > button[kind="primary"]:hover {{
        color: #ffffff !important;
        box-shadow: 0 6px 24px rgba(59, 130, 246, 0.5);
        transform: translateY(-2px);
    }}

    /* === GENERAL TEXT OVERRIDES === */
    .stMarkdown, .stMarkdown p, .stMarkdown li, .stMarkdown td, .stMarkdown th {{
        color: {COLORS['text']} !important;
    }}
    .stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4 {{
        color: {COLORS['text_bright']} !important;
    }}
    .stMarkdown strong {{
        color: {COLORS['text_bright']} !important;
    }}
    .stMarkdown a {{
        color: {COLORS['accent_cyan']} !important;
    }}

    /* Checkbox & selectbox text */
    .stCheckbox label span, .stSelectbox label {{
        color: {COLORS['text']} !important;
    }}
    /* Sidebar radio labels */
    [data-testid="stSidebar"] .stRadio label, [data-testid="stSidebar"] .stRadio label p,
    [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label span {{
        color: {COLORS['text_bright']} !important;
    }}
    /* Sidebar general text */
    [data-testid="stSidebar"] .stCheckbox label span,
    [data-testid="stSidebar"] .stSelectbox label,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] label {{
        color: {COLORS['text_bright']} !important;
    }}
    /* Selectbox styling */
    [data-baseweb="select"] {{
        background: {COLORS['bg_card']} !important;
        border: 1px solid {COLORS['border']} !important;
        border-radius: 12px !important;
    }}
    [data-baseweb="select"] > div {{
        background: {COLORS['bg_card']} !important;
        color: {COLORS['text']} !important;
        border: none !important;
    }}
    [data-baseweb="select"] span {{
        color: {COLORS['text']} !important;
    }}
    [data-baseweb="select"] svg {{
        fill: {COLORS['text_dim']} !important;
    }}
    /* Dropdown menu */
    [data-baseweb="popover"] {{
        background: {COLORS['bg_card']} !important;
        border: 1px solid {COLORS['border']} !important;
        border-radius: 12px !important;
    }}
    [data-baseweb="popover"] li {{
        color: {COLORS['text']} !important;
        background: {COLORS['bg_card']} !important;
    }}
    [data-baseweb="popover"] li:hover {{
        background: {COLORS['bg_card_hover']} !important;
    }}
    [role="option"] {{
        color: {COLORS['text']} !important;
    }}
    [aria-selected="true"] {{
        background: rgba(59, 130, 246, 0.15) !important;
    }}

    /* File uploader text */
    [data-testid="stFileUploader"] label {{
        color: {COLORS['text']} !important;
    }}
    [data-testid="stFileUploader"] span {{
        color: {COLORS['text_dim']} !important;
    }}

    /* Data editor outer wrapper */
    [data-testid="stDataEditor"] > div {{
        border-radius: 16px !important;
        border: 1px solid rgba(6, 182, 212, 0.35) !important;
        box-shadow: 0 0 0 1px rgba(6, 182, 212, 0.08), 0 4px 24px rgba(0,0,0,0.4) !important;
        overflow: hidden !important;
    }}

    /* Success/Warning/Info boxes */
    .stAlert {{
        border-radius: 12px;
    }}

    /* Spinner text */
    .stSpinner > div {{
        color: {COLORS['text']} !important;
    }}

    /* === DIVIDERS === */
    hr {{
        border-color: {COLORS['border']} !important;
        margin: 1.5rem 0 !important;
    }}

    /* === EXPANDER === */
    [data-testid="stExpander"] {{
        background: {COLORS['bg_card']};
        border: 1px solid {COLORS['border']};
        border-radius: 12px;
        overflow: hidden;
    }}
    [data-testid="stExpander"] summary {{
        background: {COLORS['bg_card']} !important;
        color: {COLORS['text_bright']} !important;
        padding: 12px 16px;
    }}
    [data-testid="stExpander"] summary:hover {{
        background: {COLORS['bg_card_hover']} !important;
        color: {COLORS['text_bright']} !important;
    }}
    [data-testid="stExpander"] summary span {{
        color: {COLORS['text_bright']} !important;
    }}
    [data-testid="stExpander"] summary svg {{
        fill: {COLORS['text']} !important;
        color: {COLORS['text']} !important;
    }}
    [data-testid="stExpander"] [data-testid="stExpanderDetails"] {{
        background: {COLORS['bg_dark']};
        border-top: 1px solid {COLORS['border']};
    }}
    /* Legacy class fallback */
    .streamlit-expanderHeader {{
        background: {COLORS['bg_card']} !important;
        border: 1px solid {COLORS['border']};
        border-radius: 12px;
        color: {COLORS['text_bright']} !important;
    }}
    .streamlit-expanderHeader:hover {{
        background: {COLORS['bg_card_hover']} !important;
        color: {COLORS['text_bright']} !important;
    }}

    /* === PLOTLY CHARTS — override backgrounds === */
    .stPlotlyChart {{
        border: 1px solid {COLORS['border']};
        border-radius: 16px;
        overflow: hidden;
    }}

    /* === ANALYSIS CONTAINER === */
    .analysis-box {{
        background: {COLORS['bg_card']};
        border: 1px solid {COLORS['border']};
        border-radius: 16px;
        padding: 32px;
        margin-top: 16px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
    }}
    .analysis-box table {{
        border-collapse: collapse;
        width: 100%;
    }}
    .analysis-box th, .analysis-box td {{
        border: 1px solid rgba(255, 255, 255, 0.2) !important;
        padding: 10px 14px !important;
        color: {COLORS['text']} !important;
    }}
    .analysis-box th {{
        background: rgba(255, 255, 255, 0.05) !important;
        color: {COLORS['text_bright']} !important;
        font-weight: 600;
    }}

    /* General table styling (also for AI output outside analysis-box) */
    .stMarkdown table {{
        border-collapse: collapse;
        width: 100%;
    }}
    .stMarkdown th, .stMarkdown td {{
        border: 1px solid rgba(255, 255, 255, 0.2) !important;
        padding: 10px 14px !important;
        color: {COLORS['text']} !important;
    }}
    .stMarkdown th {{
        background: rgba(255, 255, 255, 0.05) !important;
        color: {COLORS['text_bright']} !important;
        font-weight: 600;
    }}

    /* === LONG/SHORT CARDS === */
    .direction-card {{
        background: {COLORS['bg_card']};
        border: 1px solid {COLORS['border']};
        border-radius: 16px;
        padding: 24px;
        text-align: center;
    }}
    .direction-long {{ border-left: 3px solid {COLORS['green']}; }}
    .direction-short {{ border-left: 3px solid {COLORS['red']}; }}

    /* === INFO BOX === */
    .landing-card {{
        background: linear-gradient(145deg, {COLORS['bg_card']} 0%, {COLORS['bg_card_hover']} 100%);
        border: 1px solid {COLORS['border']};
        border-radius: 20px;
        padding: 48px;
        text-align: center;
        margin: 40px auto;
        max-width: 700px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
    }}
    .landing-card h3 {{
        color: {COLORS['text_bright']};
        font-size: 1.4rem;
        margin-bottom: 16px;
    }}
    .landing-card p {{
        color: {COLORS['text']};
        font-size: 0.95rem;
        line-height: 1.7;
    }}

    /* === CHAT INPUT === */
    [data-testid="stChatInput"] {{
        background: {COLORS['bg_card']} !important;
        border: 1px solid {COLORS['border']} !important;
        border-radius: 14px !important;
    }}
    [data-testid="stChatInput"] textarea,
    [data-testid="stChatInput"] input,
    [data-testid="stChatInput"] div[contenteditable],
    .stChatInput textarea,
    .stChatInput input {{
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
        background: transparent !important;
        caret-color: #ffffff !important;
    }}
    [data-testid="stChatInput"] textarea::placeholder,
    .stChatInput textarea::placeholder {{
        color: {COLORS['text_dim']} !important;
        -webkit-text-fill-color: {COLORS['text_dim']} !important;
    }}
    [data-testid="stChatInput"] button {{
        color: {COLORS['accent_cyan']} !important;
    }}
    /* Bottom chat input bar */
    .stChatFloatingInputContainer {{
        background: {COLORS['bg_dark']} !important;
        border-top: 1px solid {COLORS['border']} !important;
    }}
    [data-testid="stBottom"] {{
        background: {COLORS['bg_dark']} !important;
    }}

    /* === SCROLLBAR === */
    ::-webkit-scrollbar {{ width: 6px; }}
    ::-webkit-scrollbar-track {{ background: {COLORS['bg_dark']}; }}
    ::-webkit-scrollbar-thumb {{ background: {COLORS['border']}; border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: {COLORS['text_dim']}; }}

    /* === HIDE STREAMLIT DEFAULTS === */
    #MainMenu {{ visibility: hidden; }}
    footer {{ visibility: hidden; }}
    header {{ visibility: hidden; }}
</style>
""", unsafe_allow_html=True)


# --- Plotly Theme ---
PLOTLY_LAYOUT = dict(
    template='none',
    paper_bgcolor='#111827',
    plot_bgcolor='#0a0e17',
    font=dict(color='#e2e8f0', family='Inter, system-ui, sans-serif', size=13),
    title=dict(font=dict(size=17, color='#ffffff', family='Inter, system-ui, sans-serif')),
    xaxis=dict(
        gridcolor='rgba(255,255,255,0.06)',
        zerolinecolor='rgba(255,255,255,0.12)',
        tickfont=dict(color='#cbd5e1', size=12),
        title_font=dict(color='#cbd5e1'),
        linecolor='rgba(255,255,255,0.1)',
    ),
    yaxis=dict(
        gridcolor='rgba(255,255,255,0.06)',
        zerolinecolor='rgba(255,255,255,0.12)',
        tickfont=dict(color='#cbd5e1', size=12),
        title_font=dict(color='#cbd5e1'),
        linecolor='rgba(255,255,255,0.1)',
    ),
    margin=dict(l=50, r=20, t=50, b=40),
    hoverlabel=dict(bgcolor='#1e293b', bordercolor='#334155', font=dict(color='#f8fafc', size=13)),
    colorway=['#06b6d4', '#3b82f6', '#8b5cf6', '#10b981', '#ef4444', '#f59e0b'],
)


# --- Data Loading ---
def parse_trades(df):
    """Parse trading data from broker export (Bitget/Bybit format)."""
    trades = df.copy()

    # Clean PNL and Fee columns (remove 'USDT' suffix)
    trades['pnl'] = trades['Realized PNL'].astype(str).str.replace('USDT', '').astype(float)
    trades['fee'] = trades['Fee'].astype(str).str.replace('USDT', '').astype(float)

    # Parse datetime
    trades['open_time'] = pd.to_datetime(trades['Open Time(UTC+02:00)'])
    trades['close_time'] = pd.to_datetime(trades['Close Time'])

    # Derived columns
    trades['duration_min'] = (trades['close_time'] - trades['open_time']).dt.total_seconds() / 60
    trades['weekday'] = trades['open_time'].dt.day_name()
    trades['hour'] = trades['open_time'].dt.hour
    trades['date'] = trades['open_time'].dt.date
    trades['week'] = trades['open_time'].dt.isocalendar().week
    trades['month'] = trades['open_time'].dt.to_period('M').astype(str)
    trades['is_win'] = trades['pnl'] > 0
    trades['direction'] = trades['Direction']
    trades['asset'] = trades['Futures'].str.replace('USDT', '')

    # Sort by time
    trades = trades.sort_values('open_time').reset_index(drop=True)

    return trades


def compute_stats(trades):
    """Compute trading statistics for the AI prompt."""
    stats = {}

    # Overall
    stats['total_trades'] = len(trades)
    stats['total_pnl'] = round(trades['pnl'].sum(), 2)
    stats['total_fees'] = round(trades['fee'].sum(), 2)
    stats['win_rate'] = round((trades['is_win'].sum() / len(trades)) * 100, 1)
    stats['avg_win'] = round(trades[trades['is_win']]['pnl'].mean(), 4)
    stats['avg_loss'] = round(trades[~trades['is_win']]['pnl'].mean(), 4)
    stats['best_trade'] = round(trades['pnl'].max(), 4)
    stats['worst_trade'] = round(trades['pnl'].min(), 4)
    stats['risk_reward'] = round(abs(stats['avg_win'] / stats['avg_loss']), 2) if stats['avg_loss'] != 0 else 0
    stats['avg_duration_min'] = round(trades['duration_min'].mean(), 1)

    # By direction
    for d in ['Long', 'Short']:
        dt = trades[trades['direction'] == d]
        if len(dt) > 0:
            stats[f'{d.lower()}_count'] = len(dt)
            stats[f'{d.lower()}_winrate'] = round((dt['is_win'].sum() / len(dt)) * 100, 1)
            stats[f'{d.lower()}_pnl'] = round(dt['pnl'].sum(), 2)

    # By weekday
    weekday_stats = trades.groupby('weekday').agg(
        trades=('pnl', 'count'),
        pnl=('pnl', 'sum'),
        winrate=('is_win', 'mean')
    ).round(2)
    stats['weekday_stats'] = weekday_stats.to_dict()

    # By hour
    hour_stats = trades.groupby('hour').agg(
        trades=('pnl', 'count'),
        pnl=('pnl', 'sum'),
        winrate=('is_win', 'mean')
    ).round(2)
    stats['hour_stats'] = hour_stats.to_dict()

    # By asset (top 10)
    asset_stats = trades.groupby('asset').agg(
        trades=('pnl', 'count'),
        pnl=('pnl', 'sum'),
        winrate=('is_win', 'mean')
    ).sort_values('trades', ascending=False).head(10).round(2)
    stats['asset_stats'] = asset_stats.to_dict()

    # Revenge trading detection: trades opened within 5 min of a losing trade close
    revenge_trades = []
    for i in range(1, len(trades)):
        prev = trades.iloc[i-1]
        curr = trades.iloc[i]
        if prev['pnl'] < 0:
            gap = (curr['open_time'] - prev['close_time']).total_seconds() / 60
            if 0 <= gap <= 5:
                revenge_trades.append({
                    'date': str(curr['date']),
                    'asset': curr['asset'],
                    'pnl': curr['pnl'],
                    'gap_min': round(gap, 1)
                })
    stats['revenge_trades'] = revenge_trades
    stats['revenge_count'] = len(revenge_trades)

    # Losing streaks
    streaks = []
    current_streak = 0
    for _, t in trades.iterrows():
        if t['pnl'] <= 0:
            current_streak += 1
        else:
            if current_streak >= 3:
                streaks.append(current_streak)
            current_streak = 0
    stats['losing_streaks'] = streaks
    stats['max_losing_streak'] = max(streaks) if streaks else 0

    # Monthly PNL
    monthly = trades.groupby('month')['pnl'].sum().round(2)
    stats['monthly_pnl'] = monthly.to_dict()

    return stats


def build_ai_prompt(stats, trades):
    """Build the prompt for the AI Trading Coach."""

    # Load system prompt
    prompt_path = Path(__file__).parent / "trading_coach_prompt.md"
    system_prompt = prompt_path.read_text(encoding='utf-8')

    # Build data summary
    data_summary = f"""
## Trading Data Summary

**Period:** {trades['date'].min()} to {trades['date'].max()}
**Total:** {stats['total_trades']} Trades

### Performance
- Total PNL: {stats['total_pnl']} USDT
- Total Fees: {stats['total_fees']} USDT
- Win Rate: {stats['win_rate']}%
- Avg Win: {stats['avg_win']} USDT | Avg Loss: {stats['avg_loss']} USDT
- Risk-Reward Ratio: {stats['risk_reward']}
- Best Trade: {stats['best_trade']} USDT | Worst Trade: {stats['worst_trade']} USDT
- Avg Trade Duration: {stats['avg_duration_min']} minutes

### Long vs Short
- Long: {stats.get('long_count', 0)} Trades, Win Rate {stats.get('long_winrate', 0)}%, PNL {stats.get('long_pnl', 0)} USDT
- Short: {stats.get('short_count', 0)} Trades, Win Rate {stats.get('short_winrate', 0)}%, PNL {stats.get('short_pnl', 0)} USDT

### Performance by Weekday
"""
    weekday_data = stats['weekday_stats']
    for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
        if day in weekday_data['trades']:
            data_summary += f"- {day}: {weekday_data['trades'][day]} Trades, PNL {weekday_data['pnl'][day]} USDT, Win Rate {round(weekday_data['winrate'][day]*100,1)}%\n"

    data_summary += "\n### Performance by Time of Day\n"
    hour_data = stats['hour_stats']
    for h in sorted(hour_data['trades'].keys()):
        data_summary += f"- {h}:00: {hour_data['trades'][h]} Trades, PNL {hour_data['pnl'][h]} USDT, Win Rate {round(hour_data['winrate'][h]*100,1)}%\n"

    data_summary += "\n### Top Assets\n"
    asset_data = stats['asset_stats']
    for asset in asset_data['trades']:
        data_summary += f"- {asset}: {asset_data['trades'][asset]} Trades, PNL {asset_data['pnl'][asset]} USDT, Win Rate {round(asset_data['winrate'][asset]*100,1)}%\n"

    data_summary += f"\n### Emotional Patterns\n"
    data_summary += f"- Revenge Trades (within 5 min after a loss): {stats['revenge_count']}\n"
    if stats['revenge_trades']:
        for rt in stats['revenge_trades'][:10]:
            data_summary += f"  - {rt['date']}: {rt['asset']}, PNL {rt['pnl']} USDT, Gap {rt['gap_min']} Min\n"
    data_summary += f"- Max Losing Streak: {stats['max_losing_streak']}\n"
    data_summary += f"- Losing Streaks (3+): {len(stats['losing_streaks'])} times\n"

    data_summary += f"\n### Monthly PNL\n"
    for month, pnl in stats['monthly_pnl'].items():
        data_summary += f"- {month}: {pnl} USDT\n"

    data_summary += """

---

Analyze this data now. Follow EXACTLY the format from your system prompt.
All 8 sections are mandatory. Skip NONE. Section 8 (Focus Plan) is the most important — it contains 5 concrete instructions the trader must change IMMEDIATELY.
"""

    return system_prompt, data_summary


def build_journal_ai_prompt(journal_trades):
    """Build AI prompt from journal trade data."""
    prompt_path = Path(__file__).parent / "trading_coach_prompt.md"
    system_prompt = prompt_path.read_text(encoding='utf-8')

    jt = journal_trades
    total = len(jt)
    if total == 0:
        data_summary = "No trades logged in the journal yet. Ask the trader to log some trades first."
        return system_prompt, data_summary

    wins = [t for t in jt if t.get('profit_loss') == 'Profit']
    losses = [t for t in jt if t.get('profit_loss') == 'Loss']
    win_rate = round(len(wins) / total * 100, 1)
    total_pnl = round(sum(t.get('gross_pnl', 0) for t in jt), 2)
    total_fees = round(sum(t.get('fees', 0) for t in jt), 2)
    avg_win = round(sum(t.get('gross_pnl', 0) for t in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(t.get('gross_pnl', 0) for t in losses) / len(losses), 2) if losses else 0
    best = max((t.get('gross_pnl', 0) for t in jt), default=0)
    worst = min((t.get('gross_pnl', 0) for t in jt), default=0)

    longs = [t for t in jt if t.get('direction') == 'Long']
    shorts = [t for t in jt if t.get('direction') == 'Short']
    long_wr = round(sum(1 for t in longs if t.get('profit_loss') == 'Profit') / len(longs) * 100, 1) if longs else 0
    short_wr = round(sum(1 for t in shorts if t.get('profit_loss') == 'Profit') / len(shorts) * 100, 1) if shorts else 0
    long_pnl = round(sum(t.get('gross_pnl', 0) for t in longs), 2)
    short_pnl = round(sum(t.get('gross_pnl', 0) for t in shorts), 2)

    data_summary = f"""## Journal Trade Data Summary

**Total Trades:** {total}
**Win Rate:** {win_rate}%
**Total PnL:** {total_pnl} USDT
**Total Fees:** {total_fees} USDT
**Avg Win:** {avg_win} USDT | **Avg Loss:** {avg_loss} USDT
**Best Trade:** {best} USDT | **Worst Trade:** {worst} USDT

### Long vs Short
- Long: {len(longs)} Trades, Win Rate {long_wr}%, PnL {long_pnl} USDT
- Short: {len(shorts)} Trades, Win Rate {short_wr}%, PnL {short_pnl} USDT

### Performance by Strategy
"""
    strat_stats = {}
    for t in jt:
        s = t.get('strategy') or 'Unknown'
        if s not in strat_stats:
            strat_stats[s] = {'trades': 0, 'wins': 0, 'pnl': 0}
        strat_stats[s]['trades'] += 1
        strat_stats[s]['pnl'] += t.get('gross_pnl', 0)
        if t.get('profit_loss') == 'Profit':
            strat_stats[s]['wins'] += 1
    for s, d in strat_stats.items():
        wr = round(d['wins'] / d['trades'] * 100, 1)
        data_summary += f"- {s}: {d['trades']} Trades, Win Rate {wr}%, PnL {round(d['pnl'],2)} USDT\n"

    data_summary += "\n### Performance by Session\n"
    sess_stats = {}
    for t in jt:
        s = t.get('session') or 'Unknown'
        if s not in sess_stats:
            sess_stats[s] = {'trades': 0, 'wins': 0, 'pnl': 0}
        sess_stats[s]['trades'] += 1
        sess_stats[s]['pnl'] += t.get('gross_pnl', 0)
        if t.get('profit_loss') == 'Profit':
            sess_stats[s]['wins'] += 1
    for s, d in sess_stats.items():
        wr = round(d['wins'] / d['trades'] * 100, 1)
        data_summary += f"- {s}: {d['trades']} Trades, Win Rate {wr}%, PnL {round(d['pnl'],2)} USDT\n"

    data_summary += "\n### Performance by Pair\n"
    pair_stats = {}
    for t in jt:
        p = t.get('pair') or 'Unknown'
        if p not in pair_stats:
            pair_stats[p] = {'trades': 0, 'wins': 0, 'pnl': 0}
        pair_stats[p]['trades'] += 1
        pair_stats[p]['pnl'] += t.get('gross_pnl', 0)
        if t.get('profit_loss') == 'Profit':
            pair_stats[p]['wins'] += 1
    for p, d in sorted(pair_stats.items(), key=lambda x: -x[1]['pnl']):
        wr = round(d['wins'] / d['trades'] * 100, 1)
        data_summary += f"- {p}: {d['trades']} Trades, Win Rate {wr}%, PnL {round(d['pnl'],2)} USDT\n"

    data_summary += "\n### All Trades\n"
    for t in jt:
        data_summary += (
            f"- {t.get('name','?')} | {t.get('open','')} → {t.get('close','')} | "
            f"{t.get('pair','')} {t.get('direction','')} | "
            f"Session: {t.get('session','')} | Strategy: {t.get('strategy','')} | "
            f"PnL: {t.get('gross_pnl',0)} USDT | {t.get('profit_loss','')}"
        )
        if t.get('confluences'):
            data_summary += f" | Confluences: {', '.join(t['confluences'])}"
        if t.get('additions'):
            data_summary += f" | Notes: {t['additions']}"
        data_summary += "\n"

    data_summary += """
---

Analyze this journal data now. Follow EXACTLY the format from your system prompt.
All 8 sections are mandatory. Skip NONE. Section 8 (Focus Plan) is the most important.

If trade screenshots are provided, analyze them as part of the trade context — look at chart patterns, entry/exit points, market structure, and any visible mistakes or good decisions. Reference specific screenshots in your analysis where relevant.
"""
    return system_prompt, data_summary


def call_gemini_with_images(system_prompt, user_prompt, images):
    """Call Gemini 2.5 Flash with text + images. images = list of (bytes, mime_type, label)."""
    import google.generativeai as genai

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set."

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-2.5-flash',
        system_instruction=system_prompt
    )

    parts = []
    for img_bytes, mime_type, label in images:
        parts.append(f"\n[Screenshot: {label}]")
        parts.append(genai.types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
    parts.append(user_prompt)

    response = model.generate_content(
        parts,
        generation_config=genai.types.GenerationConfig(temperature=0.7, max_output_tokens=8000)
    )
    return response.text


def call_gemini(system_prompt, user_prompt):
    """Call Gemini 2.5 Flash for analysis."""
    import google.generativeai as genai

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set. Add it in Streamlit Secrets or .env!"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-2.5-flash',
        system_instruction=system_prompt
    )

    response = model.generate_content(
        user_prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.7,
            max_output_tokens=8000,
        )
    )
    return response.text


def call_gemini_chat(chat_history):
    """Call Gemini 2.5 Flash with full chat history."""
    import google.generativeai as genai

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set. Add it in Streamlit Secrets or .env!"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-2.5-flash',
        system_instruction=chat_history[0]['content']  # system prompt
    )

    # Build Gemini chat history (skip system prompt)
    gemini_history = []
    for msg in chat_history[1:-1]:  # skip system and last user message
        role = 'user' if msg['role'] == 'user' else 'model'
        gemini_history.append({'role': role, 'parts': [msg['content']]})

    chat = model.start_chat(history=gemini_history)

    # Send last user message
    last_msg = chat_history[-1]['content']
    response = chat.send_message(
        last_msg,
        generation_config=genai.types.GenerationConfig(
            temperature=0.7,
            max_output_tokens=4000,
        )
    )
    return response.text


# =====================================================
# SHARED ANALYTICS HELPERS
# =====================================================

def journal_to_trades_and_stats(jt):
    """Convert journal trade list into trades DataFrame + stats dict matching broker format."""
    if not jt:
        empty = pd.DataFrame(columns=['date', 'pnl', 'asset', 'is_win'])
        return empty, None

    rows = []
    for t in jt:
        try:
            dt = pd.to_datetime(t.get('close') or t.get('open'))
        except Exception:
            dt = pd.Timestamp.now()
        rows.append({
            'date': dt,
            'pnl': float(t.get('gross_pnl', 0)),
            'asset': t.get('pair', 'Unknown') or 'Unknown',
            'is_win': t.get('profit_loss') == 'Profit',
        })
    tdf = pd.DataFrame(rows)

    total = len(tdf)
    wins = tdf['is_win'].sum()
    s = {
        'total_trades': total,
        'total_pnl': round(tdf['pnl'].sum(), 2),
        'total_fees': round(sum(t.get('fees', 0) for t in jt), 2),
        'win_rate': round(wins / total * 100, 1) if total else 0,
        'best_trade': round(tdf['pnl'].max(), 2) if total else 0,
        'worst_trade': round(tdf['pnl'].min(), 2) if total else 0,
        'weekday_stats': {'trades': {}, 'pnl': {}, 'winrate': {}},
        'hour_stats': {'trades': {}, 'pnl': {}, 'winrate': {}},
        'asset_stats': {'trades': {}, 'pnl': {}, 'winrate': {}},
    }
    for _, row in tdf.iterrows():
        day = row['date'].strftime('%A')
        hr = row['date'].hour
        asset = row['asset']
        for key, val in [(day, s['weekday_stats']), (hr, s['hour_stats']), (asset, s['asset_stats'])]:
            val['trades'][key] = val['trades'].get(key, 0) + 1
            val['pnl'][key] = round(val['pnl'].get(key, 0) + row['pnl'], 2)
            if key not in val['winrate']:
                val['winrate'][key] = []
            val['winrate'][key].append(row['is_win'])
    for val in [s['weekday_stats'], s['hour_stats'], s['asset_stats']]:
        val['winrate'] = {k: round(sum(v) / len(v), 3) for k, v in val['winrate'].items()}
    return tdf, s


def render_analytics(trades_df, stats_dict, tab_prefix=''):
    """Render the shared analytics block (metrics + 3 tabs). Works for both journal and broker data."""
    has_data = stats_dict is not None and stats_dict['total_trades'] > 0

    # Metrics row
    _total_pnl = stats_dict['total_pnl'] if has_data else 0.0
    _total_trades = stats_dict['total_trades'] if has_data else 0
    _win_rate = stats_dict['win_rate'] if has_data else 0.0
    _best = round(stats_dict['best_trade'], 2) if has_data and stats_dict['best_trade'] > 0 else None
    _worst = round(stats_dict['worst_trade'], 2) if has_data and stats_dict['worst_trade'] < 0 else None

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Total PnL", f"{_total_pnl} $")
    mc2.metric("Trades", _total_trades)
    mc3.metric("Win Rate", f"{_win_rate}%")
    mc4.metric("Best Trade", f"+{_best:.2f} $" if _best is not None else "—")
    mc5.metric("Worst Trade", f"{_worst:.2f} $" if _worst is not None else "—")
    st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

    # --- Pre-compute month range and daily PnL (used by both tab1 and tab2) ---
    from datetime import date as _date_type
    _today = datetime.now()
    # Always provide last 12 months → next 3 months so every chart is navigable
    _base_months = set()
    for _i in range(-11, 4):
        _mo_offset = _today.month - 1 + _i
        _yr_base = _today.year + _mo_offset // 12
        _mo_base = _mo_offset % 12 + 1
        _base_months.add(f'{_yr_base}-{_mo_base:02d}')
    if has_data:
        _cal_df = trades_df.copy()
        _cal_df['_day'] = pd.to_datetime(_cal_df['date']).dt.date
        _daily_pnl = _cal_df.groupby('_day')['pnl'].sum().to_dict()
        _data_months = set(pd.to_datetime(_cal_df['date']).dt.to_period('M').astype(str).tolist())
        _months_avail = sorted(_base_months | _data_months)
    else:
        _daily_pnl = {}
        _months_avail = sorted(_base_months)
    _cur_month_str = _today.strftime('%Y-%m')
    _cal_default_idx = _months_avail.index(_cur_month_str) if _cur_month_str in _months_avail else len(_months_avail) - 1

    # Build monthly PnL lookup for the bar chart
    _month_pnl = {}
    for _d, _v in _daily_pnl.items():
        _mk = f'{_d.year}-{_d.month:02d}'
        _month_pnl[_mk] = round(_month_pnl.get(_mk, 0) + _v, 2)

    # Color scaling for calendar (relative to trader's own extremes)
    _pos_vals = [v for v in _daily_pnl.values() if v > 0]
    _neg_vals = [v for v in _daily_pnl.values() if v < 0]
    _max_profit = max(_pos_vals) if _pos_vals else 1.0
    _max_loss   = abs(min(_neg_vals)) if _neg_vals else 1.0

    def _day_color(pnl):
        if pnl is None or pnl == 0:
            return '#1e2d45'
        elif pnl > 0:
            # low → faded dark (#1e3328), high → vivid bright green (#00e676)
            t = min(pnl / _max_profit, 1.0)
            t = t ** 0.6  # ease-in: small values stay dark longer
            r = int(0x1e + (0x00 - 0x1e) * t)
            g = int(0x33 + (0xe6 - 0x33) * t)
            b = int(0x28 + (0x76 - 0x28) * t)
            return f'#{r:02x}{g:02x}{b:02x}'
        else:
            # low → faded dark (#331818), high → vivid bright red (#ff3333)
            t = min(abs(pnl) / _max_loss, 1.0)
            t = t ** 0.6
            r = int(0x33 + (0xff - 0x33) * t)
            g = int(0x18 + (0x33 - 0x18) * t)
            b = int(0x18 + (0x33 - 0x18) * t)
            return f'#{r:02x}{g:02x}{b:02x}'

    tab1, tab2, tab3 = st.tabs(["Equity Curve", "Time Analysis", "Assets"])

    with tab1:
        # Equity Curve
        if has_data:
            cum = trades_df.copy()
            cum['date'] = pd.to_datetime(cum['date'])
            cum = cum.sort_values('date').reset_index(drop=True)
            cum['Cumulative PnL'] = cum['pnl'].cumsum()
            _start_date = cum['date'].iloc[0] - timedelta(days=1)
            cum_pts = pd.concat([
                pd.DataFrame({'date': [_start_date], 'Cumulative PnL': [0], 'asset': ['Start']}),
                cum[['date', 'Cumulative PnL', 'asset']]
            ], ignore_index=True)
            fig_eq = go.Figure(go.Scatter(
                x=cum_pts['date'], y=cum_pts['Cumulative PnL'],
                mode='lines+markers',
                line=dict(color=COLORS['accent_cyan'], width=2.5),
                marker=dict(size=5, color=COLORS['accent_cyan']),
                fill='tozeroy', fillcolor='rgba(6,182,212,0.08)',
                hovertemplate='%{x|%Y-%m-%d}<br>Cumulative PnL: %{y:.2f} $<extra></extra>',
            ))
            # Ensure at least 1 week is always visible
            _eq_start = pd.to_datetime(cum_pts['date'].min())
            _eq_end   = pd.to_datetime(cum_pts['date'].max())
            if (_eq_end - _eq_start).days < 7:
                _eq_end = _eq_start + timedelta(days=7)
        else:
            _eq_start = _today - timedelta(days=7)
            _eq_end   = _today
            fig_eq = go.Figure(go.Scatter(
                x=[_eq_start, _eq_end], y=[0, 0], mode='lines',
                line=dict(color=COLORS['accent_cyan'], width=2)
            ))
        fig_eq.add_hline(y=0, line_dash="dot", line_color=COLORS['text_dim'], line_width=1)
        fig_eq.update_layout(**PLOTLY_LAYOUT, title_text='Equity Curve', height=350, showlegend=False)
        _eq_span_days = (_eq_end - _eq_start).days if hasattr(_eq_end - _eq_start, 'days') else (_eq_end - _eq_start).days
        if _eq_span_days <= 14:
            _eq_dtick, _eq_fmt = 86400000, '%d.%m'        # every day
        elif _eq_span_days <= 60:
            _eq_dtick, _eq_fmt = 7 * 86400000, '%d.%m'   # every week
        elif _eq_span_days <= 365:
            _eq_dtick, _eq_fmt = 'M1', '%b %Y'            # every month
        else:
            _eq_dtick, _eq_fmt = 'M3', '%b %Y'            # every quarter
        fig_eq.update_xaxes(title='Date', range=[str(_eq_start), str(_eq_end)],
                            dtick=_eq_dtick, tickformat=_eq_fmt, tickangle=0,
                            automargin=True)
        fig_eq.update_yaxes(title='Cumulative PnL ($)', ticksuffix=' $')
        st.plotly_chart(fig_eq, use_container_width=True)

        # --- Calendar Heatmap ---
        st.markdown("<p style='color:#ffffff;font-size:1rem;font-weight:600;margin-bottom:4px;'>Daily PnL Calendar</p>", unsafe_allow_html=True)

        # Session state for calendar navigation
        _yr_key = f'{tab_prefix}_cal_yr'
        _mo_key = f'{tab_prefix}_cal_mo'
        if _yr_key not in st.session_state:
            st.session_state[_yr_key] = _today.year
        if _mo_key not in st.session_state:
            st.session_state[_mo_key] = _today.month

        # Navigation row: [←] [Month Year] [→] [📅]
        _cn1, _cn2, _cn3, _cn4 = st.columns([1, 6, 1, 1])
        with _cn1:
            if st.button('◀', key=f'{tab_prefix}_cal_prev', use_container_width=True):
                _m = st.session_state[_mo_key] - 1
                if _m < 1:
                    _m = 12
                    st.session_state[_yr_key] -= 1
                st.session_state[_mo_key] = _m
                st.rerun()
        with _cn3:
            if st.button('▶', key=f'{tab_prefix}_cal_next', use_container_width=True):
                _m = st.session_state[_mo_key] + 1
                if _m > 12:
                    _m = 1
                    st.session_state[_yr_key] += 1
                st.session_state[_mo_key] = _m
                st.rerun()
        with _cn4:
            with st.popover('📅', use_container_width=True):
                st.markdown("<p style='color:#fff;font-size:0.85rem;font-weight:600;margin-bottom:6px;'>Jump to</p>", unsafe_allow_html=True)
                _yr_options = list(range(2020, 2031))
                _pick_yr = st.selectbox('Year', _yr_options,
                                        index=_yr_options.index(st.session_state[_yr_key]) if st.session_state[_yr_key] in _yr_options else 0,
                                        key=f'{tab_prefix}_pick_yr')
                _pick_mo = st.selectbox('Month', list(range(1, 13)),
                                        index=st.session_state[_mo_key] - 1,
                                        format_func=lambda x: datetime(2000, x, 1).strftime('%B'),
                                        key=f'{tab_prefix}_pick_mo')
                if st.button('Go', key=f'{tab_prefix}_pick_go', use_container_width=True):
                    st.session_state[_yr_key] = int(_pick_yr)
                    st.session_state[_mo_key] = int(_pick_mo)
                    st.rerun()
        with _cn2:
            _yr = st.session_state[_yr_key]
            _mo = st.session_state[_mo_key]
            st.markdown(
                f"<p style='text-align:center;color:#ffffff;font-size:1rem;font-weight:700;"
                f"margin:0;padding:6px 0;'>{datetime(_yr, _mo, 1).strftime('%B %Y')}</p>",
                unsafe_allow_html=True
            )

        _yr = st.session_state[_yr_key]
        _mo = st.session_state[_mo_key]
        _cal_weeks = calendar.monthcalendar(_yr, _mo)
        _day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

        # Build HTML calendar
        _header_cells = ''.join(f'<div style="color:#6b7280;font-size:0.72rem;text-align:center;padding:6px 0;font-weight:600;">{d}</div>' for d in _day_names)
        _week_rows = ''
        for _week in _cal_weeks:
            _week_rows += '<div style="display:contents;">'
            for _dn in _week:
                if _dn == 0:
                    _week_rows += '<div style="background:transparent;border-radius:8px;min-height:58px;"></div>'
                else:
                    _d = _date_type(_yr, _mo, _dn)
                    _pnl = _daily_pnl.get(_d, None)
                    _bg = _day_color(_pnl)
                    _pnl_str = f'{_pnl:+.2f} $' if _pnl is not None else ''
                    _txt_color = '#ffffff' if _pnl is not None else '#4a5568'
                    _dn_color = 'rgba(255,255,255,0.55)' if _pnl is not None else '#4a5568'
                    _week_rows += (
                        f'<div style="background:{_bg};border-radius:8px;min-height:58px;padding:6px 8px;'
                        f'display:flex;flex-direction:column;justify-content:space-between;"'
                        f' title="{_d}: {_pnl_str}">'
                        f'<span style="color:{_dn_color};font-size:0.7rem;font-weight:600;">{_dn}</span>'
                        f'<span style="color:{_txt_color};font-size:0.78rem;font-weight:700;text-align:right;">{_pnl_str}</span>'
                        f'</div>'
                    )
            _week_rows += '</div>'

        # Monthly summary for current displayed month
        _mo_days = {d: p for d, p in _daily_pnl.items() if d.year == _yr and d.month == _mo}
        _mo_total   = round(sum(_mo_days.values()), 2)
        _mo_trading = len(_mo_days)
        _mo_best    = round(max(_mo_days.values()), 2) if _mo_days else 0
        _mo_worst   = round(min(_mo_days.values()), 2) if _mo_days else 0
        # Wins/Losses counted per individual trade (not per day)
        if has_data:
            _mo_tdf = trades_df[pd.to_datetime(trades_df['date']).apply(
                lambda x: x.year == _yr and x.month == _mo)]
            _mo_wins   = int((_mo_tdf['pnl'] > 0).sum())
            _mo_losses = int((_mo_tdf['pnl'] < 0).sum())
        else:
            _mo_wins, _mo_losses = 0, 0
        _mo_total_trades = _mo_wins + _mo_losses
        _mo_wr = round(_mo_wins / _mo_total_trades * 100, 1) if _mo_total_trades else 0
        _mo_pnl_color = '#22c55e' if _mo_total > 0 else ('#ef4444' if _mo_total < 0 else '#6b7280')
        _mo_best_str  = f'+{_mo_best:.2f} $' if _mo_best > 0 else ('—')
        _mo_worst_str = f'{_mo_worst:.2f} $' if _mo_worst < 0 else ('—')

        def _summary_tile(label, value, value_color='#ffffff'):
            return (
                f'<div style="background:#1e2d45;border-radius:8px;padding:10px 14px;flex:1;min-width:0;">'
                f'<div style="color:#6b7280;font-size:0.7rem;font-weight:600;margin-bottom:4px;">{label}</div>'
                f'<div style="color:{value_color};font-size:1rem;font-weight:700;">{value}</div>'
                f'</div>'
            )

        _summary_html = (
            f'<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">'
            + _summary_tile('Total PnL', f'{_mo_total:+.2f} $', _mo_pnl_color)
            + _summary_tile('Trading Days', str(_mo_trading))
            + _summary_tile('Win Rate', f'{_mo_wr}%', '#22c55e' if _mo_wr >= 50 else '#ef4444')
            + _summary_tile('Wins / Losses', f'{_mo_wins} / {_mo_losses}')
            + _summary_tile('Best Day', _mo_best_str, '#22c55e')
            + _summary_tile('Worst Day', _mo_worst_str, '#ef4444')
            + '</div>'
        )

        _cal_html = f"""
        <div style="background:#0d1117;border-radius:12px;border:1px solid #1e2d45;padding:12px 16px;">
          {_summary_html}
          <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-bottom:5px;">
            {_header_cells}
          </div>
          <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:5px;">
            {_week_rows}
          </div>
        </div>
        """
        st.html(_cal_html)

    with tab2:
        # Monthly PnL — always show full month range (with 0 for months without trades)
        _monthly_vals = [_month_pnl.get(m, 0) for m in _months_avail]
        _colors_m = [COLORS['green'] if x > 0 else (COLORS['red'] if x < 0 else '#4a5568') for x in _monthly_vals]
        fig_m = go.Figure(go.Bar(x=_months_avail, y=_monthly_vals, marker_color=_colors_m, marker_line_width=0,
            hovertemplate='%{x}<br>PnL: %{y:.2f} $<extra></extra>'))
        fig_m.update_layout(**PLOTLY_LAYOUT, title_text='Monthly PnL', height=280, showlegend=False)
        fig_m.update_xaxes(title='Month')
        fig_m.update_yaxes(title='PnL ($)', ticksuffix=' $')
        st.plotly_chart(fig_m, use_container_width=True)

        days_order = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        wd_pnl = stats_dict['weekday_stats']['pnl'] if has_data else {}
        wd_vals = [wd_pnl.get(d, 0) for d in days_order]
        colors_wd = [COLORS['green'] if x > 0 else (COLORS['red'] if x < 0 else '#4a5568') for x in wd_vals]
        fig_wd = go.Figure(go.Bar(x=days_order, y=wd_vals, marker_color=colors_wd, marker_line_width=0,
            hovertemplate='%{x}<br>PnL: %{y:.2f} $<extra></extra>'))
        fig_wd.update_layout(**PLOTLY_LAYOUT, title_text='PnL by Weekday', height=280, showlegend=False)
        fig_wd.update_xaxes(title='Weekday')
        fig_wd.update_yaxes(title='PnL ($)', ticksuffix=' $')
        st.plotly_chart(fig_wd, use_container_width=True)

        all_hours = list(range(24))
        hr_pnl = stats_dict['hour_stats']['pnl'] if has_data else {}
        hr_vals = [hr_pnl.get(h, 0) for h in all_hours]
        colors_hr = [COLORS['green'] if x > 0 else (COLORS['red'] if x < 0 else '#4a5568') for x in hr_vals]
        fig_hr = go.Figure(go.Bar(x=[f"{h}:00" for h in all_hours], y=hr_vals, marker_color=colors_hr, marker_line_width=0,
            hovertemplate='%{x}<br>PnL: %{y:.2f} $<extra></extra>'))
        fig_hr.update_layout(**PLOTLY_LAYOUT, title_text='PnL by Hour', height=280, showlegend=False)
        fig_hr.update_xaxes(title='Hour')
        fig_hr.update_yaxes(title='PnL ($)', ticksuffix=' $')
        st.plotly_chart(fig_hr, use_container_width=True)

    with tab3:
        if has_data:
            ast = stats_dict['asset_stats']
            assets = list(ast['pnl'].keys())
            ast_pnl = [ast['pnl'][a] for a in assets]
        else:
            assets = ['No data']
            ast_pnl = [0]
        colors_ast = [COLORS['green'] if x > 0 else COLORS['red'] for x in ast_pnl]
        fig_ast = go.Figure(go.Bar(y=assets, x=ast_pnl, orientation='h', marker_color=colors_ast, marker_line_width=0,
            hovertemplate='%{y}<br>PnL: %{x:.2f} $<extra></extra>'))
        fig_ast.update_layout(**PLOTLY_LAYOUT, title_text='PnL by Asset', height=max(350, len(assets)*32), showlegend=False)
        fig_ast.update_xaxes(title='PnL ($)', ticksuffix=' $')
        fig_ast.update_yaxes(title='Asset')
        st.plotly_chart(fig_ast, use_container_width=True)


# =====================================================
# UI
# =====================================================

# --- Auth: session is stored in st.session_state (token-based, no SDK needed) ---

# --- Login page ---
def _show_auth_page():
    st.markdown('<p class="hero-title">AI Trading Coach</p>', unsafe_allow_html=True)
    st.markdown('<p class="hero-subtitle">Sign in to access your personal trading journal.</p>', unsafe_allow_html=True)

    st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
    _, _ac, _ = st.columns([1, 1, 1])
    with _ac:
        _lt, _st = st.tabs(["Login", "Sign Up"])
        with _lt:
            _email = st.text_input("Email", key="login_email", placeholder="your@email.com")
            _pw = st.text_input("Password", type="password", key="login_pw")
            if st.button("Login", type="primary", use_container_width=True, key="login_btn"):
                _data, _code = _sb_login(_email, _pw)
                if _code == 200 and "access_token" in _data:
                    st.session_state.sb_access_token = _data["access_token"]
                    st.session_state.sb_refresh_token = _data.get("refresh_token", "")
                    st.session_state.sb_user_id = _data["user"]["id"]
                    st.session_state.sb_user_email = _data["user"]["email"]
                    _save_session_cookies(_data["access_token"], _data.get("refresh_token", ""), _data["user"]["id"], _data["user"]["email"])
                    st.rerun()
                else:
                    _msg = _data.get("error_description") or _data.get("msg") or str(_data)
                    st.error(f"Login failed: {_msg}")
        with _st:
            _email2 = st.text_input("Email", key="signup_email", placeholder="your@email.com")
            _pw2 = st.text_input("Password (min 6 chars)", type="password", key="signup_pw")
            if st.button("Create Account", type="primary", use_container_width=True, key="signup_btn"):
                if len(_pw2) < 6:
                    st.error("Password must be at least 6 characters.")
                elif "@" not in _email2 or len(_email2) > 254:
                    st.error("Please enter a valid email address.")
                else:
                    _data2, _code2 = _sb_signup(_email2, _pw2)
                    if _code2 in (200, 201) and "id" in _data2.get("user", {}):
                        st.success("Account created! You can now log in.")
                    elif _code2 in (200, 201) and "id" in _data2:
                        st.success("Account created! You can now log in.")
                    else:
                        _msg2 = _data2.get("error_description") or _data2.get("msg") or str(_data2)
                        st.error(f"Sign up failed: {_msg2}")

# --- Auth gate ---
if not _restore_session_from_cookies():
    _show_auth_page()
    st.stop()

if not _ensure_valid_token():
    st.warning("Session abgelaufen — bitte neu einloggen.")
    _clear_session_cookies()
    for _k in ['sb_access_token', 'sb_refresh_token', 'sb_user_id', 'sb_user_email', 'journal_trades']:
        st.session_state.pop(_k, None)
    st.rerun()

# --- Header (only shown when logged in) ---
st.markdown('<p class="hero-title">AI Trading Coach</p>', unsafe_allow_html=True)
st.markdown('<p class="hero-subtitle">Upload your trades — the AI tells you what you\'re doing wrong.</p>', unsafe_allow_html=True)

# --- Journal helpers (DB-backed) ---
def load_journal():
    try:
        _token = st.session_state.sb_access_token
        _uid = st.session_state.sb_user_id
        _rows = _sb_get_trades(_uid, _token)
        if isinstance(_rows, dict) and "message" in _rows:
            st.error(f"Could not load trades: {_rows['message']}")
            return []
        trades = []
        for _r in _rows:
            trades.append({
                'id': _r['id'],
                'name': _r.get('name', ''),
                'open': _r.get('open_date', ''),
                'close': _r.get('close_date', ''),
                'pair': _r.get('pair', ''),
                'direction': _r.get('direction', 'Long'),
                'session': _r.get('session', ''),
                'strategy': _r.get('strategy', ''),
                'status': _r.get('status', 'Open'),
                'net_pnl': float(_r.get('net_pnl', 0) or 0),
                'fees': float(_r.get('fees', 0) or 0),
                'gross_pnl': float(_r.get('gross_pnl', 0) or 0),
                'profit_loss': _r.get('profit_loss', ''),
                'confluences': json.loads(_r.get('confluences', '[]') or '[]'),
                'additions': _r.get('notes', ''),
                'screenshots': json.loads(_r.get('screenshots', '[]') or '[]'),
            })
        return trades
    except Exception as _e:
        st.error(f"Could not load trades: {_e}")
        return []

def save_journal(trades):
    try:
        _token = st.session_state.sb_access_token
        _uid = st.session_state.sb_user_id
        _sb_delete_trades(_uid, _token)
        if trades:
            _rows = []
            for _i, _t in enumerate(trades):
                _tid = str(_t.get('id') or uuid.uuid4())
                def _trunc(val, n): return str(val or '')[:n]
                _rows.append({
                    'id': _tid,
                    'user_id': _uid,
                    'name': _trunc(_t.get('name'), 200),
                    'open_date': _trunc(_t.get('open'), 30),
                    'close_date': _trunc(_t.get('close'), 30),
                    'pair': _trunc(_t.get('pair'), 50),
                    'direction': _trunc(_t.get('direction', 'Long'), 20),
                    'session': _trunc(_t.get('session'), 50),
                    'strategy': _trunc(_t.get('strategy'), 200),
                    'status': _trunc(_t.get('status', 'Open'), 20),
                    'net_pnl': float(_t.get('net_pnl', 0) or 0),
                    'fees': float(_t.get('fees', 0) or 0),
                    'gross_pnl': float(_t.get('gross_pnl', 0) or 0),
                    'profit_loss': _trunc(_t.get('profit_loss'), 20),
                    'confluences': json.dumps([_trunc(c, 100) for c in (_t.get('confluences') or [])[:20]]),
                    'notes': _trunc(_t.get('additions'), 2000),
                    'screenshots': json.dumps([str(u) for u in (_t.get('screenshots') or [])[:20]]),
                    'sort_order': _i,
                })
            _sb_insert_trades(_rows, _token)
    except Exception as _e:
        st.error(f"Could not save trades: {_e}")

if 'journal_trades' not in st.session_state:
    st.session_state.journal_trades = load_journal()
if 'show_add_form' not in st.session_state:
    st.session_state.show_add_form = False
if 'editing_index' not in st.session_state:
    st.session_state.editing_index = None
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'chat_messages' not in st.session_state:
    st.session_state.chat_messages = []
if 'data_context' not in st.session_state:
    st.session_state.data_context = None

# --- Sidebar ---
with st.sidebar:
    st.markdown(f"""
    <div style="text-align: center; padding: 20px 0 10px;">
        <div style="font-size: 2rem; margin-bottom: 8px;">&#x1F4C8;</div>
        <div style="font-size: 0.75rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.1em;">Trading Coach</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f"<h2 style='font-size: 0.8rem; color: {COLORS['text_dim']};'>NAVIGATION</h2>", unsafe_allow_html=True)
    page = st.radio("Page", ["📓 Journal", "📊 Import Data"], index=0, label_visibility="collapsed")

    st.markdown("---")

    _token = st.session_state.get('sb_access_token', '')
    _uid = st.session_state.get('sb_user_id', '')

    if 'export_files' not in st.session_state:
        st.session_state.export_files = _sb_list_exports(_uid, _token)

    export_files = st.session_state.export_files

    # --- Upload area ---
    st.markdown(f"<div style='font-size: 0.75rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px;'>Trade Data</div>", unsafe_allow_html=True)
    new_upload = st.file_uploader("Upload", type=['xlsx', 'csv'], key="export_uploader", label_visibility="collapsed")
    if new_upload:
        safe_name = new_upload.name.replace(" ", "_")
        already_uploaded = any(f["name"] == safe_name for f in export_files)
        if already_uploaded:
            st.session_state.selected_export = safe_name
        elif new_upload.size > 20 * 1024 * 1024:
            st.error("File too large (max 20 MB).")
        else:
            with st.spinner("Uploading..."):
                path, err = _sb_upload_export(new_upload.read(), new_upload.name, _uid, _token)
            if path:
                st.session_state.export_files = _sb_list_exports(_uid, _token)
                st.session_state.selected_export = safe_name
                st.rerun()
            else:
                st.error(f"Upload failed: {err}")

    # --- File selector ---
    use_default = False
    uploaded_file = None
    selected_export_bytes = None

    if export_files:
        file_names = [f["name"] for f in export_files]
        default_idx = 0
        if 'selected_export' in st.session_state and st.session_state.selected_export in file_names:
            default_idx = file_names.index(st.session_state.selected_export)

        _sel_col, _del_col, _ref_col = st.columns([6, 1, 1])
        with _sel_col:
            selected_name = st.selectbox("Dataset", file_names, index=default_idx, key="export_select", label_visibility="collapsed")
        with _del_col:
            if st.button("🗑", key="del_export", help="Delete file"):
                st.session_state.confirm_del_export = True
        with _ref_col:
            if st.button("↻", key="refresh_exports", help="Refresh list"):
                st.session_state.export_files = _sb_list_exports(_uid, _token)
                st.rerun()

        if st.session_state.get("confirm_del_export"):
            st.warning(f"Delete **{selected_name}**?")
            _dc1, _dc2 = st.columns(2)
            with _dc1:
                if st.button("Delete", key="del_export_yes", type="primary", use_container_width=True):
                    selected_path_del = next(f["path"] for f in export_files if f["name"] == selected_name)
                    _sb_delete_export(selected_path_del, _token)
                    st.session_state.export_files = _sb_list_exports(_uid, _token)
                    st.session_state.pop('selected_export', None)
                    st.session_state.confirm_del_export = False
                    st.rerun()
            with _dc2:
                if st.button("Cancel", key="del_export_no", use_container_width=True):
                    st.session_state.confirm_del_export = False
                    st.rerun()

        st.session_state.selected_export = selected_name
        selected_path = next(f["path"] for f in export_files if f["name"] == selected_name)
        with st.spinner("Loading..."):
            selected_export_bytes = _sb_download_export(selected_path, _token)
    else:
        st.markdown(f"<div style='font-size: 0.8rem; color: {COLORS['text_dim']}; padding: 8px 0;'>No files yet — upload your broker export above.</div>", unsafe_allow_html=True)
        if st.button("↻ Refresh", key="refresh_exports_empty", use_container_width=True):
            st.session_state.export_files = _sb_list_exports(_uid, _token)
            st.rerun()

    st.markdown("---")
    st.markdown(f"<h2 style='font-size: 0.8rem; color: {COLORS['text_dim']};'>AI MODEL</h2>", unsafe_allow_html=True)
    ai_model = st.selectbox("Model", ["Gemini 2.5 Flash (free)", "Claude Opus 4.6 (paid)"], index=0, label_visibility="collapsed")

    st.markdown("---")
    st.markdown(f"""
    <div style="padding: 12px 16px; background: {COLORS['bg_card']}; border-radius: 12px; border: 1px solid {COLORS['border']}; margin-top: 20px;">
        <div style="font-size: 0.7rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;">Logged in as</div>
        <div style="font-size: 0.8rem; color: {COLORS['accent_cyan']}; word-break: break-all;">{_html.escape(st.session_state.get('sb_user_email', ''))}</div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)
    if st.button("Logout", use_container_width=True, key="logout_btn"):
        _sb_logout(st.session_state.get('sb_access_token', ''))
        _clear_session_cookies()
        for _k in ['sb_access_token', 'sb_refresh_token', 'sb_user_id', 'sb_user_email', 'journal_trades']:
            st.session_state.pop(_k, None)
        st.rerun()


# --- Load broker data ---
df = None
if selected_export_bytes:
    import io
    selected_name = st.session_state.get('selected_export', '')
    try:
        if selected_name.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(selected_export_bytes))
        else:
            df = pd.read_excel(io.BytesIO(selected_export_bytes))
    except Exception as e:
        st.error(f"Could not parse file: {e}")

if df is not None:
    trades = parse_trades(df)
    stats = compute_stats(trades)

# =====================================================
# PAGE: JOURNAL
# =====================================================
if page == "📓 Journal":

    _jt_coach = st.session_state.journal_trades
    _jt_count = len(_jt_coach)

    _hcol1, _hcol2 = st.columns([3, 1])
    with _hcol1:
        st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
            <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
            <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Trading Journal</div>
        </div>
        """, unsafe_allow_html=True)
    with _hcol2:
        start_j_analysis_top = st.button("Start Analysis", type="primary", use_container_width=True, key="j_coach_btn_top")

    if start_j_analysis_top:
        if _jt_count == 0:
            st.warning("No trades in the journal yet. Log some trades first.")
        else:
            sys_p, dat_p = build_journal_ai_prompt(_jt_coach)
            with st.spinner("AI is analyzing your journal trades..."):
                if "Claude" in ai_model:
                    _analysis = "Claude integration coming in the next version."
                else:
                    _screenshots = _collect_trade_screenshots(_jt_coach)
                    if _screenshots:
                        _analysis = call_gemini_with_images(sys_p, dat_p, _screenshots)
                    else:
                        _analysis = call_gemini(sys_p, dat_p)
            st.session_state.analysis_result = _analysis
            st.session_state.data_context = dat_p
            st.session_state.chat_messages = []
            _out = Path(__file__).parent / "analyses"
            _out.mkdir(exist_ok=True)
            (_out / f"journal_analysis_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.md").write_text(_analysis, encoding='utf-8')
            st.rerun()

    default_strategies = ["Trend + Trend", "Trend + Reverse", "Reversal", "Breakout", "Scalping", "Swing Trading", "Position Trading"]
    default_confluences = [
        "Strong Entry", "Weak Entry", "1H STDV 1", "1H VWAP",
        "4h Vwap Sync", "1D Vwap Sync No", "1D Vwap sync",
        "FIB 0.38", "FIB 0.5", "FIB 0.61", "FIB No",
        "TP Edge", "More Profit", "5Min VWAP 1 Rejection"
    ]

    existing_strategies = set(default_strategies)
    existing_confluences = set(default_confluences)
    for t in st.session_state.journal_trades:
        if t.get('strategy') and t['strategy'] not in existing_strategies:
            existing_strategies.add(t['strategy'])
        for c in t.get('confluences', []):
            existing_confluences.add(c)
    all_strategies = sorted(existing_strategies)
    all_confluences = sorted(existing_confluences)

    if st.button("+ Add New Trade", key="add_trade_btn"):
        st.session_state.show_add_form = not st.session_state.show_add_form
        st.session_state.editing_index = None

    if st.session_state.show_add_form or st.session_state.editing_index is not None:
        editing = st.session_state.editing_index is not None
        edit_data = st.session_state.journal_trades[st.session_state.editing_index] if editing else {}

        st.markdown(f"""
        <div style="background: {COLORS['bg_card']}; border: 1px solid {COLORS['border']}; border-radius: 16px; padding: 24px; margin: 16px 0;">
            <div style="font-size: 1rem; font-weight: 600; color: {COLORS['text_bright']}; margin-bottom: 16px;">{'Edit Trade' if editing else 'New Trade'}</div>
        </div>
        """, unsafe_allow_html=True)

        with st.container():
            r1c1, r1c2, r1c3, r1c4 = st.columns(4)
            with r1c1:
                trade_name = st.text_input("Name", value=edit_data.get('name', ''), key="j_name")
            with r1c2:
                trade_open = st.date_input("Open", value=pd.to_datetime(edit_data['open']).date() if edit_data.get('open') else datetime.now().date(), key="j_open")
            with r1c3:
                trade_close = st.date_input("Close", value=pd.to_datetime(edit_data['close']).date() if edit_data.get('close') else datetime.now().date(), key="j_close")
            with r1c4:
                trade_pair = st.text_input("Pair", value=edit_data.get('pair', ''), placeholder="e.g. ENAUSDT", key="j_pair")

            r2c1, r2c2, r2c3, r2c4 = st.columns(4)
            with r2c1:
                dir_options = ["Long", "Short"]
                trade_direction = st.selectbox("Direction", dir_options, index=dir_options.index(edit_data['direction']) if edit_data.get('direction') in dir_options else 0, key="j_dir")
            with r2c2:
                session_options = ["", "London", "Asia", "New York"]
                trade_session = st.selectbox("Session", session_options, index=session_options.index(edit_data['session']) if edit_data.get('session') in session_options else 0, key="j_session")
            with r2c3:
                strat_options = [""] + all_strategies
                trade_strategy = st.selectbox("Strategy", strat_options, index=strat_options.index(edit_data['strategy']) if edit_data.get('strategy') in strat_options else 0, key="j_strat")
            with r2c4:
                custom_strategy = st.text_input("Custom Strategy", value="", placeholder="Or enter custom...", key="j_custom_strat")

            r3c1, r3c2, r3c3, r3c4 = st.columns(4)
            with r3c1:
                status_options = ["Open", "Closed"]
                trade_status = st.selectbox("Status", status_options, index=status_options.index(edit_data['status']) if edit_data.get('status') in status_options else 0, key="j_status")
            with r3c2:
                trade_net_pnl = st.number_input("Net PnL ($)", value=float(edit_data.get('net_pnl', 0)), step=0.01, format="%.2f", key="j_net")
            with r3c3:
                trade_fees = st.number_input("Fees ($)", value=float(edit_data.get('fees', 0)), step=0.01, format="%.2f", key="j_fees")
            with r3c4:
                gross = trade_net_pnl - trade_fees
                st.markdown(f"""
                <div style="margin-top: 28px;">
                    <div style="font-size: 0.75rem; color: {COLORS['text_dim']}; text-transform: uppercase; margin-bottom: 4px;">Gross PnL</div>
                    <div style="font-size: 1.3rem; font-weight: 700; color: {'#10b981' if gross >= 0 else '#ef4444'};">{gross:.2f} $</div>
                </div>
                """, unsafe_allow_html=True)

            trade_confluences = st.multiselect("Confluences", all_confluences, default=edit_data.get('confluences', []), key="j_conf")
            custom_conf = st.text_input("Add Custom Confluence", value="", placeholder="e.g. '4H VWAP Bounce' — press Enter to add", key="j_custom_conf")
            trade_additions = st.text_area("Additions / Notes", value=edit_data.get('additions', ''), height=80, placeholder="Notes about the trade...", key="j_additions")

            # --- Screenshots ---
            st.markdown(f"<div style='font-size: 0.75rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 12px; margin-bottom: 4px;'>Screenshots</div>", unsafe_allow_html=True)
            existing_screenshots = list(edit_data.get('screenshots', []))

            # Show existing screenshots with delete option
            if existing_screenshots:
                thumb_cols = st.columns(min(len(existing_screenshots), 4))
                to_remove = []
                for i, url in enumerate(existing_screenshots):
                    with thumb_cols[i % 4]:
                        st.image(url, use_container_width=True)
                        if st.button("🗑", key=f"del_img_{i}", help="Remove screenshot"):
                            to_remove.append(url)
                for url in to_remove:
                    existing_screenshots.remove(url)

            uploaded_screenshots = st.file_uploader(
                "Add screenshots (PNG, JPG, WEBP)",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
                key="j_screenshots"
            )

            bc1, bc2 = st.columns(2)
            with bc1:
                if st.button("Save", type="primary", key="j_save", use_container_width=True):
                    final_strategy = custom_strategy if custom_strategy else trade_strategy
                    final_confluences = list(trade_confluences)
                    if custom_conf and custom_conf not in final_confluences:
                        final_confluences.append(custom_conf)

                    _existing_id = st.session_state.journal_trades[st.session_state.editing_index].get('id') if editing else None
                    trade_id = _existing_id or str(uuid.uuid4())

                    # Upload new screenshots
                    new_urls = list(existing_screenshots)
                    token = st.session_state.get('sb_access_token', '')
                    user_id = st.session_state.get('sb_user_id', '')
                    if uploaded_screenshots and token and user_id:
                        with st.spinner("Uploading screenshots..."):
                            for f in uploaded_screenshots:
                                url = _sb_upload_screenshot(f.read(), f.name, trade_id, user_id, token)
                                if url:
                                    new_urls.append(url)

                    trade_entry = {
                        'id': trade_id,
                        'name': trade_name or "New Trade",
                        'open': str(trade_open),
                        'close': str(trade_close),
                        'pair': trade_pair.upper(),
                        'direction': trade_direction,
                        'session': trade_session,
                        'strategy': final_strategy,
                        'status': trade_status,
                        'net_pnl': trade_net_pnl,
                        'fees': trade_fees,
                        'gross_pnl': round(gross, 2),
                        'profit_loss': 'Profit' if gross > 0 else 'Loss',
                        'confluences': final_confluences,
                        'additions': trade_additions,
                        'screenshots': new_urls,
                    }

                    if editing:
                        st.session_state.journal_trades[st.session_state.editing_index] = trade_entry
                    else:
                        st.session_state.journal_trades.insert(0, trade_entry)

                    save_journal(st.session_state.journal_trades)
                    st.session_state.show_add_form = False
                    st.session_state.editing_index = None
                    st.rerun()

            with bc2:
                if st.button("Cancel", key="j_cancel", use_container_width=True):
                    st.session_state.show_add_form = False
                    st.session_state.editing_index = None
                    st.rerun()

    # --- Journal Table (interactive) ---
    st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

    _editor_trades = st.session_state.journal_trades
    _df_editor = pd.DataFrame([{
        '_id': t.get('id', ''),
        'Name': t.get('name', ''),
        'Open': t.get('open', ''),
        'Close': t.get('close', ''),
        'Pair': t.get('pair', ''),
        'Direction': t.get('direction', 'Long'),
        'Session': t.get('session', ''),
        'Strategy': t.get('strategy', ''),
        'Status': t.get('status', 'Open'),
        'Net PnL': float(t.get('net_pnl', 0)),
        'Fees': float(t.get('fees', 0)),
        'Gross PnL': float(t.get('gross_pnl', 0)),
        'P/L': t.get('profit_loss', 'Loss'),
        'Confluences': ', '.join(t.get('confluences', []) if isinstance(t.get('confluences'), list) else []),
        'Notes': t.get('additions', ''),
    } for t in _editor_trades]) if _editor_trades else pd.DataFrame(columns=[
        '_id','Name','Open','Close','Pair','Direction','Session','Strategy','Status','Net PnL','Fees','Gross PnL','P/L','Confluences','Notes'
    ])

    _edited = st.data_editor(
        _df_editor,
        column_config={
            '_id': None,
            'Direction': st.column_config.SelectboxColumn('Direction', options=['Long', 'Short'], required=True),
            'Session': st.column_config.SelectboxColumn('Session', options=['', 'London', 'Asia', 'New York']),
            'Strategy': st.column_config.SelectboxColumn('Strategy', options=[''] + all_strategies),
            'Status': st.column_config.SelectboxColumn('Status', options=['Open', 'Closed'], required=True),
            'P/L': st.column_config.SelectboxColumn('P/L', options=['Profit', 'Loss'], required=True),
            'Net PnL': st.column_config.NumberColumn('Net PnL ($)', format='%.2f'),
            'Fees': st.column_config.NumberColumn('Fees ($)', format='%.2f'),
            'Gross PnL': st.column_config.NumberColumn('Gross PnL ($)', format='%.2f', disabled=True),
            'Open': st.column_config.TextColumn('Open'),
            'Close': st.column_config.TextColumn('Close'),
        },
        num_rows='dynamic',
        use_container_width=True,
        hide_index=True,
        key='journal_editor',
    )

    # Save changes whenever editor output differs from stored trades
    _new_trades = []
    for _, row in _edited.iterrows():
        _gross = round(float(row.get('Net PnL', 0) or 0) - float(row.get('Fees', 0) or 0), 2)
        _row_id = str(row.get('_id', '') or '')
        if not _row_id or _row_id == 'nan':
            _row_id = str(uuid.uuid4())
        _new_trades.append({
            'id': _row_id,
            'name': str(row.get('Name', '')),
            'open': str(row.get('Open', '')),
            'close': str(row.get('Close', '')),
            'pair': str(row.get('Pair', '')),
            'direction': str(row.get('Direction', 'Long')),
            'session': str(row.get('Session', '')),
            'strategy': str(row.get('Strategy', '')),
            'status': str(row.get('Status', 'Open')),
            'net_pnl': float(row.get('Net PnL', 0) or 0),
            'fees': float(row.get('Fees', 0) or 0),
            'gross_pnl': _gross,
            'profit_loss': str(row.get('P/L', 'Loss')),
            'confluences': [c.strip() for c in str(row.get('Confluences', '')).split(',') if c.strip()],
            'additions': str(row.get('Notes', '')),
        })
    if _new_trades != st.session_state.journal_trades:
        st.session_state.journal_trades = _new_trades
        save_journal(_new_trades)

    # --- Analytics (always shown, below table) ---
    st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px;">
        <div style="width: 3px; height: 24px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.1rem; font-weight: 600; color: {COLORS['text_bright']};">Analytics</div>
    </div>
    """, unsafe_allow_html=True)

    jt = st.session_state.journal_trades
    _j_trades_df, _j_stats = journal_to_trades_and_stats(jt)
    render_analytics(_j_trades_df, _j_stats)

    # =====================================================
    # AI COACH (inline, journal data only)
    # =====================================================
    st.markdown("<div style='height: 40px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 24px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">AI Trading Coach</div>
        <div style="font-size: 0.8rem; color: {COLORS['text_dim']}; margin-left: 8px;">— analyzes your journal trades</div>
    </div>
    """, unsafe_allow_html=True)

    jt_coach = _jt_coach
    jt_count = _jt_count

    if st.session_state.analysis_result:
        st.markdown(f'<div class="analysis-box">', unsafe_allow_html=True)
        st.markdown(st.session_state.analysis_result)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px;">
            <div style="width: 3px; height: 24px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
            <div style="font-size: 1.1rem; font-weight: 600; color: {COLORS['text_bright']};">Follow-up Questions</div>
        </div>
        <div style="font-size: 0.85rem; color: {COLORS['text_dim']}; margin-bottom: 16px;">
            Ask the AI Coach anything about your analysis — e.g. "Why do I lose on Fridays?" or "Which pairs are my worst?"
        </div>
        """, unsafe_allow_html=True)

        for msg in st.session_state.chat_messages:
            if msg['role'] == 'user':
                st.markdown(f"""
                <div style="display: flex; justify-content: flex-end; margin-bottom: 12px;">
                    <div style="background: linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(139, 92, 246, 0.2)); border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 16px 16px 4px 16px; padding: 12px 18px; max-width: 80%; color: {COLORS['text_bright']};">
                        {_html.escape(str(msg['content']))}
                    </div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="background: {COLORS["bg_card"]}; border: 1px solid {COLORS["border"]}; border-radius: 16px 16px 16px 4px; padding: 16px 20px; max-width: 90%; margin-bottom: 12px;">', unsafe_allow_html=True)
                st.markdown(msg['content'])
                st.markdown('</div>', unsafe_allow_html=True)

        user_question_j = st.chat_input("Ask the AI Coach about your journal...", key="j_coach_chat")
        if user_question_j:
            st.session_state.chat_messages.append({'role': 'user', 'content': user_question_j})
            chat_sys_j = f"""You are an AI Trading Coach analyzing journal trade data.

Trading data:
{st.session_state.data_context}

Your previous analysis:
{st.session_state.analysis_result}

Answer follow-up questions directly and concretely using numbers from the data. Address the trader as "you". Keep answers brief (2-5 sentences) unless details are requested. Be honest and constructive."""
            chat_hist_j = [{'role': 'system', 'content': chat_sys_j}]
            for msg in st.session_state.chat_messages:
                chat_hist_j.append(msg)
            with st.spinner(""):
                response_j = call_gemini_chat(chat_hist_j)
            st.session_state.chat_messages.append({'role': 'assistant', 'content': response_j})
            st.rerun()

    analyses_dir_j = Path(__file__).parent / "analyses"
    if analyses_dir_j.exists():
        prev_j = sorted(analyses_dir_j.glob("journal_analysis_*.md"), reverse=True)
        if prev_j:
            st.markdown("<div style='height: 24px'></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='font-size: 0.8rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px;'>Previous Journal Analyses</div>", unsafe_allow_html=True)
            def _fmt_analysis_name(stem):
                s = stem.replace("journal_analysis_", "")
                try:
                    dt = datetime.strptime(s, "%Y-%m-%d_%H-%M")
                    return dt.strftime("%Y.%m.%d %H:%M Uhr")
                except Exception:
                    return s.replace("_", " ")
            prev_j_opts = {f"{_fmt_analysis_name(f.stem)} (Trading Journal)": f for f in prev_j[:10]}
            _jsel_col, _jdel_col = st.columns([5, 1])
            with _jsel_col:
                sel_j = st.selectbox("Select", options=["Select..."] + list(prev_j_opts.keys()), index=0, label_visibility="collapsed", key="j_analysis_selector")
            with _jdel_col:
                if sel_j and sel_j != "Select...":
                    if st.button("🗑", key="j_del_analysis", help="Delete this analysis"):
                        st.session_state.j_confirm_delete = sel_j
            if st.session_state.get("j_confirm_delete") == sel_j and sel_j and sel_j != "Select...":
                st.warning(f"Delete **{sel_j}**? This cannot be undone.")
                _cj1, _cj2 = st.columns(2)
                with _cj1:
                    if st.button("Yes, delete", key="j_confirm_yes", type="primary"):
                        prev_j_opts[sel_j].unlink()
                        st.session_state.pop("j_confirm_delete", None)
                        st.rerun()
                with _cj2:
                    if st.button("Cancel", key="j_confirm_no"):
                        st.session_state.pop("j_confirm_delete", None)
                        st.rerun()
            elif sel_j and sel_j != "Select..." and sel_j in prev_j_opts:
                loaded_j = prev_j_opts[sel_j].read_text(encoding='utf-8')
                if st.session_state.analysis_result != loaded_j:
                    st.session_state.analysis_result = loaded_j
                    _, dp_j = build_journal_ai_prompt(jt_coach)
                    st.session_state.data_context = dp_j
                    st.session_state.chat_messages = []
                    st.rerun()


# =====================================================
# PAGE: IMPORT DATA
# =====================================================
elif page == "📊 Import Data":

    if 'broker_analysis_result' not in st.session_state:
        st.session_state.broker_analysis_result = None
    if 'broker_chat_messages' not in st.session_state:
        st.session_state.broker_chat_messages = []
    if 'broker_data_context' not in st.session_state:
        st.session_state.broker_data_context = None

    # Header row with Start Analysis button top-right
    _b_hcol1, _b_hcol2 = st.columns([3, 1])
    with _b_hcol1:
        st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
            <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
            <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Import Data</div>
        </div>
        <div style="font-size: 0.85rem; color: {COLORS['text_dim']}; margin-bottom: 24px;">Analytics and AI Coach powered by your broker export (Bitget, etc.)</div>
        """, unsafe_allow_html=True)
    with _b_hcol2:
        start_b_analysis = st.button("Start Analysis", type="primary", use_container_width=True,
                                     key="b_coach_btn", disabled=(df is None))

    # --- Analytics ---
    if df is None:
        render_analytics(pd.DataFrame(columns=['date','pnl','asset','is_win']), None, tab_prefix='broker')
        st.info("Enable 'Load my trades' in the sidebar or upload your broker export to see real data.")
    else:
        _b_trades = trades.rename(columns={'date': 'date', 'pnl': 'pnl'}).copy()
        _b_trades['asset'] = _b_trades['asset'] if 'asset' in _b_trades.columns else 'Unknown'
        _b_trades['is_win'] = _b_trades['pnl'] > 0
        render_analytics(_b_trades[['date','pnl','asset','is_win']], stats, tab_prefix='broker')

    # --- Broker AI Coach ---
    st.markdown("<div style='height: 40px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 24px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">AI Trading Coach</div>
        <div style="font-size: 0.8rem; color: {COLORS['text_dim']}; margin-left: 8px;">— analyzes your imported broker data</div>
    </div>
    """, unsafe_allow_html=True)

    if df is None:
        st.info("Load your broker export in the sidebar to enable the AI Coach.")
    elif start_b_analysis:
        system_prompt_b, data_prompt_b = build_ai_prompt(stats, trades)
        with st.spinner("AI is analyzing your broker trades..."):
            if "Claude" in ai_model:
                st.warning("Claude Opus requires an ANTHROPIC_API_KEY in the .env file.")
                analysis_b = "Claude integration coming in the next version."
            else:
                analysis_b = call_gemini(system_prompt_b, data_prompt_b)
        st.session_state.broker_analysis_result = analysis_b
        st.session_state.broker_data_context = data_prompt_b
        st.session_state.broker_chat_messages = []
        output_dir_b = Path(__file__).parent / "analyses"
        output_dir_b.mkdir(exist_ok=True)
        timestamp_b = datetime.now().strftime("%Y-%m-%d_%H-%M")
        (output_dir_b / f"broker_analysis_{timestamp_b}.md").write_text(analysis_b, encoding='utf-8')
        st.rerun()

    if st.session_state.broker_analysis_result:
        st.markdown(f'<div class="analysis-box">', unsafe_allow_html=True)
        st.markdown(st.session_state.broker_analysis_result)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px;">
            <div style="width: 3px; height: 24px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
            <div style="font-size: 1.1rem; font-weight: 600; color: {COLORS['text_bright']};">Follow-up Questions</div>
        </div>
        """, unsafe_allow_html=True)

        for msg in st.session_state.broker_chat_messages:
            if msg['role'] == 'user':
                st.markdown(f"""
                <div style="display: flex; justify-content: flex-end; margin-bottom: 12px;">
                    <div style="background: linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(139, 92, 246, 0.2)); border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 16px 16px 4px 16px; padding: 12px 18px; max-width: 80%; color: {COLORS['text_bright']};">
                        {_html.escape(str(msg['content']))}
                    </div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="background: {COLORS["bg_card"]}; border: 1px solid {COLORS["border"]}; border-radius: 16px 16px 16px 4px; padding: 16px 20px; max-width: 90%; margin-bottom: 12px;">', unsafe_allow_html=True)
                st.markdown(msg['content'])
                st.markdown('</div>', unsafe_allow_html=True)

        user_question_b = st.chat_input("Ask the AI Coach about your broker data...", key="b_coach_chat")
        if user_question_b:
            st.session_state.broker_chat_messages.append({'role': 'user', 'content': user_question_b})
            chat_sys_b = f"""You are an AI Trading Coach analyzing broker trade data.

Trading data:
{st.session_state.broker_data_context}

Your previous analysis:
{st.session_state.broker_analysis_result}

Answer follow-up questions directly and concretely using numbers from the data. Address the trader as "you". Keep answers brief (2-5 sentences) unless details are requested. Be honest and constructive."""
            chat_hist_b = [{'role': 'system', 'content': chat_sys_b}]
            for msg in st.session_state.broker_chat_messages:
                chat_hist_b.append(msg)
            with st.spinner(""):
                response_b = call_gemini_chat(chat_hist_b)
            st.session_state.broker_chat_messages.append({'role': 'assistant', 'content': response_b})
            st.rerun()

    if df is not None:
        analyses_dir_b = Path(__file__).parent / "analyses"
        if analyses_dir_b.exists():
            prev_b = sorted(analyses_dir_b.glob("broker_analysis_*.md"), reverse=True)
            if prev_b:
                st.markdown("<div style='height: 24px'></div>", unsafe_allow_html=True)
                st.markdown(f"<div style='font-size: 0.8rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px;'>Previous Broker Analyses</div>", unsafe_allow_html=True)
                def _fmt_broker_name(stem):
                    s = stem.replace("broker_analysis_", "")
                    try:
                        dt = datetime.strptime(s, "%Y-%m-%d_%H-%M")
                        return dt.strftime("%Y.%m.%d %H:%M Uhr")
                    except Exception:
                        return s.replace("_", " ")
                _active_file = st.session_state.get('selected_export', 'Data Upload')
                prev_b_opts = {f"{_fmt_broker_name(f.stem)} ({_active_file})": f for f in prev_b[:10]}
                _bsel_col, _bdel_col = st.columns([5, 1])
                with _bsel_col:
                    sel_b = st.selectbox("Select", options=["Select..."] + list(prev_b_opts.keys()), index=0, label_visibility="collapsed", key="b_analysis_selector")
                with _bdel_col:
                    if sel_b and sel_b != "Select...":
                        if st.button("🗑", key="b_del_analysis", help="Delete this analysis"):
                            st.session_state.b_confirm_delete = sel_b
                if st.session_state.get("b_confirm_delete") == sel_b and sel_b and sel_b != "Select...":
                    st.warning(f"Delete **{sel_b}**? This cannot be undone.")
                    _cb1, _cb2 = st.columns(2)
                    with _cb1:
                        if st.button("Yes, delete", key="b_confirm_yes", type="primary"):
                            prev_b_opts[sel_b].unlink()
                            st.session_state.pop("b_confirm_delete", None)
                            st.rerun()
                    with _cb2:
                        if st.button("Cancel", key="b_confirm_no"):
                            st.session_state.pop("b_confirm_delete", None)
                            st.rerun()
                elif sel_b and sel_b != "Select..." and sel_b in prev_b_opts:
                    loaded_b = prev_b_opts[sel_b].read_text(encoding='utf-8')
                    if st.session_state.broker_analysis_result != loaded_b:
                        st.session_state.broker_analysis_result = loaded_b
                        _, dp_b = build_ai_prompt(stats, trades)
                        st.session_state.broker_data_context = dp_b
                        st.session_state.broker_chat_messages = []
                        st.rerun()
