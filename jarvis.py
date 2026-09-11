import ctypes
import gc
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

# Correct overlay geometry if the display ever uses DPI scaling.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# Keep a log next to the script. Under pythonw.exe (autostart) there is no
# console at all, so stdout/stderr are None and a bare print() would crash.
LOG_PATH = Path(__file__).with_name("jarvis.log")
try:
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
except Exception:
    pass
_logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
_console = sys.stdout if (sys.stdout and hasattr(sys.stdout, "write")) else None
if _console is None:
    sys.stdout = sys.stderr = _logf


def log(*parts):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), " ".join(str(p) for p in parts))
    try:
        _logf.write(line + "\n")
    except Exception:
        pass
    if _console is not None:
        try:
            _console.write(line + "\n")
            _console.flush()
        except Exception:
            pass


import numpy as np
import pyaudio
import pyautogui
import pyperclip
import pyttsx3
import requests
import speech_recognition as sr
import winsound
from openwakeword.model import Model

pyautogui.FAILSAFE = False

# ─────────────────────────── settings ───────────────────────────

SAMPLE_RATE = 16000
CHUNK = 1280                  # 80 ms frames - what openWakeWord expects
THRESHOLD = 0.5               # measured: real "hey jarvis" peaks at 0.999,
WAKE_FRAMES = 2               # holds 7-8 frames; "hey Travis" only reaches 0.39

PHRASE_LIMIT = 12.0           # max seconds of one spoken command
START_TIMEOUT = 6.0           # seconds to start speaking after the beep

OMNIROUTE_URL = "http://localhost:20128/v1/chat/completions"
OMNIROUTE_PING = "http://localhost:20128/"
MODEL = "kr/claude-haiku-4.5"          # fastest free path (Kiro), ~2-3 s
MODEL_FALLBACK = "auto/claude-sonnet"
BRAIN_TIMEOUT = 20

HISTORY_RESET_AFTER = 120.0   # seconds idle -> start a fresh conversation

CORNER_LEN = 170
MARGIN = 7
GLOW = [(11, "#062430"), (7, "#0d5a7a"), (4, "#27b6e8"), (2, "#c9f4ff")]

SYSTEM_PROMPT = (
    "You are Jarvis, a voice assistant running on Sarkis's Windows PC. "
    "Reply in English, ONE short spoken sentence, no markdown, no lists. "
    "If the user wants an action done on the computer, append it on a FINAL new "
    "line in exactly this format:\n"
    "[ACTION] <kind> <target>\n"
    "Valid kinds:\n"
    "  open_url <url>            e.g. [ACTION] open_url https://youtube.com\n"
    "  open_app <name>           e.g. [ACTION] open_app notepad\n"
    "  search <query>            google/web search (default for 'search for', 'look up', 'google')\n"
    "  search_youtube <query>    youtube search (only when the user mentions YouTube, a video, or 'watch')\n"
    "  type <text>               type text at the cursor - copy the user's words after 'type' VERBATIM\n"
    "  media <play|pause|next|prev|volup|voldown|mute>\n"
    "  lock                      lock the screen\n"
    "  sleep                     put the PC to sleep\n"
    "  shutdown | restart        (20 second cancel window)\n"
    "  cancel_shutdown\n"
    "  kill <app name>           close a program\n"
    "  run <shell command>       run a Windows command for anything not covered above\n"
    "Examples: 'put on music' -> [ACTION] media play | 'open my email' -> "
    "[ACTION] open_url https://mail.google.com | 'lock the pc' -> [ACTION] lock. "
    "If no action is needed (a question, chit-chat), do NOT write an [ACTION] line. "
    "Before the [ACTION] line say in a few words what you are doing (e.g. 'Opening YouTube.')."
)

# Alias -> bare app name, or a URI protocol ending in ':'
APP_ALIASES = {
    "calculator": "calc", "calc": "calc",
    "notepad": "notepad", "notes": "notepad",
    "browser": "chrome", "chrome": "chrome", "edge": "msedge",
    "explorer": "explorer", "files": "explorer", "file explorer": "explorer",
    "settings": "ms-settings:",
    "terminal": "wt", "cmd": "cmd", "powershell": "powershell",
    "spotify": "spotify:", "task manager": "taskmgr", "paint": "mspaint",
}

