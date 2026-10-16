"""macOS automation for driving Shapr3D's UI.

Shapr3D draws most of its UI itself (dialogs, toolbars), so the
Accessibility tree only exposes the menu bar. Everything else is found by
OCR (Vision framework) on a capture of the Shapr3D window and clicked with
synthetic mouse events. All helpers shell out to `osascript -l JavaScript`
(JXA) so no extra Python dependencies are needed.

Requires, for the process hosting the MCP server:
  - Screen Recording  (window capture)
  - Accessibility     (menus, keystrokes, mouse events)
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

APP_NAME = "Shapr3D"

_WINDOW_JS = """
ObjC.import('CoreGraphics');
function run(argv) {
  const a = ObjC.castRefToObject(
    $.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0));
  let best = null;
  for (let i = 0; i < a.count; i++) {
    const d = a.objectAtIndex(i);
    if (ObjC.unwrap(d.objectForKey('kCGWindowOwnerName')) !== argv[0]) continue;
    if (ObjC.unwrap(d.objectForKey('kCGWindowLayer')) !== 0) continue;
    const b = ObjC.deepUnwrap(d.objectForKey('kCGWindowBounds'));
    if (!best || b.Width * b.Height > best.w * best.h)
      best = {id: ObjC.unwrap(d.objectForKey('kCGWindowNumber')),
              x: b.X, y: b.Y, w: b.Width, h: b.Height};
  }
  return JSON.stringify(best);
}
"""

_OCR_JS = """
ObjC.import('Foundation'); ObjC.import('Vision');
function run(argv) {
  const req = $.VNRecognizeTextRequest.alloc.init;
  req.recognitionLevel = 0;            // accurate
  req.usesLanguageCorrection = false;  // UI labels, not prose
  const h = $.VNImageRequestHandler.alloc.initWithURLOptions(
    $.NSURL.fileURLWithPath(argv[0]), $({}));
  if (!h.performRequestsError($([req]), Ref())) throw new Error('OCR failed');
  const res = req.results, out = [];
  for (let i = 0; i < res.count; i++) {
    const o = res.objectAtIndex(i), b = o.boundingBox;
    out.push({text: ObjC.unwrap(o.topCandidates(1).objectAtIndex(0).string),
              x: b.origin.x, y: b.origin.y, w: b.size.width, h: b.size.height});
  }
  return JSON.stringify(out);
}
"""

_CLICK_JS = """
ObjC.import('CoreGraphics');
function run(argv) {
  const p = $.CGPointMake(Number(argv[0]), Number(argv[1]));
  for (const t of [$.kCGEventMouseMoved, $.kCGEventLeftMouseDown,
                   $.kCGEventLeftMouseUp]) {
    $.CGEventPost($.kCGHIDEventTap,
                  $.CGEventCreateMouseEvent($(), t, p, $.kCGMouseButtonLeft));
    delay(0.06);
  }
  return 'ok';
}
"""

_MENU_JS = """
function run(argv) {
  const path = JSON.parse(argv[1]);
  const proc = Application('System Events').processes.byName(argv[0]);
  let item = proc.menuBars[0].menuBarItems.byName(path[0]);
  for (let i = 1; i < path.length; i++)
    item = item.menus[0].menuItems.byName(path[i]);
  item.click();
  return 'ok';
}
"""

_MENU_LIST_JS = """
function run(argv) {
  const path = JSON.parse(argv[1]);
  const proc = Application('System Events').processes.byName(argv[0]);
  if (!path.length) return JSON.stringify(proc.menuBars[0].menuBarItems.name());
  let item = proc.menuBars[0].menuBarItems.byName(path[0]);
  for (let i = 1; i < path.length; i++)
    item = item.menus[0].menuItems.byName(path[i]);
  return JSON.stringify(item.menus[0].menuItems.name().filter(n => n));
}
"""

_KEYS_JS = """
function run(argv) {
  const se = Application('System Events');
  const k = JSON.parse(argv[0]);
  const opts = k.modifiers.length ? {using: k.modifiers} : {};
  if (k.code !== null) se.keyCode(k.code, opts); else se.keystroke(k.key, opts);
  return 'ok';
}
"""

# System Events key codes for keys `keystroke` cannot type.
KEY_CODES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "forwarddelete": 117, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}
MODIFIERS = {
    "cmd": "command down", "command": "command down",
    "shift": "shift down", "alt": "option down", "option": "option down",
    "opt": "option down", "ctrl": "control down", "control": "control down",
}


def _jxa(script: str, *args: str, timeout: float = 30) -> str:
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip()
        if "-1719" in msg or "assistive access" in msg:
            msg += (" - grant Accessibility to the app hosting this MCP "
                    "server (System Settings > Privacy & Security).")
        raise RuntimeError(msg)
    return proc.stdout.strip()


def is_running() -> bool:
    return subprocess.run(["pgrep", "-x", APP_NAME], capture_output=True).returncode == 0


def activate() -> None:
    subprocess.run(["open", "-a", APP_NAME], check=True, timeout=15)
    time.sleep(0.4)


def window() -> dict:
    """Main Shapr3D window: CG window id and bounds in screen points."""
    info = json.loads(_jxa(_WINDOW_JS, APP_NAME) or "null")
    if not info:
        raise RuntimeError("No on-screen Shapr3D window found.")
    return info


def capture(out: Path, win: dict | None = None) -> dict:
    """Capture the Shapr3D window by id (works even when other windows
    cover it) and scale it to window points, so image pixels == points."""
    win = win or window()
    subprocess.run(
        ["screencapture", "-x", "-o", "-l", str(win["id"]), str(out)],
        check=True, timeout=15,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            "Window capture failed - grant Screen Recording to the app "
            "hosting this MCP server."
        )
    subprocess.run(
        ["sips", "-z", str(int(win["h"])), str(int(win["w"])), str(out)],
        check=True, capture_output=True, timeout=15,
    )
    return win


def ocr(png: Path) -> list[dict]:
    return json.loads(_jxa(_OCR_JS, str(png), timeout=60))


def to_points(item: dict, win: dict) -> dict:
    """Vision boxes are normalized with a bottom-left origin; convert the
    box centre to window points (top-left origin) and screen points."""
    cx = item["x"] + item["w"] / 2
    cy = 1 - (item["y"] + item["h"] / 2)
    wx, wy = round(cx * win["w"]), round(cy * win["h"])
    return {
        "text": item["text"],
        "x": wx, "y": wy,                       # window points
        "screen_x": round(win["x"] + wx), "screen_y": round(win["y"] + wy),
    }


def match_text(query: str, items: list[dict], occurrence: int = 1) -> dict | None:
    """Exact (case-insensitive) matches win over substring matches; ties are
    ordered top-to-bottom, left-to-right and `occurrence` picks among them."""
    q = query.strip().lower()
    exact = [i for i in items if i["text"].strip().lower() == q]
    partial = [i for i in items if q in i["text"].lower() and i not in exact]
    for group in (exact, partial):
        if group:
            group = sorted(group, key=lambda i: (i["y"], i["x"]))
            return group[occurrence - 1] if len(group) >= occurrence else None
    return None


def read_ui(out: Path) -> tuple[dict, list[dict]]:
    win = capture(out)
    return win, [to_points(i, win) for i in ocr(out)]


def click_screen(x: float, y: float) -> None:
    _jxa(_CLICK_JS, str(x), str(y))


def menu(path: list[str]) -> None:
    _jxa(_MENU_JS, APP_NAME, json.dumps(path))


def menu_items(path: list[str]) -> list[str]:
    return json.loads(_jxa(_MENU_LIST_JS, APP_NAME, json.dumps(path)))


def parse_keys(combo: str) -> dict:
    """'cmd+shift+z' -> modifiers + key; named keys use key codes."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty key combo.")
    *mods, key = parts
    unknown = [m for m in mods if m.lower() not in MODIFIERS]
    if unknown:
        raise ValueError(f"Unknown modifier(s) {unknown}; use {sorted(MODIFIERS)}.")
    code = KEY_CODES.get(key.lower())
    if code is None and len(key) != 1:
        raise ValueError(f"Unknown key '{key}'; use a single character or {sorted(KEY_CODES)}.")
    return {
        "modifiers": [MODIFIERS[m.lower()] for m in mods],
        "key": key if code is None else None,
        "code": code,
    }


