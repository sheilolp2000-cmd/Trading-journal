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

try:
    from anthropic import Anthropic
    _CLAUDE_AVAILABLE = True
except ImportError:
    _CLAUDE_AVAILABLE = False

try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

# --- Config ---
st.set_page_config(
    page_title="Hindsight Edge",
    page_icon="https://em-content.zobj.net/source/apple/391/chart-increasing_1f4c8.png",
    layout="wide",
    initial_sidebar_state="collapsed"
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

# --- Global Session Store (persists across page reloads) ---
@st.cache_resource
def get_session_store():
    """Global session storage that persists across page reloads (not deleted on F5)"""
    return {}

_session_store = get_session_store()

# Auto-restore session from browser localStorage on every page load
def _auto_restore_session():
    """Automatically restore session from browser localStorage without user action"""
    if 'sb_access_token' in st.session_state:
        return  # Already authenticated

    # Inject JavaScript that reads localStorage and sets window variables
    # These variables persist across Streamlit reruns
    st.markdown(f"""
    <script>
    (function() {{
        // Read session from localStorage
        const session = localStorage.getItem('tcj_session');
        if (session) {{
            try {{
                const data = JSON.parse(atob(session));
                if (data.access_token && data.user_id) {{
                    const saved = new Date(data.saved_at);
                    const now = new Date();
                    const hours = (now - saved) / (1000 * 60 * 60);

                    if (hours < 24) {{
                        // Session is valid — store in window object
                        window.tcj_session = {{
                            access_token: data.access_token,
                            refresh_token: data.refresh_token,
                            user_id: data.user_id,
                            user_email: data.user_email,
                            saved_at: data.saved_at
                        }};

                        // Signal that we found a valid session
                        if (!window.parent.document.body.dataset.tcjSessionRestored) {{
                            window.parent.document.body.dataset.tcjSessionRestored = 'true';
                            console.log('✅ Valid session found in localStorage — restoring...');
                        }}
                    }} else {{
                        // Session expired
                        localStorage.removeItem('tcj_session');
                        console.log('⏰ Session expired (24+ hours old)');
                    }}
                }}
            }} catch(e) {{
                console.log('Error reading localStorage:', e);
            }}
        }}
    }})();
    </script>
    """, unsafe_allow_html=True)

    # Try to restore from Streamlit's session state (which persists across reruns)
    if 'tcj_session' not in _session_store:
        return False

    session = _session_store.get('tcj_session', {})
    if session and session.get('access_token') and session.get('user_id'):
        st.session_state.sb_access_token = session['access_token']
        st.session_state.sb_refresh_token = session.get('refresh_token', '')
        st.session_state.sb_user_id = session['user_id']
        st.session_state.sb_user_email = session.get('user_email', '')
        return True

    return False

# Call this at the very start to auto-restore
_auto_restore_session()

def _save_session_cookies(access_token, refresh_token, user_id, user_email):
    """Save session to browser localStorage + server-side store for 24-hour persistence"""
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id,
        "user_email": user_email,
        "saved_at": str(datetime.now())
    }

    # Save to global session store (persists across Streamlit reruns)
    _session_store['tcj_session'] = {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'user_id': user_id,
        'user_email': user_email,
        'saved_at': datetime.now()
    }

    # Save to browser localStorage for cross-session persistence
    try:
        import base64
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        st.markdown(f"""
        <script>
        localStorage.setItem('tcj_session', '{encoded}');
        window.tcj_session_data = {json.dumps(data)};
        console.log('✅ Session saved (localStorage + Server Store — 24-hour persistence)');
        </script>
        """, unsafe_allow_html=True)
    except Exception:
        pass

    # Also try cookies as backup
    if _cookie_mgr is not None:
        try:
            expires_at = datetime.now() + timedelta(hours=24)
            _cookie_mgr.set("tcj_access_token", access_token, key="set_at", expires_at=expires_at)
            _cookie_mgr.set("tcj_refresh_token", refresh_token, key="set_rt", expires_at=expires_at)
            _cookie_mgr.set("tcj_user_id", user_id, key="set_uid", expires_at=expires_at)
            _cookie_mgr.set("tcj_user_email", user_email, key="set_em", expires_at=expires_at)
        except Exception:
            pass

def _clear_session_cookies():
    """Clear session from localStorage, cookies, and query params"""
    st.markdown("""
    <script>
    localStorage.removeItem('tcj_session');
    delete window.tcj_session_data;
    const params = new URLSearchParams(window.location.search);
    params.delete('tcj_token');
    params.delete('tcj_uid');
    params.delete('tcj_email');
    params.delete('tcj_refresh');
    window.location.search = params.toString();
    console.log('✅ Session cleared from all storage');
    </script>
    """, unsafe_allow_html=True)

    if _cookie_mgr is None:
        return
    for name, key in [("tcj_access_token","del_at"),("tcj_refresh_token","del_rt"),("tcj_user_id","del_uid"),("tcj_user_email","del_em")]:
        try:
            _cookie_mgr.delete(name, key=key)
        except Exception:
            pass

def _restore_session_from_cookies():
    """Restore session from global server store (persists across page reloads)"""
    if 'sb_access_token' in st.session_state:
        return True

    # Try to restore from global session store first (survives page reloads)
    if 'tcj_session' in _session_store:
        session = _session_store['tcj_session']
        if isinstance(session, dict) and session.get('access_token') and session.get('user_id'):
            # Check if session is still valid (< 24 hours)
            saved_at = session.get('saved_at')
            if isinstance(saved_at, datetime):
                hours_old = (datetime.now() - saved_at).total_seconds() / 3600
                if hours_old < 24:
                    st.session_state.sb_access_token = session['access_token']
                    st.session_state.sb_refresh_token = session.get('refresh_token', '')
                    st.session_state.sb_user_id = session['user_id']
                    st.session_state.sb_user_email = session.get('user_email', '')
                    return True
            else:
                # Fallback if saved_at is a string
                st.session_state.sb_access_token = session['access_token']
                st.session_state.sb_refresh_token = session.get('refresh_token', '')
                st.session_state.sb_user_id = session['user_id']
                st.session_state.sb_user_email = session.get('user_email', '')
                return True

    # Try to restore from cookies as fallback
    if _cookie_mgr is not None:
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
        except Exception as e:
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
    """Upload image to Supabase Storage (private bucket), return the storage path or None."""
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
        return path
    except Exception:
        return None

_SCREENSHOT_PUBLIC_PREFIX = f"{_SB_URL}/storage/v1/object/public/Trade%20screenshot/"

def _sb_signed_url(bucket, path, token, expires_in=3600):
    """Create a short-lived signed URL for a private bucket object. Returns URL or None."""
    url = f"{_SB_URL}/storage/v1/object/sign/{bucket}/{urllib.parse.quote(path)}"
    data, code = _http("POST", url, _sb_headers(token), {"expiresIn": expires_in})
    if code == 200 and "signedURL" in data:
        return f"{_SB_URL}/storage/v1{data['signedURL']}"
    return None

def _resolve_screenshot_url(stored_value, token):
    """Turn a stored screenshot reference (bare path, or a legacy public URL from
    before the bucket was made private) into a fresh signed URL."""
    if not stored_value:
        return None
    path = stored_value
    if stored_value.startswith("http"):
        if _SCREENSHOT_PUBLIC_PREFIX in stored_value:
            path = urllib.parse.unquote(stored_value.split(_SCREENSHOT_PUBLIC_PREFIX, 1)[1])
        else:
            return stored_value  # unrecognized absolute URL — pass through as-is
    return _sb_signed_url("Trade%20screenshot", path, token) or stored_value

