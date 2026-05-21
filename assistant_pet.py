import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tkinter as tk
import winsound


ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "pet-token.txt"
STATE_FILE = ROOT / "pet-settings.json"
PORT = int(os.environ.get("AI_FINISH_PET_PORT", "8765"))
HOST = "127.0.0.1"
CODEX_LOG_PATTERN = re.compile(r"(?:notificationId=turn-|turnId=)([0-9a-f-]{20,})", re.IGNORECASE)
CODEX_LOG_TIME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


DEFAULT_SETTINGS = {
    "sound": True,
    "red_flash": True,
    "laser": True,
    "codex_watch": True,
    "intensity": "medium",
    "cooldown_seconds": 5,
}


def clean_settings(data):
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return settings

    for key in ("sound", "red_flash", "laser", "codex_watch"):
        if key in data:
            settings[key] = bool(data[key])

    if data.get("intensity") in ("low", "medium", "high"):
        settings["intensity"] = data["intensity"]

    if "cooldown_seconds" in data:
        try:
            settings["cooldown_seconds"] = max(0, min(3600, int(data["cooldown_seconds"])))
        except (TypeError, ValueError):
            pass

    return settings


def codex_log_root_candidates():
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    candidates = [
        local / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0" / "LocalCache" / "Local" / "Codex" / "Logs",
        local / "Codex" / "Logs",
    ]
    packages = local / "Packages"
    if packages.exists():
        candidates.extend(path / "LocalCache" / "Local" / "Codex" / "Logs" for path in packages.glob("OpenAI.Codex_*"))
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            yield path


def recent_codex_logs(limit=12):
    files = []
    for root in codex_log_root_candidates():
        try:
            files.extend(root.rglob("codex-desktop-*.log"))
        except OSError:
            continue
    files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return files[:limit]


def codex_line_time(line):
    line = line.lstrip("\ufeff")
    match = CODEX_LOG_TIME_PATTERN.match(line)
    if not match:
        return None
    stamp = match.group(1).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def codex_start_line(line):
    return "method=turn/start" in line and "conversationId=null" not in line


def codex_complete_line(line):
    if "turn-complete" in line:
        return "[desktop-notifications]" in line or "kind=turn-complete" in line
    if "method=turn/completed" in line or "Received turn/completed" in line:
        return True
    return "IAB_LIFECYCLE ended browser use turn route" in line


def codex_turn_id(line):
    match = CODEX_LOG_PATTERN.search(line)
    return match.group(1) if match else str(hash(line))


def read_log_tail(path, max_bytes=1024 * 1024):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="ignore").splitlines()


def codex_tail_active_state(paths, seen_turns):
    last_start = None
    last_complete = None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    for path in reversed(paths):
        for line in read_log_tail(path):
            when = codex_line_time(line)
            if when and when < cutoff:
                continue
            if codex_start_line(line):
                last_start = when or datetime.now(timezone.utc)
                continue
            if codex_complete_line(line):
                last_complete = when or datetime.now(timezone.utc)
                seen_turns.add(codex_turn_id(line))

    return last_start is not None and (last_complete is None or last_start > last_complete)


def codex_watcher():
    offsets = {}
    seen_turns = set()
    synced = False
    active = False

    while True:
        if not APP.settings.get("codex_watch", True):
            synced = False
            active = False
            time.sleep(0.5)
            continue

        logs = recent_codex_logs()
        if not synced:
            active = codex_tail_active_state(logs, seen_turns)
            if active:
                APP.events.put({"type": "thinking", "source": "Codex", "message": "Codex is already working"})
            synced = True

        for path in logs:
            try:
                size = path.stat().st_size
            except OSError:
                continue

            if path not in offsets:
                offsets[path] = size
                continue
            if size < offsets[path]:
                offsets[path] = 0
            if size == offsets[path]:
                continue

            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(offsets[path])
                    lines = handle.readlines()
                    offsets[path] = handle.tell()
            except OSError:
                continue

            for line in lines:
                if codex_start_line(line):
                    if not active:
                        APP.events.put({"type": "thinking", "source": "Codex", "message": "Codex is working"})
                    active = True
                    continue

                if not codex_complete_line(line):
                    continue

                turn_id = codex_turn_id(line)
                if turn_id in seen_turns:
                    continue
                seen_turns.add(turn_id)
                if len(seen_turns) > 128:
                    seen_turns = set(list(seen_turns)[-64:])
                active = False
                APP.events.put({"type": "success", "source": "Codex", "message": "Codex answer finished"})
        time.sleep(0.5)