def keys(combo: str) -> None:
    _jxa(_KEYS_JS, json.dumps(parse_keys(combo)))


# ---------------------------------------------------------------------------
# focus handling, typing, save panel, permissions
# ---------------------------------------------------------------------------

_FRONT_JS = """
function run() {
  return Application('System Events').processes.whose({frontmost: true})[0].name();
}
"""

_ACTIVATE_JS = """
function run(argv) { Application(argv[0]).activate(); return 'ok'; }
"""

_TYPE_JS = """
function run(argv) { Application('System Events').keystroke(argv[0]); return 'ok'; }
"""

_WINDOWS_JS = """
function run(argv) {
  const p = Application('System Events').processes.byName(argv[0]);
  return JSON.stringify(p.windows().map(w => {
    let sr = ''; try { sr = w.subrole(); } catch (e) {}
    return {name: w.name(), subrole: sr};
  }));
}
"""

_SAVE_JS = """
function run(argv) {
  const [name, folder] = argv;
  const se = Application('System Events');
  se.keystroke('a', {using: 'command down'}); delay(0.2);
  se.keystroke(name); delay(0.3);
  se.keystroke('g', {using: ['command down', 'shift down']}); delay(0.8);
  se.keystroke(folder); delay(0.5);
  se.keyCode(36); delay(1.0);   // return: go to folder
  se.keyCode(36); delay(1.2);   // return: Save
  return 'ok';
}
"""