# A mishearing must never be catastrophic - refuse these outright.
_DANGER = re.compile(
    r"format\s+[a-z]:|\bdel\s+/[sq]|\brd\s+/s|\brmdir\s+/s|diskpart|"
    r"cipher\s+/w|:\\windows\\system32|reg\s+delete\s+hk|\bmkfs\b",
    re.IGNORECASE,
)
_PROTOCOL = re.compile(r"^[a-z][a-z0-9.+-]*:$", re.IGNORECASE)

# Said in one breath ("hey jarvis, open spotify") the wake word lands in the
# transcript too. Strip it so the brain sees just the command.
_LEADING_WAKE = re.compile(
    r"^\s*(hey|hi|ok|okay)?\s*(jarvis|travis|jervis|charvis)\b[\s,.:;-]*",
    re.IGNORECASE,
)

STOP_PHRASES = ("shut down jarvis", "shutdown jarvis", "stop listening",
                "goodbye jarvis", "go to sleep jarvis")

# ─────────────────────────── sound cues ───────────────────────────


def cue(name):
    """Short non-verbal feedback, so silence is never ambiguous."""
    try:
        if name == "ready":
            for f in (523, 659, 784):
                winsound.Beep(f, 90)
        elif name == "wake":
            winsound.Beep(880, 90)
        elif name == "miss":
            winsound.Beep(400, 160)
        elif name == "error":
            winsound.Beep(300, 120)
            winsound.Beep(250, 160)
    except Exception:
        pass

# ─────────────────────────── voice out ───────────────────────────


