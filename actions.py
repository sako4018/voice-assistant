"""Какво Jarvis може да направи на компютъра.

Един вход: run_action(kind, target) -> кратък текст на български какво е станало.
Текстът се показва в рамката на екрана, затова е на български и е къс.

Логиката е същата като във версия 1 - сменени са само съобщенията и денилистът
вече важи и за run, и за kill.
"""
import ctypes
import os
import re
import subprocess
import time
import webbrowser
from urllib.parse import quote_plus

import pyautogui
import pyperclip

pyautogui.FAILSAFE = False

log = print          # jarvis.py го подменя с логването във файл

# Псевдоним -> голо име на програма, или протокол, завършващ на ':'
APP_ALIASES = {
    "calculator": "calc", "calc": "calc", "калкулатор": "calc",
    "notepad": "notepad", "notes": "notepad", "тефтер": "notepad",
    "browser": "chrome", "chrome": "chrome", "edge": "msedge",
    "explorer": "explorer", "files": "explorer", "file explorer": "explorer",
    "settings": "ms-settings:", "настройки": "ms-settings:",
    "terminal": "wt", "cmd": "cmd", "powershell": "powershell",
    "spotify": "spotify:", "task manager": "taskmgr", "paint": "mspaint",
    "word": "winword", "excel": "excel", "telegram": "telegram",
    "discord": "discord", "steam": "steam", "vscode": "code", "code": "code",
}

# Разпознаването може да сбърка - грешката не бива да е фатална.
_DANGER = re.compile(
    r"format\s+[a-z]:|\bdel\s+/[sq]|\brd\s+/s|\brmdir\s+/s|diskpart|"
    r"cipher\s+/w|:\\windows\\system32|reg\s+delete\s+hk|\bmkfs\b|"
    r"\bshutdown\s+/[fp]|vssadmin\s+delete",
    re.IGNORECASE,
)
_PROTOCOL = re.compile(r"^[a-z][a-z0-9.+-]*:$", re.IGNORECASE)

_MEDIA = {"play": 0xB3, "pause": 0xB3, "playpause": 0xB3, "toggle": 0xB3,
          "next": 0xB0, "prev": 0xB1, "previous": 0xB1, "back": 0xB1,
          "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}


def tap(vk, times=1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.04)


def _paste_text(text):
    """Пише през клипборда - pyautogui.write чупи кирилица.

    Клипбордът може да е зает от друга програма за момент, затова опитва
    няколко пъти и винаги връща това, което е било вътре преди това.
    """
    saved = None
    try:
        saved = pyperclip.paste()
    except Exception:
        pass
    for _ in range(3):
        try:
            pyperclip.copy(text)
            break
        except Exception:
            time.sleep(0.15)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    if saved is not None:
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


def run_action(kind, target):
    target = (target or "").strip()
    if _DANGER.search(target):
        log("blocked:", kind, target)
        return "спряно - опасна команда"
    try:
        if kind == "open_url":
            if not target.startswith(("http://", "https://")):
                target = "https://" + target
            webbrowser.open(target)
            return "отварям " + target

        if kind == "open_app":
            cmd = APP_ALIASES.get(target.lower(), target)
            if _PROTOCOL.match(cmd):
                os.startfile(cmd)
            else:
                subprocess.Popen('start "" %s' % cmd, shell=True)
            return "пускам " + target

        if kind == "search":
            webbrowser.open("https://www.google.com/search?q=" + quote_plus(target))
            return "търся: " + target

        if kind == "search_youtube":
            webbrowser.open("https://www.youtube.com/results?search_query="
                            + quote_plus(target))
            return "търся в YouTube: " + target

        if kind == "type":
            _paste_text(target)
            return "написах го"

        if kind == "media":
            vk = _MEDIA.get(target.lower().replace(" ", ""))
            if vk is None:
                return "не знам такъв медиен клавиш: " + target
            tap(vk, 5 if vk in (0xAF, 0xAE) else 1)
            return "медия: " + target

        if kind == "lock":
            subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
            return "заключвам"

        if kind == "sleep":
            subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
                             shell=True)
            return "приспивам компютъра"

        if kind in ("shutdown", "restart"):
            flag = "/s" if kind == "shutdown" else "/r"
            subprocess.Popen("shutdown %s /t 20" % flag, shell=True)
            word = "изключване" if kind == "shutdown" else "рестарт"
            return "%s след 20 сек - кажи 'отказ', за да спре" % word

        if kind == "cancel_shutdown":
            subprocess.Popen("shutdown /a", shell=True)
            return "отказах изключването"

        if kind == "kill":
            name = target.lower().replace(".exe", "").strip()
            if not name:
                return "не разбрах коя програма"
            subprocess.Popen('taskkill /IM "%s.exe" /F' % name, shell=True)
            return "затварям " + name

        if kind == "run":
            if not target:
                return "празна команда"
            subprocess.Popen(target, shell=True)
            return "изпълнявам"

        return "не знам как се прави '%s'" % kind
    except Exception as exc:
        log("action %s failed: %s" % (kind, exc))
        return "не стана"
