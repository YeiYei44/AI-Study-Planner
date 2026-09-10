"""Turn whatever cookie material the user can get into Playwright cookies.

On a locked-down device the user often can't install a cookie-export
extension, but can usually still reach DevTools and copy the ``Cookie:``
request header (or "Copy as cURL"). So this module accepts, and
auto-detects:

* a JSON array of cookie objects (Cookie-Editor / EditThisCookie), or a
  Playwright ``storage_state`` object;
* a ``curl`` command pasted from DevTools;
* a raw ``Cookie:`` header string.

The last two carry no per-cookie domain, so those cookies are scoped to
the Canvas base URL (which is all this project talks to anyway).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SAMESITE = {
    "no_restriction": "None",
    "none": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}
_JSON_ALLOWED = {
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
}
_HEADER_ARG_RE = re.compile(
    r"(?:-H|--header|-b|--cookie)\s+(['\"])(.*?)\1", re.DOTALL
)


# -- JSON (extension export / storage_state) ----------------------------------


def _normalize_json_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    name, value, domain = raw.get("name"), raw.get("value"), raw.get("domain")
    if not name or value is None or not domain:
        return None

    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": raw.get("path") or "/",
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", False)),
    }

    ss = raw.get("sameSite")
    key = ss.lower().replace("-", "_") if isinstance(ss, str) else ""
    cookie["sameSite"] = _SAMESITE.get(key, "Lax")
    if cookie["sameSite"] == "None" and not cookie["secure"]:
        cookie["sameSite"] = "Lax"  # Chromium rejects that combination

    exp = raw.get("expires", raw.get("expirationDate"))
    if isinstance(exp, (int, float)) and exp > 0:
        cookie["expires"] = int(exp)

    return {k: v for k, v in cookie.items() if k in _JSON_ALLOWED}


def _from_json(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if not isinstance(data, list):
        raise ValueError(
            "Expected a JSON array of cookies or a storage_state object."
        )
    out: list[dict[str, Any]] = []
    for raw in data:
        if isinstance(raw, dict):
            norm = _normalize_json_cookie(raw)
            if norm:
                out.append(norm)
    return out


# -- Cookie header / curl ----------------------------------------------------


def _pairs(raw: str) -> list[tuple[str, str]]:
    raw = raw.strip()
    if raw[:7].lower() == "cookie:":
        raw = raw[7:].strip()
    pairs: list[tuple[str, str]] = []
    for piece in raw.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def _from_header(header: str, url: str) -> list[dict[str, Any]]:
    return [{"name": n, "value": v, "url": url} for n, v in _pairs(header)]


def _from_curl(text: str, url: str) -> list[dict[str, Any]]:
    for _quote, val in _HEADER_ARG_RE.findall(text):
        pairs = _pairs(val)
        if pairs:
            return [{"name": n, "value": v, "url": url} for n, v in pairs]
    return []


# -- public API ------------------------------------------------------------


def load_cookies(text: str, url: str) -> list[dict[str, Any]]:
    """Parse cookie material of any supported shape into Playwright cookies."""
    s = text.strip()
    if not s:
        return []
    if s[0] in "[{":
        return _from_json(s)
    if "curl " in s or _HEADER_ARG_RE.search(s):
        curl = _from_curl(s, url)
        if curl:
            return curl
    return _from_header(s, url)


def load_cookie_file(path: Path, url: str) -> list[dict[str, Any]]:
    return load_cookies(Path(path).read_text(), url)


def filter_domain(
    cookies: list[dict[str, Any]], needle: str
) -> list[dict[str, Any]]:
    return [
        c
        for c in cookies
        if needle in (c.get("domain") or "") or needle in (c.get("url") or "")
    ]