_PERMS_JS = """
ObjC.import('ApplicationServices'); ObjC.import('CoreGraphics');
// not in JXA's bridge metadata: bind by signature
ObjC.bindFunction('CGPreflightScreenCaptureAccess', ['bool', []]);
ObjC.bindFunction('CGRequestScreenCaptureAccess', ['bool', []]);
function run(argv) {
  const prompt = argv[0] === '1';
  const opts = $.NSDictionary.dictionaryWithObjectForKey(
    prompt ? $.kCFBooleanTrue : $.kCFBooleanFalse, $('AXTrustedCheckOptionPrompt'));
  const ax = $.AXIsProcessTrustedWithOptions(opts);
  let screen = $.CGPreflightScreenCaptureAccess();
  if (!screen && prompt) screen = $.CGRequestScreenCaptureAccess();
  let events = false, err = '';
  try {   // first Apple Event to System Events triggers the Automation prompt
    Application('System Events').processes.length; events = true;
  } catch (e) { err = String(e); }
  return JSON.stringify({accessibility: !!ax, screen_recording: !!screen,
                         automation_system_events: events, error: err});
}
"""


def frontmost_app() -> str:
    return _jxa(_FRONT_JS)


def activate_app(name: str) -> None:
    _jxa(_ACTIVATE_JS, name)


def type_text(text: str) -> None:
    _jxa(_TYPE_JS, text)


def windows() -> list[dict]:
    return json.loads(_jxa(_WINDOWS_JS, APP_NAME) or "[]")


def wait_for_save_dialog(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(w["name"] == "Save" or w["subrole"] == "AXDialog" for w in windows()):
            return True
        time.sleep(0.5)
    return False


def fill_save_dialog(name: str, folder: str) -> None:
    """Shapr3D's Save panel runs out of process (sandbox), so Accessibility
    can't reach its fields; it is driven by keyboard: name, Cmd+Shift+G to
    the folder, Save. Shapr3D must be frontmost while typing."""
    _jxa(_SAVE_JS, name, folder, timeout=60)


def permissions(prompt: bool) -> dict:
    return json.loads(_jxa(_PERMS_JS, "1" if prompt else "0", timeout=60))