class PetState:
    def __init__(self):
        self.settings = self.load_settings()
        self.events = queue.Queue()
        self.token = self.load_token()
        self.last_success_at = 0
        self.httpd = None

    def load_token(self):
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        return token

    def load_settings(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return clean_settings(data)
            except Exception:
                pass
        return dict(DEFAULT_SETTINGS)

    def save_settings(self):
        STATE_FILE.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")


APP = PetState()


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pet-Token")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def parse_body(handler, limit=8192):
    raw_len = handler.headers.get("Content-Length", "0")
    try:
        length = min(int(raw_len), limit)
    except ValueError:
        length = 0
    body = handler.rfile.read(length) if length else b""
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AIFinishPet/0.2"

    def log_message(self, *_):
        return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pet-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/status":
            return json_response(self, 200, {
                "ok": True,
                "port": PORT,
                "tokenRequired": True,
                "settings": APP.settings,
            })
        if url.path == "/token":
            return json_response(self, 200, {
                "ok": True,
                "token": APP.token,
                "bookmarklet": bookmarklet(APP.token),
                "consoleScript": console_script(APP.token),
            })
        return json_response(self, 404, {"ok": False, "message": "Not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path not in ("/notify", "/test", "/settings"):
            return json_response(self, 404, {"ok": False, "message": "Not found"})

        if url.path in ("/notify", "/settings"):
            token = self.headers.get("X-Pet-Token", "")
            if token != APP.token:
                return json_response(self, 401, {"ok": False, "message": "Bad token"})

        try:
            data = parse_body(self)
        except Exception:
            return json_response(self, 400, {"ok": False, "message": "Bad JSON"})

        if url.path == "/settings":
            updates = data if isinstance(data, dict) else {}
            APP.settings = clean_settings({**APP.settings, **updates})
            APP.save_settings()
            APP.events.put({"type": "settings"})
            return json_response(self, 200, {"ok": True, "settings": APP.settings})

        event_type = data.get("type") if isinstance(data, dict) else None
        if url.path == "/test":
            event_type = "success"
        if event_type not in ("thinking", "success", "idle"):
            event_type = "success"
        APP.events.put({
            "type": event_type,
            "source": data.get("source", "web") if isinstance(data, dict) else "web",
            "message": data.get("message", "") if isinstance(data, dict) else "",
            "at": time.time(),
        })
        return json_response(self, 200, {"ok": True})


def start_server():
    APP.httpd = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    APP.httpd.serve_forever()


def console_script(token):
    return f"""(() => {{
  const TOKEN = {json.dumps(token)};
  const URL = 'http://127.0.0.1:{PORT}/notify';
  let busy = false;
  let lastText = '';
  let stable = 0;
  function text() {{
    const nodes = [...document.querySelectorAll('[data-message-author-role="assistant"], [data-testid*="assistant"], .markdown, article')];
    return (nodes.at(-1)?.innerText || document.body.innerText || '').slice(-5000);
  }}
  function hasStop() {{
    return /stop generating|stop responding|停止生成|停止回答|stop/i.test(document.body.innerText || '');
  }}
  function inputReady() {{
    const el = document.querySelector('textarea, [contenteditable="true"]');
    return !!el && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  }}
  async function send(type, message) {{
    await fetch(URL, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Pet-Token': TOKEN }},
      body: JSON.stringify({{ type, source: location.hostname, message }})
    }}).catch(() => {{}});
  }}
  setInterval(() => {{
    const t = text();
    const stop = hasStop();
    const ready = inputReady();
    if ((stop || !ready) && !busy) {{ busy = true; stable = 0; send('thinking', 'AI is thinking'); }}
    if (busy) {{
      stable = t === lastText ? stable + 1 : 0;
      if (!stop && ready && stable >= 2 && t.length > 20) {{
        busy = false;
        send('success', 'AI answer finished');
      }}
    }}
    lastText = t;
  }}, 1200);
  alert('AI Finish Pet listener is running on this tab.');
}})();"""


def bookmarklet(token):
    script = console_script(token).replace("\n", " ")
    return "javascript:" + urllib.parse.quote(script, safe="()")


class PetApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AI Finish Pet")
        self.transparent = "#010203"
        self.width = 176
        self.height = 132
        self.scale = 4
        self.root.geometry(f"{self.width}x{self.height}+80+120")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.transparent)
        try:
            self.root.attributes("-transparentcolor", self.transparent)
        except tk.TclError:
            self.root.attributes("-alpha", 0.96)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.status = tk.StringVar(value="待机")
        self.detail = tk.StringVar(value="等待 AI 回答完成。")
        self.intensity = tk.StringVar(value=APP.settings["intensity"])
        self.sound = tk.BooleanVar(value=APP.settings["sound"])
        self.red_flash = tk.BooleanVar(value=APP.settings["red_flash"])
        self.laser = tk.BooleanVar(value=APP.settings["laser"])
        self.codex_watch = tk.BooleanVar(value=APP.settings["codex_watch"])

        self.drag = {"mouse_x": 0, "mouse_y": 0, "win_x": 0, "win_y": 0}
        self.activity = "idle"
        self.mode = "idle"
        self.dragging = False
        self.facing = 1
        self.frame = 0
        self.toast = None
        self.flash = None
        self.laser_overlay = None
        self.laser_canvas = None
        self.laser_job = None
        self.laser_step = 0
        self.reset_job = None
        self.animate_job = None
        self.drain_job = None
        self.toast_job = None
        self.flash_job = None
        self.closing = False

        self.build_ui()
        self.animate_job = self.root.after(160, self.animate)
        self.drain_job = self.root.after(120, self.drain_events)

    def build_ui(self):
        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=self.transparent,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.drag_stop)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.root.bind("<Button-3>", self.show_menu)
        self.root.bind("<Escape>", lambda _e: self.clear_flash())
        self.root.bind("<Control-q>", lambda _e: self.quit())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="测试提醒", command=self.trigger_success)
        self.menu.add_command(label="复制监听脚本", command=self.copy_script)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="声音", variable=self.sound, command=self.save_ui_settings)
        self.menu.add_checkbutton(label="红屏", variable=self.red_flash, command=self.save_ui_settings)
        self.menu.add_checkbutton(label="眼射激光", variable=self.laser, command=self.save_ui_settings)
        self.menu.add_checkbutton(label="Codex 完成提醒", variable=self.codex_watch, command=self.save_ui_settings)
        intensity_menu = tk.Menu(self.menu, tearoff=0)
        for value, label in (("low", "低强度"), ("medium", "中强度"), ("high", "高强度")):
            intensity_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.intensity,
                command=self.save_ui_settings,
            )
        self.menu.add_cascade(label="提醒强度", menu=intensity_menu)
        self.menu.add_separator()
        self.menu.add_command(label=f"端口 {HOST}:{PORT}", command=self.copy_status)
        self.menu.add_command(label="退出", command=self.quit)
        self.draw_pet("idle")

    def px(self, x, y, w, h, color, dx=0, dy=0):
        s = self.scale
        self.canvas.create_rectangle(
            (x + dx) * s,
            (y + dy) * s,
            (x + dx + w) * s - 1,
            (y + dy + h) * s - 1,
            fill=color,
            outline="",
        )

    def pxf(self, x, y, w, h, color, dx=0, dy=0, sprite_w=44):
        if self.facing < 0:
            x = sprite_w - x - w
        self.px(x, y, w, h, color, dx, dy)

    def draw_pet(self, state):
        self.canvas.delete("all")
        if state == "flying":
            self.draw_flying_pet()
        else:
            self.draw_standing_pet(state)

    def draw_standing_pet(self, state):
        outline = "#22243a"
        hair = "#ffd84d"
        hair_shadow = "#d69b21"
        skin = "#ffd0a6"
        suit = "#2556a3"
        suit_dark = "#17366f"
        white = "#fff7df"
        gold = "#f6c542"
        gold_dark = "#b97816"
        cape = "#b91f2b"
        cape_dark = "#79131f"
        cape_blue = "#17366f"
        blush = "#ff7a7a"
        eye = "#ff304a" if state in ("thinking", "success") else "#1b1e35"
        bob = 1 if self.frame % 8 in (3, 4, 5) else 0
        dx = 5
        dy = 4 + bob

        self.px(8, 11, 24, 19, outline, dx, dy)
        self.px(9, 12, 22, 18, cape, dx, dy)
        self.px(11, 12, 18, 3, cape_blue, dx, dy)
        self.px(12, 15, 2, 13, white, dx, dy)
        self.px(26, 15, 2, 13, white, dx, dy)
        self.px(9, 25, 22, 5, cape_dark, dx, dy)
        self.px(6, 18, 5, 11, cape_dark, dx, dy)
        self.px(29, 18, 5, 11, cape_dark, dx, dy)

        self.px(7, 14, 9, 6, outline, dx, dy)
        self.px(24, 14, 9, 6, outline, dx, dy)
        self.px(8, 15, 7, 3, gold, dx, dy)
        self.px(25, 15, 7, 3, gold, dx, dy)
        self.px(7, 18, 4, 2, gold_dark, dx, dy)
        self.px(29, 18, 4, 2, gold_dark, dx, dy)
        self.px(6, 17, 3, 2, gold, dx, dy)
        self.px(31, 17, 3, 2, gold, dx, dy)

        self.px(13, 14, 14, 15, outline, dx, dy)
        self.px(14, 15, 12, 13, suit, dx, dy)
        self.px(15, 15, 10, 3, white, dx, dy)
        self.px(14, 18, 12, 2, gold, dx, dy)
        self.px(16, 20, 8, 2, gold_dark, dx, dy)
        self.px(17, 18, 6, 9, suit_dark, dx, dy)
        self.px(19, 20, 2, 6, "#e7eefb", dx, dy)
        self.px(18, 17, 4, 3, gold, dx, dy)
        self.px(15, 19, 3, 2, gold, dx, dy)
        self.px(22, 19, 3, 2, gold, dx, dy)
        self.px(14, 25, 12, 2, gold, dx, dy)

        self.px(9, 20, 3, 6, outline, dx, dy)
        self.px(28, 20, 3, 6, outline, dx, dy)
        self.px(9, 25, 3, 2, cape, dx, dy)
        self.px(28, 25, 3, 2, cape, dx, dy)
        self.px(15, 28, 4, 3, outline, dx, dy)
        self.px(21, 28, 4, 3, outline, dx, dy)
        self.px(14, 30, 6, 2, cape, dx, dy)
        self.px(20, 30, 6, 2, cape, dx, dy)

        self.px(10, 6, 18, 11, outline, dx, dy)
        self.px(12, 8, 14, 8, skin, dx, dy)
        self.px(10, 2, 18, 7, outline, dx, dy)
        self.px(11, 3, 16, 5, hair, dx, dy)
        self.px(14, 1, 7, 3, hair, dx, dy)
        self.px(20, 2, 6, 3, hair, dx, dy)
        self.px(17, 5, 4, 2, hair_shadow, dx, dy)
        self.px(11, 8, 3, 5, hair, dx, dy)
        self.px(25, 8, 3, 5, hair, dx, dy)
        self.px(15, 10, 2, 2, eye, dx, dy)
        self.px(22, 10, 2, 2, eye, dx, dy)
        self.px(14, 9, 4, 1, hair_shadow, dx, dy)
        self.px(21, 9, 4, 1, hair_shadow, dx, dy)
        self.px(15, 13, 2, 1, blush, dx, dy)
        self.px(23, 13, 2, 1, blush, dx, dy)
        self.px(18, 14, 4, 1, "#7a342a", dx, dy)

        if state == "thinking":
            dot_y = 1 + (self.frame // 3) % 2
            self.px(19, dot_y, 1, 1, "#f8fafc", dx, dy)
            self.px(22, dot_y - 1, 1, 1, "#f8fafc", dx, dy)
            self.px(25, dot_y - 2, 1, 1, "#f8fafc", dx, dy)

        if state == "success" and self.laser.get():
            self.draw_lasers(dx, dy)

    def draw_flying_pet(self):
        outline = "#22243a"
        hair = "#ffd84d"
        hair_shadow = "#d69b21"
        skin = "#ffd0a6"
        suit = "#2556a3"
        suit_dark = "#17366f"
        white = "#fff7df"
        gold = "#f6c542"
        gold_dark = "#b97816"
        cape = "#b91f2b"
        cape_dark = "#79131f"
        cape_blue = "#17366f"
        flap = 1 if self.frame % 4 in (1, 2) else -1
        dx = 1
        dy = 8 + flap

        self.pxf(2, 13 - flap, 18, 12, outline, dx, dy)
        self.pxf(3, 14 - flap, 16, 10, cape, dx, dy)
        self.pxf(4, 14 - flap, 13, 2, cape_blue, dx, dy)
        self.pxf(5, 16 - flap, 2, 8, white, dx, dy)
        self.pxf(16, 16 - flap, 2, 8, white, dx, dy)
        self.pxf(2, 23 - flap, 13, 2, cape_dark, dx, dy)

        self.pxf(15, 12, 15, 10, outline, dx, dy)
        self.pxf(16, 13, 13, 8, suit, dx, dy)
        self.pxf(18, 13, 8, 2, white, dx, dy)
        self.pxf(17, 15, 11, 2, gold, dx, dy)
        self.pxf(19, 16, 7, 2, gold_dark, dx, dy)
        self.pxf(17, 18, 10, 3, suit_dark, dx, dy)
        self.pxf(19, 19, 2, 2, "#e7eefb", dx, dy)
        self.pxf(12, 15, 6, 4, outline, dx, dy)
        self.pxf(13, 15, 5, 3, gold, dx, dy)
        self.pxf(28, 15, 5, 4, outline, dx, dy)
        self.pxf(28, 15, 4, 3, gold, dx, dy)
        self.pxf(9, 17, 6, 3, outline, dx, dy)
        self.pxf(27, 21, 8, 3, outline, dx, dy)
        self.pxf(31, 10, 10, 11, outline, dx, dy)
        self.pxf(32, 12, 8, 8, skin, dx, dy)
        self.pxf(30, 8, 11, 5, outline, dx, dy)
        self.pxf(31, 8, 9, 4, hair, dx, dy)
        self.pxf(32, 7, 4, 2, hair, dx, dy)
        self.pxf(35, 9, 4, 2, hair_shadow, dx, dy)
        self.pxf(39, 12, 2, 3, hair, dx, dy)
        eye = "#ff304a" if self.activity in ("thinking", "success") else "#1b1e35"
        self.pxf(34, 15, 1, 2, eye, dx, dy)
        self.pxf(37, 15, 1, 2, eye, dx, dy)
        self.pxf(35, 18, 3, 1, "#7a342a", dx, dy)

        for x in (1, 3, 5):
            self.pxf(x, 18 + ((self.frame + x) % 2), 1, 1, "#fef3c7", dx, dy)

    def draw_lasers(self, dx, dy):
        red = "#ef233c"
        hot = "#ffd6d6"
        self.px(15, 10, 2, 2, hot, dx, dy)
        self.px(22, 10, 2, 2, hot, dx, dy)
        for step in range(12):
            self.px(16 + step, 10 - step // 3, 2, 1, red, dx, dy)
            self.px(23 + step, 10 - step // 3, 2, 1, red, dx, dy)
        for step in range(10):
            self.px(17 + step, 11 - step // 3, 1, 1, hot, dx, dy)
            self.px(24 + step, 11 - step // 3, 1, 1, hot, dx, dy)

    def eye_screen_points(self):
        s = self.scale
        if self.mode == "flying":
            flap = 1 if self.frame % 4 in (1, 2) else -1
            dx = 1
            dy = 8 + flap
            sprite_w = 44
            eyes = [(34.5, 16), (37.5, 16)]
            if self.facing < 0:
                eyes = [(sprite_w - x, y) for x, y in eyes]
        else:
            bob = 1 if self.frame % 8 in (3, 4, 5) else 0
            dx = 5
            dy = 4 + bob
            eyes = [(16, 11), (23, 11)]

        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        return [(root_x + int((dx + x) * s), root_y + int((dy + y) * s)) for x, y in eyes]

    def start_laser_sweep(self):
        self.clear_laser()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.laser_step = 0
        self.laser_overlay = tk.Toplevel(self.root)
        self.laser_overlay.overrideredirect(True)
        self.laser_overlay.attributes("-topmost", True)
        self.laser_overlay.configure(bg=self.transparent)
        try:
            self.laser_overlay.attributes("-transparentcolor", self.transparent)
        except tk.TclError:
            self.laser_overlay.attributes("-alpha", 0.72)
        self.laser_overlay.geometry(f"{screen_w}x{screen_h}+0+0")
        self.laser_canvas = tk.Canvas(
            self.laser_overlay,
            width=screen_w,
            height=screen_h,
            bg=self.transparent,
            bd=0,
            highlightthickness=0,
        )
        self.laser_canvas.pack(fill="both", expand=True)
        self.animate_laser_sweep()

    def animate_laser_sweep(self):
        if self.closing or self.laser_canvas is None:
            return
        self.laser_step += 1
        steps = 18 if self.intensity.get() == "high" else 14
        progress = min(1.0, self.laser_step / steps)
        canvas = self.laser_canvas
        canvas.delete("all")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        eyes = self.eye_screen_points()
        direction = self.facing if self.mode == "flying" else 1
        target_x = screen_w if direction >= 0 else 0
        center_y = int(screen_h * 0.5)
        spread = int(screen_h * (0.08 + 0.62 * progress))
        width = int(10 + 64 * progress)

        for eye_x, eye_y in eyes:
            canvas.create_polygon(
                eye_x,
                eye_y,
                target_x,
                max(0, center_y - spread),
                target_x,
                min(screen_h, center_y + spread),
                fill="#b91c1c",
                outline="",
                stipple="gray25",
            )
            canvas.create_line(eye_x, eye_y, target_x, center_y, fill="#ef233c", width=width)
            canvas.create_line(eye_x, eye_y, target_x, center_y, fill="#ffd6d6", width=max(2, width // 4))

        if progress > 0.45:
            stipple = "gray12" if progress < 0.7 else "gray25"
            canvas.create_rectangle(0, 0, screen_w, screen_h, fill="#ef1111", outline="", stipple=stipple)
        if progress > 0.82:
            canvas.create_rectangle(0, 0, screen_w, screen_h, fill="#ff2525", outline="", stipple="gray50")

        if self.laser_step < steps + 9:
            self.laser_job = self.root.after(55, self.animate_laser_sweep)
        else:
            self.clear_laser()

    def animate(self):
        if self.closing:
            return
        self.frame = (self.frame + 1) % 24
        self.draw_pet(self.mode)
        delay = 90 if self.mode == "flying" else 180
        self.animate_job = self.root.after(delay, self.animate)

    def drag_start(self, event):
        self.drag["mouse_x"] = event.x_root
        self.drag["mouse_y"] = event.y_root
        self.drag["win_x"] = self.root.winfo_x()
        self.drag["win_y"] = self.root.winfo_y()
        self.drag["last_x"] = event.x_root
        self.dragging = True
        self.mode = "flying"
        self.draw_pet(self.mode)

    def drag_move(self, event):
        delta = event.x_root - self.drag.get("last_x", event.x_root)
        if abs(delta) >= 2:
            self.facing = 1 if delta > 0 else -1
            self.drag["last_x"] = event.x_root
        x = self.drag["win_x"] + event.x_root - self.drag["mouse_x"]
        y = self.drag["win_y"] + event.y_root - self.drag["mouse_y"]
        self.root.geometry(f"+{x}+{y}")

    def drag_stop(self, _event):
        self.dragging = False
        self.mode = self.activity
        self.draw_pet(self.mode)

    def show_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)
        self.menu.grab_release()

    def save_ui_settings(self):
        APP.settings["sound"] = bool(self.sound.get())
        APP.settings["red_flash"] = bool(self.red_flash.get())
        APP.settings["laser"] = bool(self.laser.get())
        APP.settings["codex_watch"] = bool(self.codex_watch.get())
        APP.settings["intensity"] = self.intensity.get()
        APP.save_settings()
        self.show_toast("设置已保存")

    def sync_ui_settings(self):
        APP.settings = clean_settings(APP.settings)
        self.sound.set(APP.settings["sound"])
        self.red_flash.set(APP.settings["red_flash"])
        self.laser.set(APP.settings["laser"])
        self.codex_watch.set(APP.settings["codex_watch"])
        self.intensity.set(APP.settings["intensity"])

    def copy_script(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(console_script(APP.token))
        self.detail.set("监听脚本已复制。")
        self.show_toast("脚本已复制")

    def copy_status(self):
        text = f"AI Finish Pet: http://{HOST}:{PORT}"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.show_toast("端口已复制")

    def show_toast(self, text):
        self.cancel_job("toast_job")
        if self.toast is not None and self.toast.winfo_exists():
            self.toast.destroy()
        self.toast = tk.Toplevel(self.root)
        self.toast.overrideredirect(True)
        self.toast.attributes("-topmost", True)
        self.toast.configure(bg="#111827")
        label = tk.Label(
            self.toast,
            text=text,
            fg="#f8fafc",
            bg="#111827",
            padx=9,
            pady=5,
            font=("Microsoft YaHei UI", 9),
        )
        label.pack()
        x = self.root.winfo_x() + 34
        y = self.root.winfo_y() - 32
        self.toast.geometry(f"+{x}+{y}")
        self.toast_job = self.root.after(1200, self.hide_toast)

    def hide_toast(self):
        if self.toast is not None and self.toast.winfo_exists():
            self.toast.destroy()
        self.toast = None
        self.toast_job = None

    def trigger_success(self):
        APP.events.put({"type": "success", "source": "manual", "message": "test"})

    def drain_events(self):
        if self.closing:
            return
        while True:
            try:
                event = APP.events.get_nowait()
            except queue.Empty:
                break
            self.handle_event(event)
        self.drain_job = self.root.after(120, self.drain_events)

    def handle_event(self, event):
        t = event.get("type")
        if t == "settings":
            self.sync_ui_settings()
            return
        if t == "thinking":
            self.status.set("思考中")
            self.detail.set(f"{event.get('source', 'web')} 正在生成。")
            self.activity = "thinking"
            if not self.dragging:
                self.mode = "thinking"
            self.draw_pet(self.mode)
            return
        if t == "idle":
            self.back_to_idle()
            return
        self.success(event)

    def success(self, event):
        now = time.time()
        if now - APP.last_success_at < int(APP.settings.get("cooldown_seconds", 5)):
            return
        APP.last_success_at = now
        self.status.set("回答完成")
        self.detail.set(f"{event.get('source', 'AI')} 完成了。")
        self.activity = "success"
        if not self.dragging:
            self.mode = "success"
        self.draw_pet(self.mode)
        if self.sound.get():
            self.play_sound()
        if self.laser.get():
            self.start_laser_sweep()
        elif self.red_flash.get() and self.intensity.get() != "low":
            self.flash_screen()
        self.cancel_job("reset_job")
        reset_ms = {"low": 2600, "medium": 3800, "high": 4800}.get(self.intensity.get(), 3800)
        self.reset_job = self.root.after(reset_ms, self.back_to_idle)

    def back_to_idle(self):
        self.cancel_job("reset_job")
        self.status.set("待机")
        self.detail.set("等待 AI 回答完成。")
        self.activity = "idle"
        if not self.dragging:
            self.mode = "idle"
        self.draw_pet(self.mode)

    def play_sound(self):
        intensity = self.intensity.get()

        def worker():
            cycles = {"low": 3, "medium": 5, "high": 8}.get(intensity, 5)
            for idx in range(cycles):
                winsound.Beep(1280 if idx % 2 == 0 else 920, 170)
                winsound.Beep(640, 120)
                time.sleep(0.035)
            if intensity == "high":
                winsound.Beep(1560, 260)
                winsound.Beep(1760, 320)
            else:
                winsound.Beep(1420, 240)
        threading.Thread(target=worker, daemon=True).start()

    def flash_screen(self):
        self.clear_flash()
        alpha = 0.35 if self.intensity.get() == "medium" else 0.58
        self.flash = tk.Toplevel(self.root)
        self.flash.overrideredirect(True)
        self.flash.attributes("-topmost", True)
        self.flash.attributes("-alpha", alpha)
        self.flash.configure(bg="#ef1111")
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        self.flash.geometry(f"{width}x{height}+0+0")
        self.flash.bind("<Escape>", lambda _e: self.clear_flash())
        self.cancel_job("flash_job")
        self.flash_job = self.root.after(260 if self.intensity.get() == "medium" else 520, self.clear_flash)

    def clear_flash(self):
        self.cancel_job("flash_job")
        flash = getattr(self, "flash", None)
        if flash is not None and flash.winfo_exists():
            flash.destroy()
        self.flash = None

    def clear_laser(self):
        self.cancel_job("laser_job")
        overlay = getattr(self, "laser_overlay", None)
        if overlay is not None and overlay.winfo_exists():
            overlay.destroy()
        self.laser_overlay = None
        self.laser_canvas = None

    def cancel_job(self, attr):
        job = getattr(self, attr, None)
        if not job:
            return
        try:
            self.root.after_cancel(job)
        except tk.TclError:
            pass
        setattr(self, attr, None)

    def quit(self):
        if self.closing:
            return
        self.closing = True
        try:
            for attr in ("animate_job", "drain_job", "toast_job", "flash_job", "laser_job", "reset_job"):
                self.cancel_job(attr)
            self.clear_flash()
            self.clear_laser()
            self.hide_toast()
            if APP.httpd:
                threading.Thread(target=APP.httpd.shutdown, daemon=True).start()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    threading.Thread(target=codex_watcher, daemon=True).start()
    app = PetApp()
    app.detail.set(f"监听 {HOST}:{PORT}。右键桌宠可复制脚本和调整设置。")
    app.run()


if __name__ == "__main__":
    main()
