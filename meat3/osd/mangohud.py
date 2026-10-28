"""MangoHud config generator for meat³'s in-game OSD.

How it works (verified against flightlessmango/MangoHud master, src/hud_elements.cpp,
src/shell.cpp, src/config.cpp, src/notify.cpp):

* MangoHud has no "read a text file" element. The supported way to show external text is
  `exec=<shell command>`, paired with a preceding `custom_text=<label>` (custom_text starts a
  new table row in column 0, exec stays on that row and right-aligns its output in the next
  column). Requires `legacy_layout=0`.
* All exec commands run in ONE persistent `/bin/sh` that MangoHud forks once per game
  (Shell class); each poll writes the command to that shell's stdin and reads back the LAST
  line of output after a fixed 50 ms sleep. Polls happen on MangoHud's hw-info thread every
  `fps_sampling_period` (default 500 ms), not on the render thread.
* So the cheapest possible exec is pure shell builtins (no fork/exec at all): we `read` the
  N-th line of /run/meat3/osd.txt with `IFS=: read` and print the value part.
  Cost: one open()+read() per line per poll, plus MangoHud's own 50 ms wait per exec item
  (sequential, on the hw thread) - keep the OSD to <= ~8 items so updates stay < 0.5 s.
* MangoHud's config parser drops everything after '#', so neither the command nor the labels
  may contain '#'. Values are taken from the first '='.
* MangoHud watches the config file with inotify (IN_MODIFY|IN_DELETE_SELF) and reloads it, so
  regenerating after changing the OSD item list takes effect in a running game.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

from meat3.core.fmt import osd_label

DEFAULT_OSD_FILE = "/run/meat3/osd.txt"

# evdev KEY_* names (what the daemon reads) -> X keysym names (what MangoHud's toggle_hud parses)
_EVDEV_KEYSYM = {
    "RIGHTCTRL": "Control_R", "LEFTCTRL": "Control_L", "RIGHTSHIFT": "Shift_R", "LEFTSHIFT": "Shift_L",
    "RIGHTALT": "Alt_R", "LEFTALT": "Alt_L", "LEFTMETA": "Super_L", "RIGHTMETA": "Super_R",
    "ESC": "Escape", "TAB": "Tab", "SPACE": "space", "ENTER": "Return", "BACKSPACE": "BackSpace",
    "INSERT": "Insert", "DELETE": "Delete", "HOME": "Home", "END": "End", "PAGEUP": "Prior",
    "PAGEDOWN": "Next", "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
    "PAUSE": "Pause", "SCROLLLOCK": "Scroll_Lock", "SYSRQ": "Print", "PRINT": "Print",
    "GRAVE": "grave", "MINUS": "minus", "EQUAL": "equal", "COMMA": "comma", "DOT": "period",
    "SLASH": "slash", "BACKSLASH": "backslash", "SEMICOLON": "semicolon", "APOSTROPHE": "apostrophe",
    "LEFTBRACE": "bracketleft", "RIGHTBRACE": "bracketright",
    "KP0": "KP_0", "KP1": "KP_1", "KP2": "KP_2", "KP3": "KP_3", "KP4": "KP_4", "KP5": "KP_5",
    "KP6": "KP_6", "KP7": "KP_7", "KP8": "KP_8", "KP9": "KP_9", "KPPLUS": "KP_Add",
    "KPMINUS": "KP_Subtract", "KPASTERISK": "KP_Multiply", "KPSLASH": "KP_Divide", "KPENTER": "KP_Enter",
}


def evdev_to_keysym(hotkey: str) -> Optional[str]:
    """'KEY_RIGHTCTRL+KEY_F12' -> 'Control_R+F12'. None if any key cannot be mapped."""
    out = []
    for part in (hotkey or "").split("+"):
        k = part.strip().upper()
        if k.startswith("KEY_"):
            k = k[4:]
        if not k:
            return None
        if k in _EVDEV_KEYSYM:
            out.append(_EVDEV_KEYSYM[k])
        elif k[0] == "F" and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
            out.append(k)
        elif len(k) == 1 and k.isalnum():
            out.append(k.lower())
        else:
            return None
    return "+".join(out) if out else None


def exec_command(line_no: int, osd_file: str = DEFAULT_OSD_FILE) -> str:
    """Builtin-only /bin/sh command printing the value part of line `line_no` (1-based).

    osd.txt lines are '<label>: <value unit>' and labels never contain ':' (see
    meat3.core.fmt.osd_label), so `IFS=: read -r a b` leaves ' <value unit>' in b (the leading
    space is invisible because exec output is right-aligned). No '#' anywhere (MangoHud's parser
    would cut the line), no fork/exec, works in dash and bash. The `[ -r ]` guard avoids a
    redirection error on a compound command, which POSIX allows to kill a non-interactive shell
    (MangoHud's single exec shell). Missing file / short file => empty value."""
    if any(ch in osd_file for ch in "#'\n"):
        raise ValueError("osd file path must not contain '#', quotes or newlines")
    reads = "; ".join(["IFS=: read -r a b"] * line_no)
    q = f"'{osd_file}'"
    return f"a=; b=; if [ -r {q} ]; then {{ {reads}; }} <{q}; fi; printf '%s\\n' \"$b\""


def _clean(text: str) -> str:
    return text.replace("#", "No.").replace("\n", " ").strip()


def generate(sensors: Iterable[dict], hotkey: str = "", osd_file: str = DEFAULT_OSD_FILE,
             position: str = "top-left", font_size: int = 20, include_fps: bool = True,
             hotkey_owner: str = "mangohud", start_hidden: bool = False,
             text_color: str = "FFFFFF", background_alpha: float = 0.4) -> str:
    """Return a complete MangoHud.conf showing `sensors` (dicts with id/group/label) in order.

    hotkey_owner:
      "mangohud" - MangoHud's own toggle_hud is set to the daemon's hotkey (translated to an X
                   keysym) and no control socket is opened, so the daemon's mangohudctl call
                   finds nothing and cannot double-toggle.
      "daemon"   - opens `control=mangohud` so the daemon can toggle the HUD with
                   `mangohudctl set hud`; MangoHud's own key is left at its default.
    """
    ks = evdev_to_keysym(hotkey) if hotkey else None
    lines = [
        "### meat³ in-game OSD - generated by meat3 GUI on " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "### Use: MANGOHUD_CONFIGFILE=<this file> mangohud %command%   (Steam launch options)",
        "### Values come from " + osd_file + " (written by meat3d every poll).",
        "### Regenerate from the GUI (OSD tab) after changing items; MangoHud reloads it live.",
        "legacy_layout=0",
        "table_columns=2",
        f"position={position}",
        f"font_size={int(font_size)}",
        f"text_color={text_color}",
        f"background_alpha={background_alpha:g}",
        "round_corners=6",
        "text_outline",
        "hud_compact",
    ]
    if include_fps:
        lines += ["fps", "frametime=0"]
    if start_hidden:
        lines.append("no_display")
    if hotkey_owner == "mangohud" and ks:
        lines.append(f"toggle_hud={ks}")
    elif hotkey_owner == "daemon":
        lines.append("control=mangohud")
    lines.append("")
    for n, s in enumerate(sensors, start=1):
        lines.append(f"custom_text={_clean(osd_label(s))}")
        lines.append(f"exec={exec_command(n, osd_file)}")
    return "\n".join(lines) + "\n"


def write_config(path: str, text: str) -> Path:
    """Atomic replace (MangoHud's inotify watcher re-arms on IN_DELETE_SELF)."""
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


def usage_text(config_path: str) -> str:
    return (f"Steam launch options:  MANGOHUD_CONFIGFILE={config_path} mangohud %command%\n"
            f"Terminal:              MANGOHUD_CONFIGFILE={config_path} mangohud <game>\n"
            "Lutris/Heroic: enable MangoHud and add the MANGOHUD_CONFIGFILE env var.\n"
            "Steam (pressure-vessel) games only see host /run if the daemon also mirrors osd.txt "
            "into the user's home; set 'osd.txt path' accordingly.")
