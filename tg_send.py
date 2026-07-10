#!/usr/bin/env python3
"""Telegram sender: renders GFM-ish markdown to Telegram HTML, falls back to plain text if
the HTML send fails, and chunks messages over the 4096-char limit. Lifted from Noir's
tg_send.py (the parts a text bridge needs) so the rendering is already battle-tested.

CLI: `tg_send.py <chat_id>` with the message on stdin (token from TELEGRAM_BOT_TOKEN).
Importable: `from tg_send import send_telegram, md_to_html`.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Last HTTP/network failure detail. _post never raises, so without this an HTML-parse 400
# (Telegram's "can't parse entities") is indistinguishable from a network drop when debugging.
last_error = ""


def md_to_html(text: str) -> str:
    """Convert the markdown claude emits into Telegram-safe HTML. Code spans are lifted out
    into placeholders before any prose regex runs: Telegram 400s on entities nested inside
    pre/code, and the bullet/header rewrites must never touch code content."""
    text = html.escape(text, quote=False)  # & < >
    text = text.replace("\x00", "")  # NUL never legit; keeps placeholders unforgeable
    code = []

    def stash(tag):
        def sub(m):
            code.append(f"<{tag}>{m.group(1)}</{tag}>")
            return f"\x00{len(code) - 1}\x00"
        return sub

    text = re.sub(r"```[^\n]*\n(.*?)```", stash("pre"), text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", stash("code"), text)              # inline code
    text = re.sub(r"\*\*([^\n*]+)\*\*", r"<b>\1</b>", text)         # **bold**
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*)$", r"<b>\1</b>", text)  # # headers -> bold
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)             # - / * bullets -> •
    return re.sub(r"\x00(\d+)\x00", lambda m: code[int(m.group(1))], text)


def _note_error(method, exc):
    global last_error
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        last_error = f"{method} HTTP {exc.code}: {body}"
    else:
        last_error = f"{method}: {type(exc).__name__}: {exc}"
    print(f"tg_send: {last_error}", file=sys.stderr)


def _post(token, method, params):
    try:
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        _note_error(method, e)
        return None


def _chunks(text, size=3500):
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        if len(line) > size:
            for i in range(0, len(line), size):
                out.append(line[i:i + size])
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    out = out or [""]
    # A ``` block straddling a split is closed at the chunk end and reopened in the next, so
    # md_to_html sees both fences in one string and the plain-text fallback stays readable.
    open_fence = False
    for i, c in enumerate(out):
        prefix = "```\n" if open_fence else ""
        for ln in c.split("\n"):
            if ln.lstrip().startswith("```"):
                open_fence = not open_fence
        out[i] = prefix + c + ("\n```" if open_fence else "")
    return out


def send_telegram(token, chat_id, text) -> bool:
    if not (token and chat_id and text):
        return False
    ok_all = True
    for chunk in _chunks(text):
        params = {"chat_id": chat_id, "text": md_to_html(chunk),
                  "parse_mode": "HTML", "disable_web_page_preview": "true"}
        r = _post(token, "sendMessage", params)
        if not (r and r.get("ok")):
            r = _post(token, "sendMessage", {"chat_id": chat_id, "text": chunk})  # plain fallback
            ok_all = ok_all and bool(r and r.get("ok"))
    return ok_all


if __name__ == "__main__":
    chat = sys.argv[1] if len(sys.argv) > 1 else ""
    ok = send_telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), chat, sys.stdin.read())
    sys.exit(0 if ok else 1)
