"""Outbound Telegram sender: renders GFM-ish markdown to Telegram HTML, falls back to plain
text if the HTML send fails, and chunks messages over the 4096-char limit. The rendering
handles the fiddly parts (code spans, fences straddling a chunk split) so it doesn't drift.

Deliberately hand-rolled on urllib (no `requests`) and fully synchronous, so it can be
called from `asyncio.to_thread` inside a turn without dragging in an async HTTP client. The
live progress bubble, which runs in the async context, uses PTB's bot API directly instead.

CLI: `python -m agent.messaging <chat_id>` with the message on stdin (token from
TELEGRAM_BOT_TOKEN). Importable: `from agent.messaging import send_message, send_file`.
"""
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# Last HTTP/network failure detail. _post never raises, so without this an HTML-parse 400
# (Telegram's "can't parse entities") is indistinguishable from a network drop when debugging.
last_error = ""


def _split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _table_sep(line: str) -> bool:
    """True if `line` is a GFM table separator row (e.g. `|---|:--:|--|`) — dashes/colons
    only, at least one dash. Distinguishes a real table header from a stray line with a `|`."""
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _vwidth(s: str) -> int:
    """Display width in a monospace terminal: CJK/fullwidth chars are 2 columns wide, so a
    plain len() would misalign any table mixing CJK and ASCII cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _render_table(rows: list) -> str:
    """Rows (header first, already split into cells) -> a monospace-aligned block. Telegram
    has no table element in any parse mode (HTML or MarkdownV2) — a <pre> block with padded
    columns is the closest a chat message gets to an actual table."""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(_vwidth(r[i]) for r in rows) for i in range(ncols)]

    def line(cells):
        return " | ".join(c + " " * (widths[i] - _vwidth(c)) for i, c in enumerate(cells))

    out = [line(rows[0]), "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in rows[1:]]
    return "\n".join(out)


def _stash_tables(text: str, code: list) -> str:
    """Find GFM tables (header row + |---|---| separator + body rows) and replace each with a
    placeholder pointing at a monospace-rendered <pre> block, appended to `code`. Must run
    after the ``` fence stash so a code block containing a literal `|` is never mistaken for
    a table."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _table_sep(lines[i + 1]):
            header = _split_row(lines[i])
            body, j = [], i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                body.append(_split_row(lines[j]))
                j += 1
            code.append(f"<pre>{_render_table([header] + body)}</pre>")
            out.append(f"\x00{len(code) - 1}\x00")
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def md_to_html(text: str) -> str:
    """Convert the markdown claude emits into Telegram-safe HTML. Code spans and tables are
    lifted out into placeholders before any prose regex runs: Telegram 400s on entities
    nested inside pre/code, and the bullet/header/italic rewrites must never touch that
    content (a table's `-` separator row would otherwise be mangled by the bullet regex)."""
    text = html.escape(text, quote=False)  # & < >
    text = text.replace("\x00", "")  # NUL never legit; keeps placeholders unforgeable
    code = []

    def stash(tag):
        def sub(m):
            code.append(f"<{tag}>{m.group(1)}</{tag}>")
            return f"\x00{len(code) - 1}\x00"
        return sub

    text = re.sub(r"```[^\n]*\n(.*?)```", stash("pre"), text, flags=re.DOTALL)
    text = _stash_tables(text, code)                                # GFM table -> <pre>
    text = re.sub(r"`([^`\n]+)`", stash("code"), text)              # inline code
    text = re.sub(r"\*\*([^\n*]+)\*\*", r"<b>\1</b>", text)         # **bold**
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*)$", r"<b>\1</b>", text)  # # headers -> bold
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)             # - / * bullets -> •
    text = re.sub(r"\*([^\n*]+)\*", r"<i>\1</i>", text)            # *italic*
    text = re.sub(r"(?<!\w)_([^\n_]+)_(?!\w)", r"<i>\1</i>", text)  # _italic_
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
    print(f"messaging: {last_error}", file=sys.stderr)


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


def send_message(token, chat_id, text) -> bool:
    """Send a text message (chunked + markdown-rendered, plain-text fallback per chunk)."""
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


def send_file(token, chat_id, path, caption="") -> bool:
    """Upload a local file to the chat via sendDocument (hand-rolled multipart — the venv
    has no requests). Caption is sent as plain text, truncated to Telegram's 1024 limit."""
    if not (token and chat_id):
        return False
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return False
    import uuid
    fname = re.sub(r'[\r\n"]', "_", os.path.basename(path)) or "file.bin"
    if caption:
        caption = caption.encode("utf-16-le")[:2048].decode("utf-16-le", "ignore")

    def _upload(cap):
        boundary = "tgagent" + uuid.uuid4().hex
        fields = {"chat_id": str(chat_id)}
        if cap:
            fields["caption"] = cap
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n').encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="document"; filename="{fname}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        body += blob + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return bool(json.loads(r.read()).get("ok"))
        except Exception as e:
            _note_error("sendDocument", e)
            return False

    if _upload(caption):
        return True
    return bool(caption) and _upload("")


if __name__ == "__main__":
    chat = sys.argv[1] if len(sys.argv) > 1 else ""
    ok = send_message(os.environ.get("TELEGRAM_BOT_TOKEN", ""), chat, sys.stdin.read())
    sys.exit(0 if ok else 1)
