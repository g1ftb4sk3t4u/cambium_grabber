"""Login to the Cambium Networks support portal (support.cambiumnetworks.com).

Unlike MikroTik's open CDN, every download here sits behind a real account
login - there's no anonymous catalog to crawl. This replicates the site's
two-step web form (email, then password) with plain requests; no browser
automation needed at runtime, since the form is a standard POST with CSRF
tokens rather than a JS-rendered SPA (verified by walking the real flow with
a browser once and comparing against what a bare requests.Session sees).

Session cookies are cached to disk so a scheduled run doesn't log in fresh
every time - re-authenticates automatically only when the cached session
has actually expired.
"""

import json
import re
from pathlib import Path

import requests

LOGIN_URL = "https://support.cambiumnetworks.com/login"
FILES_URL = "https://support.cambiumnetworks.com/files"

_CSRF_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_CSRF_TS_RE = re.compile(r'name="csrf_timestamp"\s+value="([^"]+)"')
_HAS_PASSWORD_FIELD_RE = re.compile(r'name="password"')


class LoginError(RuntimeError):
    """Raised when the login flow doesn't match what this module expects -
    e.g. Cambium added MFA, changed the form, or the account hit an SSO
    redirect instead of a password prompt. Fails loudly rather than
    guessing, since a silent wrong-turn here would look like "0 files
    found" instead of "couldn't log in".
    """


def _parse_csrf(html: str):
    token = _CSRF_TOKEN_RE.search(html)
    ts = _CSRF_TS_RE.search(html)
    if not token or not ts:
        raise LoginError("Could not find csrf_token/csrf_timestamp on the login page - Cambium may have changed the form")
    return token.group(1), ts.group(1)


def is_authenticated(session: requests.Session) -> bool:
    """A logged-out session hitting /files gets redirected to /login."""
    r = session.get(FILES_URL, allow_redirects=True, timeout=15)
    return "/login" not in r.url and r.status_code == 200


def login(session: requests.Session, email: str, password: str, remember: bool = True) -> None:
    """Two-step form login: identify by email, then submit password.
    Raises LoginError on anything unexpected (MFA, SSO, changed form).
    """
    r1 = session.get(LOGIN_URL, params={"camefrom": FILES_URL}, timeout=15)
    csrf_token, csrf_ts = _parse_csrf(r1.text)

    # Referer matters here - without it (or with a non-browser User-Agent,
    # see BROWSER_USER_AGENT below) the site accepts the POST and returns
    # 200, but silently doesn't authenticate the session. Found by diffing
    # a real browser's login flow against a bare requests.Session.
    r2 = session.post(LOGIN_URL, data={
        "csrf_token": csrf_token,
        "csrf_timestamp": csrf_ts,
        "camefrom": FILES_URL,
        "email": email,
        "next": "Next",
    }, headers={"Referer": r1.url}, timeout=15)

    if not _HAS_PASSWORD_FIELD_RE.search(r2.text):
        raise LoginError(
            "No password field after submitting email - this account may use SSO or "
            "the login flow changed. Automated login can't proceed; log in manually "
            "in a browser once and see what's different."
        )

    csrf_token2, csrf_ts2 = _parse_csrf(r2.text)
    r3 = session.post(LOGIN_URL, data={
        "csrf_token": csrf_token2,
        "csrf_timestamp": csrf_ts2,
        "camefrom": FILES_URL,
        "email": email,
        "password": password,
        "remember": "true" if remember else "false",
        "submit": "Sign In",
    }, headers={"Referer": r2.url}, timeout=15)

    if "/login" in r3.url or not is_authenticated(session):
        raise LoginError("Login submitted but the session isn't authenticated afterward - check the credentials")


def save_session(session: requests.Session, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
    tmp.replace(path)


def load_session(session: requests.Session, path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for c in data.get("cookies", []):
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return True


def ensure_authenticated(session: requests.Session, email: str, password: str, session_cache: Path) -> bool:
    """Load a cached session if present and still valid; otherwise log in
    fresh and cache the new session. Returns True if a fresh login happened
    (useful for logging - a scheduled run re-authenticating every time would
    be worth knowing about, since it means the cached session isn't sticking).
    """
    load_session(session, session_cache)
    if is_authenticated(session):
        return False
    login(session, email, password)
    save_session(session, session_cache)
    return True