def _download_image_bytes(url_or_data):
    """Download image from URL or decode base64, return (bytes, mime_type) or (None, None)."""
    try:
        # Check if it's base64 data
        if isinstance(url_or_data, str) and url_or_data.startswith('data:image'):
            # Parse data URL
            header, data = url_or_data.split(',', 1)
            mime_type = header.split(':')[1].split(';')[0]
            import base64
            img_bytes = base64.b64decode(data)
            return img_bytes, mime_type

        # Try as URL
        if isinstance(url_or_data, str) and (url_or_data.startswith('http://') or url_or_data.startswith('https://')):
            req = urllib.request.Request(url_or_data, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                return resp.read(), content_type
    except Exception:
        pass

    return None, None

def _collect_trade_screenshots(trades, token=None):
    """Collect all screenshots from journal trades, return list of (bytes, mime_type, trade_name)."""
    images = []
    for t in trades:
        # Screenshots are stored as bare storage paths (or legacy public URLs) —
        # resolve to a fresh signed URL before downloading.
        for screenshot_data in t.get("screenshots", []):
            if not screenshot_data:
                continue

            img_bytes, mime = None, None

            if isinstance(screenshot_data, str):
                resolved = _resolve_screenshot_url(screenshot_data, token) if token else screenshot_data
                img_bytes, mime = _download_image_bytes(resolved)
            elif isinstance(screenshot_data, bytes):
                img_bytes, mime = screenshot_data, "image/jpeg"

            if img_bytes:
                _label = (
                    f"{t.get('name', 'Trade')} — {t.get('pair', '')} {t.get('direction', '')} "
                    f"— Journal says: {t.get('profit_loss', 'Unknown')}"
                )
                images.append((img_bytes, mime, _label))

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

# --- Professional Trading Design CSS ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
    --bg: #05070d;
    --surface: #0d1117;
    --surface-2: #131a26;
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.16);
    --accent: #06b6d4;
    --accent-2: #8b5cf6;
    --green: #10b981;
    --red: #ef4444;
    --yellow: #f59e0b;
    --text: #e6edf3;
    --text-dim: #94a3b8;
    --text-bright: #ffffff;
}

* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

body, .stApp { background: var(--bg); color: var(--text); }

.main .block-container {
    padding: 1.5rem 3rem 4rem 3rem;
    max-width: 1440px;
    margin: 0 auto;
}

/* --- Hide Streamlit chrome + sidebar --- */
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }

/* --- Metric cards --- */
[data-testid="stMetric"] {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.1rem 1.4rem;
    transition: border-color 0.2s ease;
}
[data-testid="stMetric"]:hover { border-color: var(--border-strong); }
[data-testid="stMetric"] label {
    color: var(--text-dim) !important;
    font-size: 0.72rem !important;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: var(--text-bright) !important;
    font-size: 1.7rem !important;
    font-weight: 700;
    line-height: 1.2;
}

/* --- Tabs --- */
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    background: transparent;
    border-bottom: 1px solid var(--border);
    margin-bottom: 1.5rem;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 0;
    color: var(--text-dim);
    font-weight: 500;
    padding: 0.7rem 1.4rem;
    font-size: 0.9rem;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--text-bright) !important; background: transparent; }
.stTabs [aria-selected="true"] {
    background: transparent !important;
    color: var(--text-bright) !important;
    border-bottom: 2px solid var(--accent) !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }

/* --- Buttons --- */
.stButton > button {
    background: var(--surface-2) !important;
    color: var(--text) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.88rem;
    padding: 0.55rem 1.1rem !important;
    transition: all 0.15s ease;
    box-shadow: none;
}
.stButton > button:hover {
    border-color: var(--accent) !important;
    color: var(--text-bright) !important;
    background: var(--surface-2) !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #06b6d4, #0e7490) !important;
    color: #ffffff !important;
    border: none !important;
}
.stButton > button[kind="primary"]:hover {
    filter: brightness(1.12);
    transform: translateY(-1px);
    box-shadow: 0 6px 20px rgba(6, 182, 212, 0.25);
}

/* Prominent Start Analysis buttons */
.st-key-j_coach_btn_top button, .st-key-b_coach_btn_top button {
    font-size: 1rem !important;
    font-weight: 700 !important;
    padding: 0.9rem 1.5rem !important;
    border-radius: 12px !important;
}

