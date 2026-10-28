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
  N-th line of /tmp/meat3/osd.txt with `IFS=: read` and print the value part.
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

DEFAULT_OSD_FILE = "/tmp/meat3/osd.txt"        # /run isn't visible inside Steam's game container; /tmp is

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


MODIFIERS = {"KEY_LEFTCTRL", "KEY_RIGHTCTRL", "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT", "KEY_LEFTALT", "KEY_RIGHTALT",
             "KEY_LEFTMETA", "KEY_RIGHTMETA"}


def parse_hotkey(value) -> list[str]:
    """'rightctrl+F12', 'KEY_RIGHTCTRL+KEY_F12' or a list -> ['KEY_RIGHTCTRL', 'KEY_F12'].

    1-4 keys, the last one not a modifier, and every key one MangoHud can also bind (so the daemon's
    hotkey and MangoHud's toggle_hud stay the same combo). Raises ValueError with a readable reason."""
    parts = value if isinstance(value, (list, tuple)) else str(value or "").split("+")
    keys = []
    for p in parts:
        k = str(p).strip().upper().replace(" ", "")
        if not k:
            continue
        k = k if k.startswith("KEY_") else "KEY_" + k
        if evdev_to_keysym(k) is None:
            raise ValueError(f"unsupported key {k[4:]!r}")
        if k not in keys:
            keys.append(k)
    if not 1 <= len(keys) <= 4:
        raise ValueError("use 1 to 4 keys, e.g. RIGHTCTRL+F12")
    if keys[-1] in MODIFIERS:
        raise ValueError("end the combo with a normal key, e.g. RIGHTCTRL+F12")
    return [k for k in keys[:-1] if k in MODIFIERS] + [k for k in keys[:-1] if k not in MODIFIERS] + keys[-1:]


def set_toggle_key(path: str, hotkey: str) -> bool:
    """Point an existing meat3-generated config's toggle_hud at a new hotkey. False if the file
    doesn't exist or doesn't use toggle_hud (the 'daemon toggles MangoHud' mode needs no change)."""
    ks = evdev_to_keysym(hotkey)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    hit = False
    for i, ln in enumerate(lines):
        if ln.startswith("toggle_hud="):
            lines[i], hit = f"toggle_hud={ks}", True
    if hit and ks:
        write_config(path, "\n".join(lines) + "\n")
    return hit and bool(ks)


def env_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "environment.d" / "90-meat3-mangohud.conf"


def global_enabled() -> bool:
    return env_file().exists()


def set_global(enabled: bool, config_path: str) -> Path:
    """Load MangoHud into every Vulkan game (all Proton/DXVK/VKD3D games) via the session environment.

    MangoHud's implicit Vulkan layer switches on with MANGOHUD=1, and MANGOHUD_CONFIGFILE points it at
    meat3's config, so no per-game launch options are needed. systemd's environment.d is read at login
    (KDE and GNOME), so it takes effect after logging out and back in. OpenGL-only games still need
    the `mangohud` wrapper."""
    p = env_file()
    if not enabled:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return p
    cfg = os.path.abspath(os.path.expanduser(config_path))
    if any(c in cfg for c in "\n\"'$`\\ "):
        raise ValueError("MangoHud config path must not contain spaces, quotes, $ or backslashes")
    p.parent.mkdir(parents=True, exist_ok=True)
    write_config(str(p), "# written by meat3 (OSD tab > MangoHud in every game); delete this file to undo\n"
                         f"MANGOHUD=1\nMANGOHUD_CONFIGFILE={cfg}\n")
    return p


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