class Speaker(threading.Thread):
    """pyttsx3 on its own thread, a fresh engine per line.

    Reusing one engine looked fine but silently dropped every utterance after
    the first (measured: 0.07 s "spoken" vs 3.2 s real), and save_to_file on a
    reused engine deadlocked outright.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0          # queued + in-flight; a flag would race

    @property
    def busy(self):
        with self._lock:
            return self._pending > 0

    def say(self, text):
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._pending += 1
        self.q.put(text)

    def wait_idle(self, timeout):
        end = time.time() + timeout
        while self.busy and time.time() < end:
            time.sleep(0.05)

    def run(self):
        while True:
            text = self.q.get()
            try:
                eng = pyttsx3.init()
                eng.setProperty("rate", 185)
                eng.say(text)
                eng.runAndWait()
                eng.stop()
                del eng
                gc.collect()
            except Exception as exc:
                log("tts error:", exc)
            finally:
                with self._lock:
                    self._pending -= 1

# ─────────────────────────── overlay ───────────────────────────


class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black")
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+0+0" % (w, h))
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._draw(w, h)
        self.root.update_idletasks()
        self._click_through()
        self.root.withdraw()
        self.visible = False
        self.phase = 0.0

    def _draw(self, w, h):
        m, c = MARGIN, CORNER_LEN
        segments = [
            (m, m, m + c, m), (m, m, m, m + c),
            (w - m, m, w - m - c, m), (w - m, m, w - m, m + c),
            (m, h - m, m + c, h - m), (m, h - m, m, h - m - c),
            (w - m, h - m, w - m - c, h - m), (w - m, h - m, w - m, h - m - c),
        ]
        for width, color in GLOW:
            for x1, y1, x2, y2 in segments:
                self.canvas.create_line(x1, y1, x2, y2, fill=color,
                                        width=width, capstyle="round")

    def _click_through(self):
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        if not hwnd:
            hwnd = self.root.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    def _pulse(self):
        if not self.visible:
            return
        self.phase += 0.16
        self.root.attributes("-alpha", 0.60 + 0.35 * (0.5 + 0.5 * math.sin(self.phase)))
        self.root.after(40, self._pulse)

    def show(self):
        if self.visible:
            return
        self.visible = True
        self.phase = 0.0
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._pulse()

    def hide(self):
        self.visible = False
        self.root.withdraw()

# ─────────────────────────── tray icon ───────────────────────────


def make_tray(on_quit):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as exc:
        log("tray disabled:", exc)
        return None

    def dot(color):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((10, 10, 54, 54), fill=color)
        return img

    icon = pystray.Icon(
        "jarvis", dot((39, 182, 232, 255)), "Jarvis - waiting",
        menu=pystray.Menu(pystray.MenuItem("Quit Jarvis", lambda *_: on_quit())))
    icon._img_idle = dot((39, 182, 232, 255))
    icon._img_busy = dot((80, 220, 120, 255))
    return icon


def tray_state(icon, busy):
    if icon is None:
        return
    try:
        icon.icon = icon._img_busy if busy else icon._img_idle
        icon.title = "Jarvis - listening" if busy else "Jarvis - waiting"
    except Exception:
        pass

# ─────────────────────────── OmniRoute brain ───────────────────────────


def omniroute_up():
    try:
        requests.get(OMNIROUTE_PING, timeout=3)
        return True
    except requests.RequestException:
        return False


# Measured on this machine: a cold `omniroute serve` needs ~75 s before it
# answers. The lock guarantees only ever one launcher, otherwise every failed
# command would spawn another server racing for the same port.
_omni_lock = threading.Lock()
OMNI_COLD_START = 180


def omniroute_starting():
    return _omni_lock.locked()


def ensure_omniroute():
    """Start the local OmniRoute server if it is not already running.
    Safe to call from anywhere - concurrent calls collapse into one launch."""
    if omniroute_up():
        return True
    if not _omni_lock.acquire(blocking=False):
        return False                      # another thread is already on it
    try:
        if omniroute_up():
            return True
        log("starting OmniRoute (cold start takes over a minute)...")
        try:
            subprocess.Popen("omniroute serve", shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            log("could not launch omniroute:", exc)
            return False
        started = time.time()
        while time.time() - started < OMNI_COLD_START:
            time.sleep(2.0)
            if omniroute_up():
                log("OmniRoute is up after %.0f s" % (time.time() - started))
                return True
        log("OmniRoute did not come up within %d s" % OMNI_COLD_START)
        return False
    finally:
        _omni_lock.release()


def think(history):
    """One pass over the models. Never retries in a way that leaves Jarvis
    silent for minutes - a refused connection fails instantly and we say so."""
    for model in (MODEL, MODEL_FALLBACK):
        payload = {
            "model": model, "stream": False, "max_tokens": 300,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        }
        try:
            r = requests.post(OMNIROUTE_URL, json=payload, timeout=BRAIN_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
            log("brain returned no choices (%s): %s" % (model, str(data)[:200]))
        except requests.RequestException as exc:
            log("think error (%s): %s" % (model, exc))
    if omniroute_starting():
        return "My brain is still starting up, give it a minute."
    # Bring the server back up in the background so the next command works.
    threading.Thread(target=ensure_omniroute, daemon=True).start()
    return "My brain is offline, I'm starting it now. Try again in a minute."

# ─────────────────────────── actions ───────────────────────────

_ACTION_RE = re.compile(r"^\s*\[ACTION\]\s+(\w+)\s*(.*?)\s*$", re.MULTILINE)


def split_action(reply):
    actions = [(m.group(1).lower(), m.group(2).strip())
               for m in _ACTION_RE.finditer(reply)]
    spoken = _ACTION_RE.sub("", reply).strip()
    return spoken, actions


def tap(vk, times=1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.04)


_MEDIA = {"play": 0xB3, "pause": 0xB3, "playpause": 0xB3, "toggle": 0xB3,
          "next": 0xB0, "prev": 0xB1, "previous": 0xB1, "back": 0xB1,
          "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}


def _paste_text(text):
    """Type through the clipboard - pyautogui.write mangles non-ASCII.
    The clipboard can be momentarily locked by another app, so retry, and
    always put back whatever was there before."""
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
    target = target.strip()
    try:
        if kind == "open_url":
            if not target.startswith(("http://", "https://")):
                target = "https://" + target
            webbrowser.open(target)
            return "opened " + target

        if kind == "open_app":
            cmd = APP_ALIASES.get(target.lower(), target)
            if _PROTOCOL.match(cmd):
                os.startfile(cmd)
            else:
                subprocess.Popen('start "" %s' % cmd, shell=True)
            return "launching " + target

        if kind == "search":
            webbrowser.open("https://www.google.com/search?q=" + quote_plus(target))
            return "searching " + target

        if kind == "search_youtube":
            webbrowser.open("https://www.youtube.com/results?search_query=" + quote_plus(target))
            return "searching YouTube for " + target

        if kind == "type":
            _paste_text(target)
            return "typed it"

        if kind == "media":
            vk = _MEDIA.get(target.lower().replace(" ", ""))
            if vk is None:
                return "unknown media key " + target
            tap(vk, 5 if vk in (0xAF, 0xAE) else 1)
            return "media " + target

        if kind == "lock":
            subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
            return "locking"

        if kind == "sleep":
            subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
            return "sleeping"

        if kind in ("shutdown", "restart"):
            flag = "/s" if kind == "shutdown" else "/r"
            subprocess.Popen("shutdown %s /t 20" % flag, shell=True)
            return "%s in 20 seconds, say cancel to stop" % kind

        if kind == "cancel_shutdown":
            subprocess.Popen("shutdown /a", shell=True)
            return "shutdown cancelled"

        if kind == "kill":
            name = target.lower().replace(".exe", "").strip()
            if not name or _DANGER.search(name):
                return "that is blocked"
            subprocess.Popen('taskkill /IM "%s.exe" /F' % name, shell=True)
            return "closing " + name

        if kind == "run":
            if not target or _DANGER.search(target):
                return "that command is blocked"
            subprocess.Popen(target, shell=True)
            return "running it"

        return "I don't know how to " + kind
    except Exception as exc:
        log("action %s failed: %s" % (kind, exc))
        return "that failed"

# ─────────────────────────── the shared microphone ───────────────────────────


class _SharedSource(sr.AudioSource):
    """Presents the live PyAudio stream to SpeechRecognition.

    sr.Recognizer only needs .stream.read(CHUNK) -> bytes plus the three
    format attributes, so its battle-tested listen() logic (energy threshold,
    end-of-phrase detection, timeouts) runs directly on our own stream.
    """

    class _Reader:
        def __init__(self, owner):
            self.owner = owner

        def read(self, n):
            return self.owner.stream.read(n, exception_on_overflow=False)

    def __init__(self, owner):
        self.stream = self._Reader(owner)
        self.SAMPLE_RATE = SAMPLE_RATE
        self.SAMPLE_WIDTH = 2
        self.CHUNK = CHUNK

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Mic:
    """One PyAudio input stream, shared by the wake word and the recognizer.

    The earlier design closed this stream and opened a separate sr.Microphone
    for each command. That lost ~0.3 s at exactly the wrong moment: saying
    "hey jarvis, open spotify" in one breath (which the wake model detects
    happily) dropped the "open spotify" part into the gap. Keeping one stream
    means the command audio is already flowing when the wake word fires.
    """

    def __init__(self, pa):
        self.pa = pa
        self.stream = None
        self.source = _SharedSource(self)

    def open(self, tries=5):
        for i in range(tries):
            try:
                self.stream = self.pa.open(
                    rate=SAMPLE_RATE, channels=1, format=pyaudio.paInt16,
                    input=True, frames_per_buffer=CHUNK)
                return True
            except Exception as exc:
                log("mic open failed (%d/%d): %s" % (i + 1, tries, exc))
                time.sleep(2.0)
        return False

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def frame(self):
        return np.frombuffer(self.stream.read(CHUNK, exception_on_overflow=False),
                             dtype=np.int16)

    def drain(self):
        """Throw away buffered audio - notably Jarvis's own voice, which would
        otherwise be sitting in the buffer waiting to be heard as a command."""
        try:
            while self.stream.get_read_available() >= CHUNK:
                self.stream.read(CHUNK, exception_on_overflow=False)
        except Exception:
            pass


def transcribe(recognizer, mic):
    """Listen on the shared stream. Returns (text, reason)."""
    try:
        audio = recognizer.listen(mic.source, timeout=START_TIMEOUT,
                                  phrase_time_limit=PHRASE_LIMIT)
    except sr.WaitTimeoutError:
        return None, "silence"
    except Exception as exc:
        log("capture error:", exc)
        return None, "audio"
    try:
        return recognizer.recognize_google(audio, language="en-US"), "ok"
    except sr.UnknownValueError:
        return None, "unclear"
    except sr.RequestError as exc:
        log("google stt error:", exc)
        return None, "service"

# ─────────────────────────── command handling ───────────────────────────


def handle_command(text, history, speaker):
    """Returns False when Jarvis should shut down."""
    low = text.lower().strip(" .!?")
    if any(p in low for p in STOP_PHRASES):
        speaker.say("Goodbye.")
        speaker.wait_idle(5)
        return False

    history.append({"role": "user", "content": text})
    reply = think(history)
    history.append({"role": "assistant", "content": reply})
    del history[:-12]

    spoken, actions = split_action(reply)
    if spoken:
        speaker.say(spoken)
    elif not actions:
        speaker.say("Done.")
    for kind, tgt in actions:
        log("[action]", kind, tgt, "->", run_action(kind, tgt))
    return True


def audio_loop(q, stop, icon, speaker):
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold = 1.0        # allow a short think-pause mid-sentence
    oww = Model(wakeword_models=["hey_jarvis_v0.1"], inference_framework="onnx")
    pa = pyaudio.PyAudio()
    mic = Mic(pa)
    history = []
    last_turn = 0.0
    calibrated = False

    threading.Thread(target=ensure_omniroute, daemon=True).start()

    try:
        while not stop.is_set():
            if mic.stream is None and not mic.open():
                cue("error")
                log("no microphone - retrying in 10 s")
                time.sleep(10)
                continue

            if not calibrated:
                try:
                    recognizer.adjust_for_ambient_noise(mic.source, duration=0.8)
                    log("ambient noise level: %.0f" % recognizer.energy_threshold)
                except Exception as exc:
                    log("calibration failed:", exc)
                calibrated = True
                cue("ready")
                log('Jarvis ready - waiting for "hey jarvis"')

            # ── wait for the wake word ──
            oww.reset()
            hot = 0
            try:
                while not stop.is_set():
                    score = oww.predict(mic.frame())["hey_jarvis_v0.1"]
                    hot = hot + 1 if score >= THRESHOLD else 0
                    if hot >= WAKE_FRAMES:
                        break
            except Exception as exc:
                log("wake loop lost the mic:", exc)
                mic.close()
                continue

            if stop.is_set():
                break

            # ── capture the command from the same, still-open stream ──
            cue("wake")
            q.put("show")
            tray_state(icon, True)
            try:
                text, reason = transcribe(recognizer, mic)
            except Exception:
                log("capture crashed:\n" + traceback.format_exc())
                text, reason = None, "audio"
            q.put("hide")
            tray_state(icon, False)

            if reason == "audio":
                mic.close()          # stream is suspect - rebuild it next pass

            if not text:
                cue("miss" if reason in ("silence", "unclear") else "error")
                log("no command (%s)" % reason)
                continue

            log("heard:", text)
            text = _LEADING_WAKE.sub("", text).strip()
            if not text:
                cue("miss")
                continue

            if time.time() - last_turn > HISTORY_RESET_AFTER:
                history.clear()
            last_turn = time.time()

            try:
                if not handle_command(text, history, speaker):
                    stop.set()
                    break
            except Exception:
                log("command crashed:\n" + traceback.format_exc())
                cue("error")

            # Wait out our own voice, then drop it from the buffer so Jarvis
            # never hears himself as the next command.
            speaker.wait_idle(20)
            time.sleep(0.3)
            if mic.stream is not None:
                mic.drain()
    finally:
        mic.close()
        pa.terminate()
        q.put("quit")

# ─────────────────────────── glue ───────────────────────────


def poll(overlay, q, icon):
    try:
        while True:
            msg = q.get_nowait()
            if msg == "show":
                overlay.show()
            elif msg == "hide":
                overlay.hide()
            elif msg == "quit":
                if icon is not None:
                    try:
                        icon.stop()
                    except Exception:
                        pass
                overlay.root.destroy()
                return
    except queue.Empty:
        pass
    overlay.root.after(50, lambda: poll(overlay, q, icon))


def claim_single_instance():
    """Only one Jarvis may hold the microphone. Autostart plus a manual launch
    would otherwise leave two instances fighting over it and neither working.
    Kept out of import so the module stays testable while Jarvis is running."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "JarvisVoiceAssistant")
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def main():
    if not claim_single_instance():
        log("another Jarvis is already running - exiting")
        return 0
    log("=== Jarvis starting ===")
    overlay = Overlay()
    q = queue.Queue()
    stop = threading.Event()

    def request_quit():
        stop.set()
        q.put("quit")

    icon = make_tray(request_quit)
    if icon is not None:
        try:
            icon.run_detached()
        except Exception as exc:
            log("tray run_detached failed:", exc)
            icon = None

    speaker = Speaker()
    speaker.start()

    threading.Thread(target=audio_loop, args=(q, stop, icon, speaker),
                     daemon=True).start()

    overlay.root.after(50, lambda: poll(overlay, q, icon))
    try:
        overlay.root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    log("=== Jarvis stopped ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