/* --- Inputs --- */
.stTextInput input, .stNumberInput input, .stTextArea textarea, .stDateInput input {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
    caret-color: var(--text-bright);
}
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus, .stDateInput input:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 1px var(--accent) !important;
}
[data-baseweb="input"], [data-baseweb="textarea"] {
    background: transparent !important;
    border: none !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder { color: #5b6878 !important; }
.stNumberInput button {
    background: var(--surface-2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-dim) !important;
}
label, .stSelectbox label, .stMultiSelect label {
    color: var(--text-dim) !important;
    font-size: 0.8rem !important;
    font-weight: 500 !important;
}

/* --- Select / dropdowns --- */
[data-baseweb="select"] > div {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    color: var(--text) !important;
}
[data-baseweb="select"] span { color: var(--text) !important; }
[data-baseweb="select"] svg { fill: var(--text-dim) !important; }
[data-baseweb="popover"] > div, [data-baseweb="popover"] [role="listbox"] {
    background: var(--surface-2) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
}
[data-baseweb="popover"] li, [role="option"] { color: var(--text) !important; background: transparent !important; }
[data-baseweb="popover"] li:hover, [role="option"]:hover { background: rgba(255, 255, 255, 0.06) !important; }
[aria-selected="true"][role="option"] { background: rgba(6, 182, 212, 0.12) !important; }

/* Multiselect tags */
[data-baseweb="tag"] {
    background: rgba(139, 92, 246, 0.15) !important;
    border: 1px solid rgba(139, 92, 246, 0.25) !important;
    color: #c4b5fd !important;
    border-radius: 6px !important;
}
[data-baseweb="tag"] span { color: #c4b5fd !important; }

/* --- File uploader --- */
[data-testid="stFileUploaderDropzone"] {
    background: var(--surface) !important;
    border: 1px dashed var(--border-strong) !important;
    border-radius: 12px !important;
}
[data-testid="stFileUploader"] label { color: var(--text-dim) !important; }
[data-testid="stFileUploader"] span { color: var(--text-dim) !important; }
[data-testid="stFileUploaderDropzone"] button {
    background: var(--surface-2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 8px !important;
}

/* --- Popover trigger (calendar jump) --- */
[data-testid="stPopover"] button {
    background: var(--surface-2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
}

/* --- Checkbox / radio --- */
.stCheckbox label span, .stRadio label span { color: var(--text) !important; }

/* --- Markdown / typography --- */
.stMarkdown, .stMarkdown p { color: #cbd5e1 !important; font-size: 0.95rem; line-height: 1.65; }
.stMarkdown li { color: #cbd5e1 !important; }
.stMarkdown h1 { color: var(--text-bright) !important; font-size: 2rem !important; font-weight: 700 !important; letter-spacing: -0.02em; margin: 1.5rem 0 1rem !important; }
.stMarkdown h2 { color: var(--text-bright) !important; font-size: 1.5rem !important; font-weight: 600 !important; letter-spacing: -0.01em; margin: 1.4rem 0 0.8rem !important; }
.stMarkdown h3 { color: var(--text-bright) !important; font-size: 1.15rem !important; font-weight: 600 !important; margin: 1rem 0 0.5rem !important; }
.stMarkdown h4 { color: var(--text) !important; font-size: 1rem !important; font-weight: 600 !important; }
.stMarkdown strong { color: var(--text-bright) !important; font-weight: 700; }
.stMarkdown a { color: var(--accent) !important; text-decoration: none; }
.stMarkdown a:hover { text-decoration: underline; }
.stMarkdown table { border-collapse: collapse; width: 100%; }
.stMarkdown th, .stMarkdown td { border: 1px solid var(--border-strong) !important; padding: 9px 13px !important; color: var(--text) !important; font-size: 0.88rem; }
.stMarkdown th { background: rgba(255, 255, 255, 0.04) !important; color: var(--text-bright) !important; font-weight: 600; }

/* --- Dividers --- */
hr { border-color: var(--border) !important; margin: 1.5rem 0 !important; }

/* --- Expander --- */
[data-testid="stExpander"] {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
}
[data-testid="stExpander"] summary { background: transparent !important; color: var(--text-bright) !important; padding: 12px 16px; }
[data-testid="stExpander"] summary:hover { background: rgba(255, 255, 255, 0.04) !important; }
[data-testid="stExpander"] summary span { color: var(--text-bright) !important; }
[data-testid="stExpander"] summary svg { fill: var(--text) !important; }
[data-testid="stExpander"] [data-testid="stExpanderDetails"] { background: var(--surface); border-top: 1px solid var(--border); }

/* --- Plotly charts --- */
.stPlotlyChart {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
}

/* --- Analysis container --- */
.analysis-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 28px 32px;
    margin-top: 16px;
}
.analysis-box table { border-collapse: collapse; width: 100%; }
.analysis-box th, .analysis-box td { border: 1px solid var(--border-strong) !important; padding: 9px 13px !important; color: var(--text) !important; }
.analysis-box th { background: rgba(255, 255, 255, 0.04) !important; color: var(--text-bright) !important; font-weight: 600; }

/* --- Alerts --- */
.stAlert {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px;
    color: var(--text) !important;
}

/* --- Chat input --- */
[data-testid="stChatInput"] {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
}
[data-testid="stChatInput"] textarea, [data-testid="stChatInput"] input {
    color: var(--text-bright) !important;
    background: transparent !important;
    caret-color: var(--text-bright) !important;
}
[data-testid="stChatInput"] textarea::placeholder { color: #5b6878 !important; }
[data-testid="stChatInput"] button { color: var(--accent) !important; }
.stChatFloatingInputContainer, [data-testid="stBottom"] { background: var(--bg) !important; }
[data-testid="stBottom"] > div { background: var(--bg) !important; }

/* --- Spinner --- */
.stSpinner > div { color: var(--text) !important; }

/* --- Scrollbar --- */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.12); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.2); }

/* --- Trade card chevron button --- */
.trade-card-btn button {
    background: transparent !important;
    border: none !important;
    color: var(--text-dim) !important;
    padding: 8px 4px !important;
    font-size: 1.1rem !important;
    height: 100% !important;
}
.trade-card-btn button:hover { color: var(--accent) !important; }

/* --- Detail panel fields --- */
.detail-field {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
}
.detail-field:last-child { border-bottom: none; }
.detail-label { color: var(--text-dim); font-size: 0.84rem; }
</style>
""", unsafe_allow_html=True)


# --- Plotly Theme ---
PLOTLY_LAYOUT = dict(
    template='none',
    paper_bgcolor='#0d1117',
    plot_bgcolor='#0d1117',
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
    """Build the prompt for Hindsight Edge."""

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
All 8 sections are mandatory. Skip NONE. Section 2 (Focus Plan) is the most important — it contains 5 concrete instructions the trader must change IMMEDIATELY.
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
All 8 sections are mandatory. Skip NONE. Section 2 (Focus Plan) is the most important.

If trade screenshots are provided, analyze them as part of the trade context — look at chart patterns, entry/exit points, market structure, and any visible mistakes or good decisions. Reference specific screenshots in your analysis where relevant.
"""
    return system_prompt, data_summary


def build_candle_ai_prompt(journal_trades, screenshot_count=0):
    """Build the pre-entry candle-reading prompt (vision-only pass over trade screenshots)."""
    prompt_path = Path(__file__).parent / "candle_analysis_prompt.md"
    system_prompt = prompt_path.read_text(encoding='utf-8')

    jt = [t for t in journal_trades if t.get('screenshots')]
    if not jt:
        return system_prompt, ""

    data = ["## Trades attached as screenshots\n",
            "Metadata is context only. Read the CHARTS — if they disagree, report the disagreement.\n"]
    for t in jt:
        data.append(
            f"- **{t.get('name','?')}** | {t.get('pair','')} | {t.get('direction','')} | "
            f"opened {t.get('open','')} | journal says: {t.get('profit_loss','?')} "
            f"({t.get('gross_pnl',0)} USDT) | strategy: {t.get('strategy','') or '—'} | "
            f"{len(t.get('screenshots', []))} screenshot(s)"
        )
        if t.get('additions'):
            data.append(f"  - trader's note: {t['additions']}")

    data.append(f"""
---

You have been given {screenshot_count} screenshot(s), each labelled with its trade name.

Do the pre-entry candle analysis now, exactly in the format from your system prompt.

Work through the screenshots ONE AT A TIME. For each: locate the left edge of the
position box first, then read the ~20 candles to the LEFT of it. Only after every
screenshot has its own reading do you write the Cross-Trade Pattern section.

Report honestly what you can and cannot resolve in each image.""")
    return system_prompt, "\n".join(data)


def _collect_candle_screenshots(trades, token=None):
    """Like _collect_trade_screenshots but labels each image with its trade context."""
    images = []
    for t in trades:
        _shots = t.get("screenshots", []) or []
        for _i, screenshot_data in enumerate(_shots):
            if not screenshot_data:
                continue

            img_bytes, mime = None, None
            if isinstance(screenshot_data, str):
                resolved = _resolve_screenshot_url(screenshot_data, token) if token else screenshot_data
                img_bytes, mime = _download_image_bytes(resolved)
            elif isinstance(screenshot_data, bytes):
                img_bytes, mime = screenshot_data, "image/jpeg"

            if img_bytes:
                _label = (f"{t.get('name', 'Trade')} | {t.get('pair', '')} "
                          f"{t.get('direction', '')} | image {_i + 1} of {len(_shots)}")
                images.append((img_bytes, mime, _label))
    return images


def call_gemini_with_images(system_prompt, user_prompt, images, max_tokens=8000):
    """Call Gemini 3 Flash with text + images using file upload. images = list of (bytes, mime_type, label)."""
    import google.generativeai as genai
    import tempfile
    import os

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set."

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-3-flash-preview',
        system_instruction=system_prompt
    )

    # Build content with text and images
    content_parts = [user_prompt]

    # Upload and add images if available
    uploaded_files = []
    if images:
        for img_bytes, mime_type, label in images:
            try:
                # Create temporary file for image
                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name

                # Upload file to Gemini
                uploaded_file = genai.upload_file(tmp_path, mime_type=mime_type)
                uploaded_files.append(uploaded_file)

                # Add file reference and label to content
                content_parts.append(f"\n[Trade Screenshot: {label}]")
                content_parts.append(uploaded_file)

                # Clean up temp file
                try:
                    os.unlink(tmp_path)
                except:
                    pass

            except Exception as e:
                # If image fails, just add text label
                content_parts.append(f"\n[Trade Screenshot: {label} - image unavailable]")

    try:
        response = model.generate_content(
            content_parts,
            generation_config=genai.types.GenerationConfig(temperature=0.7, max_output_tokens=max_tokens)
        )
        return response.text
    finally:
        # Delete uploaded files to clean up quota
        for file in uploaded_files:
            try:
                genai.delete_file(file.name)
            except:
                pass



def call_gemini(system_prompt, user_prompt):
    """Call Gemini 3 Flash for analysis."""
    import google.generativeai as genai

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set. Add it in Streamlit Secrets or .env!"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-3-flash-preview',
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
    """Call Gemini 3 Flash with full chat history."""
    import google.generativeai as genai

    api_key = _get_secret('GEMINI_API_KEY') or _get_secret('GOOGLE_API_KEY')
    if not api_key:
        return "GEMINI_API_KEY not set. Add it in Streamlit Secrets or .env!"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name='gemini-3-flash-preview',
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
# CLAUDE API FUNCTIONS
# =====================================================

def call_claude(system_prompt, user_prompt):
    """Call Claude Opus 4.6 for analysis."""
    from anthropic import Anthropic

    api_key = _get_secret('ANTHROPIC_API_KEY')
    if not api_key:
        return "ANTHROPIC_API_KEY not set. Add it in Streamlit Secrets or .env!"

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model='claude-opus-4-8',
        max_tokens=8000,
        temperature=0.7,
        system=system_prompt,
        messages=[
            {'role': 'user', 'content': user_prompt}
        ]
    )
    return response.content[0].text


def call_claude_with_images(system_prompt, user_prompt, images):
    """Call Claude Opus 4.6 with text + images. images = list of (bytes, mime_type, label)."""
    from anthropic import Anthropic
    import base64

    api_key = _get_secret('ANTHROPIC_API_KEY')
    if not api_key:
        return "ANTHROPIC_API_KEY not set."

    client = Anthropic(api_key=api_key)

    # Build content with images
    content = []
    for img_bytes, mime_type, label in images:
        b64_data = base64.standard_b64encode(img_bytes).decode('utf-8')
        content.append({
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': mime_type,
                'data': b64_data
            }
        })
        content.append({
            'type': 'text',
            'text': f'\n[Screenshot: {label}]'
        })
    content.append({'type': 'text', 'text': user_prompt})

    response = client.messages.create(
        model='claude-opus-4-8',
        max_tokens=8000,
        temperature=0.7,
        system=system_prompt,
        messages=[
            {'role': 'user', 'content': content}
        ]
    )
    return response.content[0].text


def call_claude_chat(chat_history):
    """Call Claude Opus 4.6 with full chat history."""
    from anthropic import Anthropic

    api_key = _get_secret('ANTHROPIC_API_KEY')
    if not api_key:
        return "ANTHROPIC_API_KEY not set. Add it in Streamlit Secrets or .env!"

    client = Anthropic(api_key=api_key)

    # Build Claude messages (skip system prompt which is first in list)
    messages = []
    for msg in chat_history[1:]:  # skip system prompt
        messages.append({
            'role': msg['role'],
            'content': msg['content']
        })

    response = client.messages.create(
        model='claude-opus-4-8',
        max_tokens=4000,
        temperature=0.7,
        system=chat_history[0]['content'],  # system prompt
        messages=messages
    )
    return response.content[0].text


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
            cum = cum.sort_values('date', kind='stable').reset_index(drop=True)
            # Journal trades are date-only → multiple trades on the same day would
            # stack on one x-coordinate and collapse the curve. If there is no
            # intraday time info, spread same-day trades across the day by their
            # order so each trade renders as a distinct point. Broker data (which
            # has real timestamps) is left untouched.
            _has_time = (
                cum['date'].dt.hour.ne(0)
                | cum['date'].dt.minute.ne(0)
                | cum['date'].dt.second.ne(0)
            ).any()
            if not _has_time and len(cum) > 1:
                _day = cum['date'].dt.normalize()
                _seq = cum.groupby(_day).cumcount()
                _cnt = cum.groupby(_day)['pnl'].transform('size')
                cum['date'] = _day + pd.to_timedelta((_seq + 1) / (_cnt + 1) * 24.0, unit='h')
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

# =====================================================
# STARTUP TEMPLATE REDESIGN - Header + Hero (Always visible)
# =====================================================

# --- Check if authenticated (before rendering header) ---
_is_authenticated_header = _restore_session_from_cookies()
_user_email = st.session_state.get("sb_user_email", "")

# --- Top Bar ---
_topbar_right = (
    f'<span style="color:#64748b;font-size:0.8rem;">{_html.escape(_user_email)}</span>'
    if _user_email else
    '<span style="color:#64748b;font-size:0.8rem;">AI Trading Journal</span>'
)
st.markdown(f"""
<div style="position: fixed; top: 0; left: 0; right: 0; z-index: 1000; background: rgba(5, 7, 13, 0.88); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255, 255, 255, 0.08); height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 32px;">
    <div style="display: flex; align-items: center; gap: 10px;">
        <div style="width: 26px; height: 26px; border-radius: 7px; background: linear-gradient(135deg, #06b6d4, #8b5cf6);"></div>
        <span style="font-size: 1rem; font-weight: 700; color: #ffffff; letter-spacing: -0.01em;">Hindsight Edge</span>
    </div>
    {_topbar_right}
</div>
<div style="height: 64px;"></div>
""", unsafe_allow_html=True)

# --- Hero (only shown to logged-out visitors) ---
if not _is_authenticated_header:
    st.markdown("""
    <div style="text-align: center; padding: 64px 24px 40px;">
        <div style="display: inline-block; background: rgba(6, 182, 212, 0.08); border: 1px solid rgba(6, 182, 212, 0.25); color: #22d3ee; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; padding: 6px 14px; border-radius: 999px; margin-bottom: 28px;">AI-Powered Trading Journal</div>
        <h1 style="font-size: 3.4rem; font-weight: 800; color: #ffffff; letter-spacing: -0.03em; line-height: 1.12; margin: 0 0 20px;">Know exactly why you win &mdash;<br>and why you lose.</h1>
        <p style="font-size: 1.05rem; color: #94a3b8; max-width: 580px; margin: 0 auto; line-height: 1.7;">Import your broker trades, journal your setups, and let AI surface the patterns that cost you money.</p>
    </div>
    """, unsafe_allow_html=True)

# --- Auth Check (after hero) ---
_is_authenticated = _restore_session_from_cookies()

if not _is_authenticated:
    _cta_col1, _cta_col2, _cta_col3 = st.columns([1, 1.2, 1])
    with _cta_col2:
        _lt, _st = st.tabs(["Login", "Sign Up"])
        with _lt:
            st.markdown(f"<div style='font-size: 0.95rem; color: {COLORS['text_bright']}; font-weight: 600; margin-bottom: 16px;'>Login to Your Account</div>", unsafe_allow_html=True)
            _email = st.text_input("Email", key="login_email", placeholder="your@email.com")
            _pw = st.text_input("Password", type="password", key="login_pw", placeholder="••••••••")
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
            st.markdown(f"<div style='font-size: 0.95rem; color: {COLORS['text_bright']}; font-weight: 600; margin-bottom: 16px;'>Create Your Account</div>", unsafe_allow_html=True)
            _email2 = st.text_input("Email", key="signup_email", placeholder="your@email.com")
            _pw2 = st.text_input("Password (min 6 chars)", type="password", key="signup_pw", placeholder="••••••••")
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

    st.stop()

# --- Token validation (only if authenticated) ---
if not _ensure_valid_token():
    st.warning("Session abgelaufen — bitte neu einloggen.")
    _clear_session_cookies()
    for _k in ['sb_access_token', 'sb_refresh_token', 'sb_user_id', 'sb_user_email', 'journal_trades']:
        st.session_state.pop(_k, None)
    st.rerun()

st.markdown("<div style='height: 20px'></div>", unsafe_allow_html=True)

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
if 'viewing_trade_id' not in st.session_state:
    st.session_state.viewing_trade_id = None
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'chat_messages' not in st.session_state:
    st.session_state.chat_messages = []
if 'data_context' not in st.session_state:
    st.session_state.data_context = None

# --- Page Navigation ---
if 'page_nav' not in st.session_state:
    st.session_state.page_nav = "journal"
_current_page = st.session_state.get('page_nav', 'journal')

_nav_active = "background: rgba(6, 182, 212, 0.12) !important; border: 1px solid rgba(6, 182, 212, 0.5) !important; color: #ffffff !important;"
_nav_idle = "background: transparent !important; border: 1px solid rgba(255, 255, 255, 0.08) !important; color: #94a3b8 !important;"

st.markdown(f"""
<style>
.st-key-btn_journal button {{ {_nav_active if _current_page == 'journal' else _nav_idle} font-size: 0.95rem !important; font-weight: 600 !important; padding: 0.85rem 1rem !important; border-radius: 12px !important; }}
.st-key-btn_import button {{ {_nav_active if _current_page == 'import' else _nav_idle} font-size: 0.95rem !important; font-weight: 600 !important; padding: 0.85rem 1rem !important; border-radius: 12px !important; }}
</style>
""", unsafe_allow_html=True)

_btn_col1, _btn_col2 = st.columns([1, 1], gap="small")

with _btn_col1:
    if st.button("📓 Trading Journal", use_container_width=True, key="btn_journal"):
        st.session_state.page_nav = "journal"
        st.rerun()

with _btn_col2:
    if st.button("📊 Import Data", use_container_width=True, key="btn_import"):
        st.session_state.page_nav = "import"
        st.rerun()

st.markdown("<div style='height: 12px'></div>", unsafe_allow_html=True)

# --- No sidebar (all state managed via tabs) ---
# Tab state is handled by Streamlit automatically
_token = st.session_state.get('sb_access_token', '')
_uid = st.session_state.get('sb_user_id', '')

if 'export_files' not in st.session_state:
    st.session_state.export_files = _sb_list_exports(_uid, _token)

# Initialize variables
# AI Model selection — Gemini is the default/free model.
# Claude models are shown but locked behind a paywall (disabled for now).
if 'ai_model' not in st.session_state:
    st.session_state.ai_model = "Gemini 3 Flash"

# AI model selector (compact, right-aligned below the nav)
_mdl_spacer, _mdl_col = st.columns([2.4, 1])
with _mdl_col:
    ai_model_display = st.selectbox(
        "AI Model",
        ["Gemini 3 Flash (Free)", "🔒 Claude Opus 4.8 (Paid — coming soon)", "🔒 Claude Sonnet 4.6 (Paid — coming soon)"],
        index=0,
        key="ai_model_select",
    )
    # Claude models are locked behind a paywall — disabled for now, always fall back to Gemini.
    if "Claude" in ai_model_display:
        st.caption("🔒 Claude-Modelle kommen bald als Paid-Option — vorerst wird Gemini 3 Flash (Free) verwendet.")
st.session_state.ai_model = "Gemini 3 Flash"
ai_model = "Gemini 3 Flash"

selected_export_bytes = None

# --- Load broker data ---
df = None
trades = None
stats = None

# Load selected export file
if st.session_state.get('selected_export'):
    _token = st.session_state.get('sb_access_token', '')
    _uid = st.session_state.get('sb_user_id', '')

    if 'export_files' not in st.session_state:
        st.session_state.export_files = _sb_list_exports(_uid, _token)

    export_files = st.session_state.export_files
    selected_name = st.session_state.get('selected_export', '')

    # Find and load the file
    matching_file = next((f for f in export_files if f["name"] == selected_name), None)
    if matching_file:
        selected_path = matching_file["path"]
        with st.spinner("Loading file..."):
            selected_export_bytes = _sb_download_export(selected_path, _token)

        if selected_export_bytes:
            import io
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
if st.session_state.page_nav == "journal":
    # =====================================================
    # STRATEGY FOLDERS — scopes trade list, analytics and AI analysis
    # =====================================================
    ALL_STRATEGIES = "All Strategies"

    default_strategies = ["Trend + Trend", "Trend + Reverse", "Reversal", "Breakout", "Scalping", "Swing Trading", "Position Trading"]
    default_confluences = [
        "Strong Entry", "Weak Entry", "1H STDV 1", "1H VWAP",
        "4h Vwap Sync", "1D Vwap Sync No", "1D Vwap sync",
        "FIB 0.38", "FIB 0.5", "FIB 0.61", "FIB No",
        "TP Edge", "More Profit", "5Min VWAP 1 Rejection"
    ]

    if 'custom_strategies' not in st.session_state:
        st.session_state.custom_strategies = []

    existing_strategies = set(default_strategies) | set(st.session_state.custom_strategies)
    existing_confluences = set(default_confluences)
    for t in st.session_state.journal_trades:
        if t.get('strategy'):
            existing_strategies.add(t['strategy'])
        for c in t.get('confluences', []):
            existing_confluences.add(c)
    all_strategies = sorted(existing_strategies)
    all_confluences = sorted(existing_confluences)

    # A newly created folder is applied here, before the widget renders —
    # Streamlit forbids writing a widget's own key after instantiation.
    if 'pending_strategy_filter' in st.session_state:
        st.session_state.j_strategy_filter_box = st.session_state.pop('pending_strategy_filter')

    _strategy_options = [ALL_STRATEGIES] + all_strategies
    if st.session_state.get('j_strategy_filter_box') not in _strategy_options:
        st.session_state.j_strategy_filter_box = ALL_STRATEGIES

    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Strategy</div>
        <div style="font-size: 0.8rem; color: {COLORS['text_dim']}; margin-left: 8px;">— a separate journal per strategy</div>
    </div>
    """, unsafe_allow_html=True)

    _fc1, _fc2 = st.columns([4, 1])
    with _fc1:
        st.selectbox("Strategy filter", _strategy_options, label_visibility="collapsed", key="j_strategy_filter_box")
    with _fc2:
        if st.button("＋ New", key="j_new_strategy_btn", use_container_width=True, help="Create a new strategy folder"):
            st.session_state.show_new_strategy = not st.session_state.get('show_new_strategy', False)

    if st.session_state.get('show_new_strategy'):
        _nc1, _nc2, _nc3 = st.columns([4, 1, 1])
        with _nc1:
            _new_strat_name = st.text_input("New strategy", value="", placeholder="e.g. Reverse Strategy",
                                            label_visibility="collapsed", key="j_new_strategy_name")
        with _nc2:
            if st.button("Create", key="j_create_strategy", type="primary", use_container_width=True):
                _name = (_new_strat_name or "").strip()
                if not _name:
                    st.warning("Enter a name for the strategy.")
                elif _name == ALL_STRATEGIES or _name in all_strategies:
                    st.info(f"Strategy '{_name}' already exists.")
                else:
                    st.session_state.custom_strategies.append(_name)
                    st.session_state.pending_strategy_filter = _name
                    st.session_state.show_new_strategy = False
                    st.session_state.viewing_trade_id = None
                    st.rerun()
        with _nc3:
            if st.button("Cancel", key="j_cancel_strategy", use_container_width=True):
                st.session_state.show_new_strategy = False
                st.rerun()

    _active_strategy = st.session_state.j_strategy_filter_box
    _strategy_scoped = _active_strategy != ALL_STRATEGIES

    def _in_active_strategy(_t):
        return (not _strategy_scoped) or (_t.get('strategy') or '') == _active_strategy

    # Everything below this point operates on the selected folder only.
    _jt_coach = [t for t in st.session_state.journal_trades if _in_active_strategy(t)]
    _jt_count = len(_jt_coach)

    _scope_label = _active_strategy if _strategy_scoped else "all strategies"
    st.markdown(
        f'<div style="font-size:0.78rem;color:{COLORS["text_dim"]};margin:10px 0 28px 0;">'
        f'Showing <b style="color:{COLORS["text_bright"]};">{_jt_count}</b> trade(s) — '
        f'list, analytics and AI analysis are scoped to <b style="color:{COLORS["accent_cyan"]};">{_html.escape(str(_scope_label))}</b>.'
        f'</div>', unsafe_allow_html=True)

    # =====================================================
    # TRADING JOURNAL - HINDSIGHT EDGE ANALYSIS
    # =====================================================
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 32px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Hindsight Edge</div>
        <div style="font-size: 0.8rem; color: {COLORS['text_dim']}; margin-left: 8px;">— analyzes your journal trades</div>
    </div>
    """, unsafe_allow_html=True)

    # Centered Start Analysis button
    _btn_col = st.columns([1, 2, 1])
    with _btn_col[1]:
        start_j_analysis_top = st.button("⚡ Start Analysis", type="primary", use_container_width=True, key="j_coach_btn_top")
        _shots_available = sum(len(t.get('screenshots') or []) for t in _jt_coach)
        _want_candles = st.checkbox(
            f"🕯 Add pre-entry candle analysis ({_shots_available} screenshot(s))",
            value=True,
            key="j_candle_toggle",
            disabled=(_shots_available == 0),
            help="A second AI pass that reads only the candles before each entry. "
                 "Costs an extra API call and takes longer.",
        )

    # --- Previous Journal Analyses (analysis history) ---
    st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
    if _jt_count > 0:
        analyses_dir_j = Path(__file__).parent / "analyses"
        if analyses_dir_j.exists():
            prev_j = sorted(analyses_dir_j.glob("journal_analysis_*.md"), reverse=True)
            if prev_j:
                st.markdown(f"<div style='font-size: 0.8rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px;'>Previous Journal Analyses</div>", unsafe_allow_html=True)
                def _fmt_journal_name(stem):
                    s = stem.replace("journal_analysis_", "")
                    try:
                        dt = datetime.strptime(s, "%Y-%m-%d_%H-%M")
                        return dt.strftime("%Y.%m.%d %H:%M Uhr")
                    except Exception:
                        return s.replace("_", " ")
                prev_j_opts = {_fmt_journal_name(f.stem): f for f in prev_j[:10]}
                sel_j = st.selectbox("Select", options=["Select..."] + list(prev_j_opts.keys()), index=0, label_visibility="collapsed", key="j_analysis_selector", disabled=False)
                if sel_j and sel_j != "Select..." and sel_j in prev_j_opts:
                    _del_col1, _del_col2 = st.columns([9, 1])
                    with _del_col2:
                        if st.button("🗑", key="j_del_analysis", help="Delete this analysis", use_container_width=True):
                            st.session_state.j_confirm_delete = sel_j
                if st.session_state.get("j_confirm_delete") == sel_j and sel_j and sel_j != "Select...":
                    st.warning(f"Delete **{sel_j}**? This cannot be undone.")
                    _cb1, _cb2 = st.columns(2)
                    with _cb1:
                        if st.button("Yes, delete", key="j_confirm_yes", type="primary"):
                            prev_j_opts[sel_j].unlink()
                            st.session_state.pop("j_confirm_delete", None)
                            st.rerun()
                    with _cb2:
                        if st.button("Cancel", key="j_confirm_no"):
                            st.session_state.pop("j_confirm_delete", None)
                            st.rerun()

                # Show selected analysis if one is selected
                if sel_j and sel_j != "Select..." and sel_j in prev_j_opts:
                    st.markdown("<div style='height: 24px'></div>", unsafe_allow_html=True)
                    selected_file = prev_j_opts[sel_j]
                    analysis_content = selected_file.read_text(encoding='utf-8')
                    st.markdown(f'<div class="analysis-box">', unsafe_allow_html=True)
                    st.markdown(analysis_content)
                    st.markdown('</div>', unsafe_allow_html=True)

                    # Enable chat for previous analysis
                    st.session_state.analysis_result = analysis_content
                    st.session_state.data_context = f"[Previous Analysis from {sel_j}]"
                    if 'chat_messages' not in st.session_state:
                        st.session_state.chat_messages = []

    if start_j_analysis_top:
        if _jt_count == 0:
            if _strategy_scoped:
                st.warning(f"No trades in '{_active_strategy}' yet. Log trades in this strategy first, or switch to '{ALL_STRATEGIES}'.")
            else:
                st.warning("No trades in the journal yet. Log some trades first.")
        else:
            sys_p, dat_p = build_journal_ai_prompt(_jt_coach)
            _token_j = st.session_state.get('sb_access_token', '')
            _is_claude = "Claude" in ai_model
            with st.spinner("AI is analyzing your journal trades..."):
                _screenshots = _collect_trade_screenshots(_jt_coach, _token_j)
                if _is_claude:
                    if _screenshots:
                        _analysis = call_claude_with_images(sys_p, dat_p, _screenshots)
                    else:
                        _analysis = call_claude(sys_p, dat_p)
                else:
                    if _screenshots:
                        _analysis = call_gemini_with_images(sys_p, dat_p, _screenshots)
                    else:
                        _analysis = call_gemini(sys_p, dat_p)

            # --- Second pass: pre-entry candle reading (vision only) ---
            if _want_candles:
                _candle_imgs = _collect_candle_screenshots(_jt_coach, _token_j)
                if not _candle_imgs:
                    _analysis += ("\n\n---\n\n## 9. Pre-Entry Candle Analysis 🕯\n\n"
                                  "*No screenshots could be loaded for these trades, so the candle "
                                  "analysis was skipped.*\n")
                else:
                    _c_sys, _c_dat = build_candle_ai_prompt(_jt_coach, len(_candle_imgs))
                    with st.spinner(f"Reading the candles before each entry ({len(_candle_imgs)} screenshot(s))..."):
                        try:
                            if _is_claude:
                                _candle_out = call_claude_with_images(_c_sys, _c_dat, _candle_imgs)
                            else:
                                _candle_out = call_gemini_with_images(_c_sys, _c_dat, _candle_imgs, max_tokens=16000)
                        except Exception as _ce:
                            _candle_out = f"*Candle analysis failed: {_ce}*"
                    _analysis += "\n\n---\n\n" + _candle_out

            _scope_note = f"*Scope: {_scope_label} — {_jt_count} trade(s)*\n\n"
            st.session_state.analysis_result = _scope_note + _analysis
            st.session_state.data_context = f"[Strategy scope: {_scope_label}]\n{dat_p}"
            st.session_state.chat_messages = []
            _out = Path(__file__).parent / "analyses"
            _out.mkdir(exist_ok=True)
            (_out / f"journal_analysis_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.md").write_text(_scope_note + _analysis, encoding='utf-8')
            st.rerun()

    # =====================================================
    # TRADING JOURNAL - TRADE LIST
    # =====================================================
    st.markdown("<div style='height: 40px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 24px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Trading Journal</div>
    </div>
    """, unsafe_allow_html=True)

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
            Ask Hindsight Edge anything about your analysis — e.g. "Why do I lose on Fridays?" or "Which pairs are my worst?"
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

        user_question_j = st.chat_input("Ask Hindsight Edge about your journal...", key="j_coach_chat")
        if user_question_j:
            st.session_state.chat_messages.append({'role': 'user', 'content': user_question_j})
            chat_sys_j = f"""You are Hindsight Edge, an AI assistant analyzing journal trade data.

Trading data:
{st.session_state.data_context}

Your previous analysis:
{st.session_state.analysis_result}

Answer follow-up questions directly and concretely using numbers from the data. Address the trader as "you". Keep answers brief (2-5 sentences) unless details are requested. Be honest and constructive."""
            chat_hist_j = [{'role': 'system', 'content': chat_sys_j}]
            for msg in st.session_state.chat_messages:
                chat_hist_j.append(msg)
            with st.spinner(""):
                if "Claude" in ai_model:
                    response_j = call_claude_chat(chat_hist_j)
                else:
                    response_j = call_gemini_chat(chat_hist_j)
            st.session_state.chat_messages.append({'role': 'assistant', 'content': response_j})
            st.rerun()

    st.markdown("<div style='height: 40px'></div>", unsafe_allow_html=True)

    if st.button("+ Add New Trade", key="add_trade_btn"):
        st.session_state.show_add_form = not st.session_state.show_add_form
        st.session_state.editing_index = None
        # Drop the stale widget value so the active folder can prefill the field
        st.session_state.pop('j_strat', None)

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
                # New trades default into the folder that is currently selected
                _strat_default = edit_data.get('strategy') if editing else (_active_strategy if _strategy_scoped else '')
                trade_strategy = st.selectbox("Strategy", strat_options, index=strat_options.index(_strat_default) if _strat_default in strat_options else 0, key="j_strat")
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
            _screenshot_token = st.session_state.get('sb_access_token', '')
            if existing_screenshots:
                thumb_cols = st.columns(min(len(existing_screenshots), 4))
                to_remove = []
                for i, url in enumerate(existing_screenshots):
                    with thumb_cols[i % 4]:
                        st.image(_resolve_screenshot_url(url, _screenshot_token), use_container_width=True)
                        if st.button("🗑", key=f"del_img_{i}", help="Remove screenshot"):
                            to_remove.append(url)
                for url in to_remove:
                    existing_screenshots.remove(url)

            _MAX_SCREENSHOT_MB = 8
            _MAX_SCREENSHOTS_PER_TRADE = 10
            uploaded_screenshots = st.file_uploader(
                f"Add screenshots (PNG, JPG, WEBP — max {_MAX_SCREENSHOT_MB} MB each, {_MAX_SCREENSHOTS_PER_TRADE} per trade)",
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
                        _slots_left = _MAX_SCREENSHOTS_PER_TRADE - len(new_urls)
                        if len(uploaded_screenshots) > _slots_left:
                            st.warning(f"Only {max(_slots_left, 0)} more screenshot(s) allowed for this trade (max {_MAX_SCREENSHOTS_PER_TRADE}) — extra files skipped.")
                        with st.spinner("Uploading screenshots..."):
                            for f in uploaded_screenshots[:max(_slots_left, 0)]:
                                if f.size > _MAX_SCREENSHOT_MB * 1024 * 1024:
                                    st.warning(f"Skipped {f.name}: exceeds {_MAX_SCREENSHOT_MB} MB limit.")
                                    continue
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

    # --- Journal Trade List (styled cards) + Detail Panel ---
    st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

    _all_trades = _jt_coach
    _viewing_id = st.session_state.get('viewing_trade_id')
    _viewing_trade = None
    _viewing_idx = None
    if _viewing_id:
        # Resolve against the FULL list: _viewing_idx feeds edit/delete, which
        # index into st.session_state.journal_trades, not into the filtered view.
        for _vi, _vt in enumerate(st.session_state.journal_trades):
            if _vt.get('id') == _viewing_id:
                if _in_active_strategy(_vt):
                    _viewing_trade = _vt
                    _viewing_idx = _vi
                break
        if not _viewing_trade:
            st.session_state.viewing_trade_id = None
            _viewing_id = None

    if _viewing_id and _viewing_trade:
        _list_col, _detail_col = st.columns([1, 1], gap="medium")
    else:
        _list_col = st.container()
        _detail_col = None

    with _list_col:
        if not _all_trades:
            if _strategy_scoped:
                _empty_msg = f'No trades in <b style="color:{COLORS["accent_cyan"]};">{_html.escape(str(_active_strategy))}</b> yet. Click <b style="color:{COLORS["text_bright"]};">+ Add New Trade</b> — it lands in this folder automatically.'
            else:
                _empty_msg = f'No trades yet. Click <b style="color:{COLORS["text_bright"]};">+ Add New Trade</b> to get started.'
            st.markdown(f'<div style="text-align:center;color:{COLORS["text_dim"]};padding:48px 20px;"><div style="font-size:2rem;margin-bottom:8px;opacity:0.5;">📓</div><div>{_empty_msg}</div></div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div style="display:flex;align-items:center;padding:8px 16px;margin-bottom:4px;"><span style="flex:2;color:{COLORS["text_dim"]};font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;">Trade</span><span style="flex:1;color:{COLORS["text_dim"]};font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;">Pair / Session</span><span style="flex:1;color:{COLORS["text_dim"]};font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;">Strategy</span><span style="flex:1;color:{COLORS["text_dim"]};font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;text-align:right;">PnL</span><span style="width:40px;"></span></div>', unsafe_allow_html=True)

            for _idx, _t in enumerate(_all_trades):
                _pnl = float(_t.get('gross_pnl', 0))
                _pnl_color = COLORS['green'] if _pnl >= 0 else COLORS['red']
                _pnl_r2, _pnl_g2, _pnl_b2 = int(_pnl_color[1:3],16), int(_pnl_color[3:5],16), int(_pnl_color[5:7],16)
                _dir_label = 'LONG' if _t.get('direction') == 'Long' else 'SHORT'
                _dir_color = COLORS['green'] if _t.get('direction') == 'Long' else COLORS['red']
                _is_active = _viewing_id == _t.get('id')
                _active_border = COLORS['accent_cyan'] if _is_active else f"rgba({_pnl_r2},{_pnl_g2},{_pnl_b2},0.19)"
                _active_bg = COLORS['bg_card_hover'] if _is_active else COLORS['bg_card']
                _status_color = COLORS['green'] if _t.get('status') == 'Closed' else COLORS['yellow']
                _has_screenshots = bool(_t.get('screenshots'))
                _dir_bg = f"rgba({int(_dir_color[1:3],16)},{int(_dir_color[3:5],16)},{int(_dir_color[5:7],16)},0.08)"
                _screenshot_icon = ' 📷' if _has_screenshots else ''
                _pnl_sign = '+' if _pnl >= 0 else ''
                _name_esc = _html.escape(str(_t.get('name', 'Trade')))
                _strat_disp = _t.get('strategy', '') or '—'
                _session_disp = _t.get('session', '') or '—'

                _tc1, _tc2 = st.columns([14, 1])
                with _tc1:
                    _card_html = (
                        f'<div style="background:{_active_bg};border:1px solid {_active_border};border-left:3px solid {_pnl_color};border-radius:10px;padding:12px 16px;margin-bottom:2px;">'
                        f'<div style="display:flex;align-items:center;">'
                        f'<div style="flex:2;">'
                        f'<div style="font-weight:600;color:{COLORS["text_bright"]};font-size:0.9rem;">{_name_esc}<span style="font-weight:400;color:{COLORS["text_dim"]};font-size:0.75rem;margin-left:8px;">{_t.get("open","")}</span></div>'
                        f'<div style="margin-top:3px;"><span style="background:{_dir_bg};color:{_dir_color};font-size:0.65rem;padding:2px 6px;border-radius:4px;font-weight:600;">{_dir_label}</span><span style="color:{_status_color};font-size:0.72rem;margin-left:8px;">{_t.get("status","")}</span>{_screenshot_icon}</div>'
                        f'</div>'
                        f'<div style="flex:1;"><span style="color:{COLORS["text_bright"]};font-size:0.85rem;font-weight:600;">{_t.get("pair","")}</span><span style="color:{COLORS["text_dim"]};font-size:0.75rem;margin-left:6px;">· {_session_disp}</span></div>'
                        f'<div style="flex:1;color:{COLORS["text_dim"]};font-size:0.82rem;">{_strat_disp}</div>'
                        f'<div style="flex:1;text-align:right;"><div style="font-weight:700;color:{_pnl_color};font-size:1rem;">{_pnl_sign}{_pnl:.2f} $</div></div>'
                        f'</div></div>'
                    )
                    st.markdown(_card_html, unsafe_allow_html=True)
                with _tc2:
                    st.markdown("<div class='trade-card-btn'>", unsafe_allow_html=True)
                    if st.button("›", key=f"view_{_t.get('id', _idx)}", help="View trade details"):
                        st.session_state.viewing_trade_id = _t.get('id')
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

    # --- Detail Panel (right side, 50%) ---
    if _detail_col is not None and _viewing_trade:
        with _detail_col:
            # Panel with strong visual separation from bg
            st.markdown(f'<div style="background:#0d1525;border:1px solid rgba(6,182,212,0.35);border-top:3px solid {COLORS["accent_cyan"]};border-radius:16px;padding:20px 24px;box-shadow:0 8px 32px rgba(0,0,0,0.4);position:relative;">', unsafe_allow_html=True)

            # Header row: name + X button (top-right)
            _detail_name = _html.escape(str(_viewing_trade.get('name', 'Trade')))
            _dh1, _dh2 = st.columns([5, 1])
            with _dh1:
                _dir_val = _viewing_trade.get('direction', '—')
                _dir_c = COLORS['green'] if _dir_val == 'Long' else COLORS['red']
                _dir_bg2 = f"rgba({int(_dir_c[1:3],16)},{int(_dir_c[3:5],16)},{int(_dir_c[5:7],16)},0.1)"
                st.markdown(f'<div style="font-size:1.1rem;font-weight:700;color:{COLORS["text_bright"]};margin-bottom:2px;">{_detail_name}</div><div style="font-size:0.78rem;color:{COLORS["text_dim"]};">Trade Details</div>', unsafe_allow_html=True)
            with _dh2:
                if st.button("✕", key="close_detail_panel", help="Close", use_container_width=True):
                    st.session_state.viewing_trade_id = None
                    st.rerun()

            st.markdown(f'<div style="height:1px;background:linear-gradient(90deg,{COLORS["accent_cyan"]},transparent);margin:14px 0;"></div>', unsafe_allow_html=True)

            # Fields
            _fields = [
                ("Pair", _viewing_trade.get('pair', '—'), None),
                ("Direction", _dir_val, _dir_c),
                ("Session", _viewing_trade.get('session', '') or '—', None),
                ("Strategy", _viewing_trade.get('strategy', '') or '—', None),
                ("Status", _viewing_trade.get('status', '—'), COLORS['green'] if _viewing_trade.get('status') == 'Closed' else COLORS['yellow']),
                ("Open", _viewing_trade.get('open', '—'), None),
                ("Close", _viewing_trade.get('close', '—'), None),
            ]
            _fhtml = ''
            for _lbl, _val, _clr in _fields:
                _vs = f"color:{_clr};font-weight:600;" if _clr else f"color:{COLORS['text_bright']};font-weight:500;"
                _fhtml += f'<div class="detail-field"><span class="detail-label">{_lbl}</span><span style="{_vs}font-size:0.9rem;">{_html.escape(str(_val))}</span></div>'
            st.markdown(_fhtml, unsafe_allow_html=True)

            # PnL box
            _pnl_v = float(_viewing_trade.get('gross_pnl', 0))
            _pnl_c = COLORS['green'] if _pnl_v >= 0 else COLORS['red']
            _pr, _pg, _pb = int(_pnl_c[1:3],16), int(_pnl_c[3:5],16), int(_pnl_c[5:7],16)
            _net_v = float(_viewing_trade.get('net_pnl', 0))
            _fees_v = float(_viewing_trade.get('fees', 0))
            _psign = '+' if _pnl_v >= 0 else ''
            st.markdown(
                f'<div style="margin-top:16px;padding:14px 16px;background:rgba({_pr},{_pg},{_pb},0.05);border:1px solid rgba({_pr},{_pg},{_pb},0.2);border-radius:12px;display:flex;justify-content:space-between;align-items:center;">'
                f'<div><div style="color:{COLORS["text_dim"]};font-size:0.72rem;margin-bottom:2px;">Net PnL</div><div style="color:{COLORS["text_bright"]};font-weight:500;">{_net_v:.2f} $</div></div>'
                f'<div><div style="color:{COLORS["text_dim"]};font-size:0.72rem;margin-bottom:2px;">Fees</div><div style="color:{COLORS["text_bright"]};font-weight:500;">{_fees_v:.2f} $</div></div>'
                f'<div><div style="color:{COLORS["text_dim"]};font-size:0.72rem;margin-bottom:2px;">Gross PnL</div><div style="color:{_pnl_c};font-weight:700;font-size:1.2rem;">{_psign}{_pnl_v:.2f} $</div></div>'
                f'</div>', unsafe_allow_html=True)

            # Confluences
            _confs = _viewing_trade.get('confluences', [])
            if _confs and any(_confs):
                _tags = ''.join([f'<span style="background:rgba(139,92,246,0.09);color:{COLORS["accent_purple"]};padding:4px 10px;border-radius:6px;font-size:0.78rem;margin:3px 4px 3px 0;display:inline-block;border:1px solid rgba(139,92,246,0.15);">{_html.escape(c)}</span>' for c in _confs if c])
                st.markdown(f'<div style="margin-top:16px;"><div style="font-size:0.75rem;color:{COLORS["text_dim"]};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px;">Confluences</div><div>{_tags}</div></div>', unsafe_allow_html=True)

            # Notes
            _notes = _viewing_trade.get('additions', '')
            if _notes:
                st.markdown(f'<div style="margin-top:14px;"><div style="font-size:0.75rem;color:{COLORS["text_dim"]};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px;">Notes</div><div style="color:{COLORS["text"]};font-size:0.86rem;line-height:1.6;background:{COLORS["bg_dark"]};padding:10px 12px;border-radius:8px;">{_html.escape(str(_notes))}</div></div>', unsafe_allow_html=True)

            st.markdown('</div>', unsafe_allow_html=True)

            # Screenshots (below panel card)
            _screenshots = _viewing_trade.get('screenshots', [])
            if _screenshots:
                st.markdown(f'<div style="margin-top:14px;font-size:0.75rem;color:{COLORS["text_dim"]};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px;">Screenshots</div>', unsafe_allow_html=True)
                _detail_token = st.session_state.get('sb_access_token', '')
                for _surl in _screenshots:
                    st.image(_resolve_screenshot_url(_surl, _detail_token), use_container_width=True)

            # Edit / Delete
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
            _ab1, _ab2 = st.columns(2)
            with _ab1:
                if st.button("✏️ Edit", key="detail_edit_btn", use_container_width=True):
                    st.session_state.editing_index = _viewing_idx
                    st.session_state.show_add_form = True
                    st.session_state.viewing_trade_id = None
                    st.rerun()
            with _ab2:
                if st.button("🗑 Delete", key="detail_delete_btn", use_container_width=True):
                    st.session_state.journal_trades.pop(_viewing_idx)
                    save_journal(st.session_state.journal_trades)
                    st.session_state.viewing_trade_id = None
                    st.rerun()

    # --- Analytics (always shown, below table) ---
    st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px;">
        <div style="width: 3px; height: 24px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.1rem; font-weight: 600; color: {COLORS['text_bright']};">Analytics</div>
    </div>
    """, unsafe_allow_html=True)

    jt = _jt_coach
    _j_trades_df, _j_stats = journal_to_trades_and_stats(jt)
    render_analytics(_j_trades_df, _j_stats)


# =====================================================
# PAGE: IMPORT DATA
# =====================================================
if st.session_state.page_nav == "import":
    if 'broker_analysis_result' not in st.session_state:
        st.session_state.broker_analysis_result = None
    if 'broker_chat_messages' not in st.session_state:
        st.session_state.broker_chat_messages = []
    if 'broker_data_context' not in st.session_state:
        st.session_state.broker_data_context = None

    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 24px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Import Data</div>
        <div style="font-size: 0.85rem; color: {COLORS['text_dim']}; margin-left: 8px;">— Analytics and Hindsight Edge powered by your broker export (Binance, Kraken, Coinbase, Bybit, OKX, Bitget, Kucoin, Huobi, Gate.io, Deribit, etc.)</div>
    </div>
    """, unsafe_allow_html=True)

    # --- Upload Section ---
    # Centered Start Analysis button (at top, same as Trading Journal)
    _b_btn_col_top = st.columns([1, 2, 1])
    with _b_btn_col_top[1]:
        start_b_analysis = st.button("⚡ Start Analysis", type="primary", use_container_width=True,
                                     key="b_coach_btn_top", disabled=(df is None))

    st.markdown("<div style='height: 24px'></div>", unsafe_allow_html=True)

    st.markdown(f"<div style='font-size: 0.75rem; color: {COLORS['text_dim']}; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px;'>📤 Upload Broker Export</div>", unsafe_allow_html=True)

    _token = st.session_state.get('sb_access_token', '')
    _uid = st.session_state.get('sb_user_id', '')

    if 'export_files' not in st.session_state:
        st.session_state.export_files = _sb_list_exports(_uid, _token)

    export_files = st.session_state.export_files

    # Upload new file
    _upload_col1, _upload_col2, _upload_col3 = st.columns([2, 1, 1])
    with _upload_col1:
        new_upload = st.file_uploader("Choose XLSX or CSV", type=['xlsx', 'csv'], key="export_uploader_main", label_visibility="collapsed")
    with _upload_col2:
        if st.button("Upload", key="upload_btn_main", use_container_width=True):
            if new_upload:
                safe_name = new_upload.name.replace(" ", "_")
                already_uploaded = any(f["name"] == safe_name for f in export_files)
                if already_uploaded:
                    st.session_state.selected_export = safe_name
                    st.info(f"File already exists: {safe_name}")
                elif new_upload.size > 20 * 1024 * 1024:
                    st.error("File too large (max 20 MB).")
                else:
                    with st.spinner("Uploading..."):
                        path, err = _sb_upload_export(new_upload.read(), new_upload.name, _uid, _token)
                    if path:
                        st.session_state.export_files = _sb_list_exports(_uid, _token)
                        st.session_state.selected_export = safe_name
                        st.success(f"Uploaded: {safe_name}")
                        st.rerun()
                    else:
                        st.error(f"Upload failed: {err}")
            else:
                st.warning("Select a file first")
    with _upload_col3:
        if st.button("↻", key="refresh_exports_main", use_container_width=True, help="Refresh list"):
            st.session_state.export_files = _sb_list_exports(_uid, _token)
            st.rerun()

    # File selector dropdown
    if export_files:
        file_names = [f["name"] for f in export_files]
        default_idx = 0
        if 'selected_export' in st.session_state and st.session_state.selected_export in file_names:
            default_idx = file_names.index(st.session_state.selected_export)

        selected_name = st.selectbox("Select Dataset", file_names, index=default_idx, key="export_select_main", label_visibility="collapsed")
        st.session_state.selected_export = selected_name

    # --- Previous Broker Analyses (moved to top) ---
    st.markdown("<div style='height: 32px'></div>", unsafe_allow_html=True)
    if df is not None:
        analyses_dir_b = Path(__file__).parent / "analyses"
        if analyses_dir_b.exists():
            prev_b = sorted(analyses_dir_b.glob("broker_analysis_*.md"), reverse=True)
            if prev_b:
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
                sel_b = st.selectbox("Select", options=["Select..."] + list(prev_b_opts.keys()), index=0, label_visibility="collapsed", key="b_analysis_selector", disabled=False)
                if sel_b and sel_b != "Select..." and sel_b in prev_b_opts:
                    _del_col1, _del_col2 = st.columns([9, 1])
                    with _del_col2:
                        if st.button("🗑", key="b_del_analysis", help="Delete this analysis", use_container_width=True):
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

    if df is None:
        st.info("📤 Load your broker export above to enable the AI Coach.")
    elif start_b_analysis:
        system_prompt_b, data_prompt_b = build_ai_prompt(stats, trades)
        with st.spinner("AI is analyzing your broker trades..."):
            if "Claude" in ai_model:
                analysis_b = call_claude(system_prompt_b, data_prompt_b)
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

        user_question_b = st.chat_input("Ask Hindsight Edge about your broker data...", key="b_coach_chat")
        if user_question_b:
            st.session_state.broker_chat_messages.append({'role': 'user', 'content': user_question_b})
            chat_sys_b = f"""You are Hindsight Edge, an AI assistant analyzing broker trade data.

Trading data:
{st.session_state.broker_data_context}

Your previous analysis:
{st.session_state.broker_analysis_result}

Answer follow-up questions directly and concretely using numbers from the data. Address the trader as "you". Keep answers brief (2-5 sentences) unless details are requested. Be honest and constructive."""
            chat_hist_b = [{'role': 'system', 'content': chat_sys_b}]
            for msg in st.session_state.broker_chat_messages:
                chat_hist_b.append(msg)
            with st.spinner(""):
                if "Claude" in ai_model:
                    response_b = call_claude_chat(chat_hist_b)
                else:
                    response_b = call_gemini_chat(chat_hist_b)
            st.session_state.broker_chat_messages.append({'role': 'assistant', 'content': response_b})
            st.rerun()

    # --- Analytics (placed after AI Coach) ---
    st.markdown("<div style='height: 60px'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 24px;">
        <div style="width: 3px; height: 28px; background: linear-gradient(180deg, {COLORS['accent_cyan']}, {COLORS['accent_purple']}); border-radius: 2px;"></div>
        <div style="font-size: 1.3rem; font-weight: 700; color: {COLORS['text_bright']};">Analytics</div>
    </div>
    """, unsafe_allow_html=True)

    if df is None:
        render_analytics(pd.DataFrame(columns=['date','pnl','asset','is_win']), None, tab_prefix='broker')
        st.info("📤 Upload your broker export to see detailed analytics.")
    else:
        _b_trades = trades.rename(columns={'date': 'date', 'pnl': 'pnl'}).copy()
        _b_trades['asset'] = _b_trades['asset'] if 'asset' in _b_trades.columns else 'Unknown'
        _b_trades['is_win'] = _b_trades['pnl'] > 0
        render_analytics(_b_trades[['date','pnl','asset','is_win']], stats, tab_prefix='broker')
