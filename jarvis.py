"""Jarvis - гласов асистент за Windows.

Държиш десен Ctrl, казваш каквото искаш на български, пускаш клавиша.
Толкова.

Версия 1 работеше на събуждаща дума ("hey jarvis") и в реална употреба не сработи
нито веднъж - в лога имаше 0 чути команди. Всички реално ползвани проекти от този
тип (whisper-writer, VoiceInk, whisper-hotkey) са на клавиш, не на дума. Затова:

  клавиш -> запис -> локален Whisper -> Groq -> действие -> показва се на екрана

Настройките са в config.json до този файл.
"""
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path

# Рамката да излиза на правилното място и при мащабиране на екрана.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "jarvis.log"
try:
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
        LOG_PATH.replace(HERE / "jarvis.log.1")
except Exception:
    pass
_logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
_console = sys.stdout if (sys.stdout and hasattr(sys.stdout, "write")) else None
if _console is None:                # под pythonw.exe няма конзола изобщо
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
import sounddevice as sd
import winsound
from pynput import keyboard

import actions
import brain

actions.log = log
brain.log = log

# ─────────────────────────── настройки ───────────────────────────

DEFAULTS = {
    "hotkey": "ctrl_r",              # десен Ctrl - рядко се ползва за друго
    "language": "bg",
    "whisper_model": "small",        # tiny | base | small | medium | large-v3
    "whisper_device": "auto",        # auto | cuda | cpu
    "input_device": None,            # None = каквото е по подразбиране в момента
    "speak_replies": True,
    "tts_voice": "bg-BG-BorislavNeural",
}

CONFIG_PATH = HERE / "config.json"


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        log("няма config.json - ползвам настройките по подразбиране")
    except Exception as exc:
        log("лош config.json:", exc)
    return cfg


CFG = load_config()

MIN_SECONDS = 0.4            # по-кратко от това е случайно натискане
MAX_SECONDS = 20.0           # предпазител, ако клавишът "залепне"
RATE = 16000                 # Whisper иска точно това
HISTORY_RESET_AFTER = 120.0

CORNER_LEN = 170
MARGIN = 7
GLOW = [(11, "#062430"), (7, "#0d5a7a"), (4, "#27b6e8"), (2, "#c9f4ff")]

# ─────────────────────────── тонове ───────────────────────────


def cue(name):
    """Кратък звук, за да не е двусмислена тишината."""
    try:
        if name == "ready":
            for f in (523, 659, 784):
                winsound.Beep(f, 90)
        elif name == "start":
            winsound.Beep(880, 70)
        elif name == "stop":
            winsound.Beep(660, 70)
        elif name == "done":
            winsound.Beep(988, 80)
        elif name == "miss":
            winsound.Beep(400, 160)
        elif name == "error":
            winsound.Beep(300, 120)
            winsound.Beep(250, 160)
    except Exception:
        pass

# ─────────────────────────── глас навън ───────────────────────────


class Voice(threading.Thread):
    """Говори на български през edge-tts.

    Windows няма български SAPI глас (проверено: само Hazel, David, Zira),
    затова pyttsx3 от версия 1 не върши работа тук. edge-tts е безплатен и
    не иска ключ, но иска интернет - няма ли, просто мълчи и остава тонът.
    """

    def __init__(self, voice, enabled):
        super().__init__(daemon=True)
        self.voice = voice
        self.enabled = enabled
        self.q = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0

    @property
    def busy(self):
        with self._lock:
            return self._pending > 0

    def say(self, text):
        text = (text or "").strip()
        if not text or not self.enabled:
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
                self._speak(text)
            except Exception as exc:
                log("tts error:", exc)
            finally:
                with self._lock:
                    self._pending -= 1

    def _speak(self, text):
        import asyncio
        import io

        import av
        import edge_tts

        async def grab():
            buf = io.BytesIO()
            comm = edge_tts.Communicate(text, self.voice)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf

        buf = asyncio.run(grab())
        if not buf.tell():
            return
        buf.seek(0)
        with av.open(buf, format="mp3") as container:
            stream = container.streams.audio[0]
            rate = stream.rate
            chunks = [f.to_ndarray().reshape(-1) for f in container.decode(stream)]
        if not chunks:
            return
        pcm = np.concatenate(chunks)
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32) / 32768.0
        sd.play(pcm, rate)
        sd.wait()

# ─────────────────────────── рамка на екрана ───────────────────────────


class Overlay:
    """Рамка по ъглите + един ред текст долу.

    Текстът е важната разлика спрямо версия 1: винаги се вижда какво е чуто и
    какво е направено, вместо да гадаеш защо нищо не се е случило.
    """

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black")
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        self.w, self.h = w, h
        self.root.geometry("%dx%d+0+0" % (w, h))
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._draw_corners(w, h)
        self.box = self.canvas.create_rectangle(0, 0, 0, 0, fill="#0a1016",
                                                outline="#27b6e8", width=2,
                                                state="hidden")
        self.label = self.canvas.create_text(w // 2, h - 90, text="", fill="#dff6ff",
                                             font=("Segoe UI", 17), anchor="center",
                                             state="hidden")
        self.root.update_idletasks()
        self._click_through()
        self.root.withdraw()
        self.visible = False
        self.phase = 0.0
        self._hide_job = None

    def _draw_corners(self, w, h):
        m, c = MARGIN, CORNER_LEN
        segments = [
            (m, m, m + c, m), (m, m, m, m + c),
            (w - m, m, w - m - c, m), (w - m, m, w - m, m + c),
            (m, h - m, m + c, h - m), (m, h - m, m, h - m - c),
            (w - m, h - m, w - m - c, h - m), (w - m, h - m, w - m, h - m - c),
        ]
        self.corners = []
        for width, color in GLOW:
            for x1, y1, x2, y2 in segments:
                self.corners.append(self.canvas.create_line(
                    x1, y1, x2, y2, fill=color, width=width, capstyle="round"))

    def _click_through(self):
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) \
            or self.root.winfo_id()
        GWL_EXSTYLE = -20
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | 0x00080000 | 0x00000020 | 0x00000080)

    def _pulse(self):
        if not self.visible:
            return
        self.phase += 0.16
        self.root.attributes("-alpha",
                             0.60 + 0.35 * (0.5 + 0.5 * math.sin(self.phase)))
        self.root.after(40, self._pulse)

    def _set_corners(self, shown):
        state = "normal" if shown else "hidden"
        for item in self.corners:
            self.canvas.itemconfigure(item, state=state)

    def show(self, text="", corners=True, hold=None):
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        self._set_corners(corners)
        if text:
            self.canvas.itemconfigure(self.label, text=text, state="normal")
            self.root.update_idletasks()
            x1, y1, x2, y2 = self.canvas.bbox(self.label)
            self.canvas.coords(self.box, x1 - 22, y1 - 14, x2 + 22, y2 + 14)
            self.canvas.itemconfigure(self.box, state="normal")
            self.canvas.tag_raise(self.label)
        else:
            self.canvas.itemconfigure(self.label, state="hidden")
            self.canvas.itemconfigure(self.box, state="hidden")
        if not self.visible:
            self.visible = True
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self._pulse()
        if hold:
            self._hide_job = self.root.after(int(hold * 1000), self.hide)

    def hide(self):
        self._hide_job = None
        self.visible = False
        self.root.withdraw()

# ─────────────────────────── запис от микрофона ───────────────────────────


class Recorder:
    """Отваря микрофона за всеки запис поотделно.

    Версия 1 държеше един поток отворен с часове. Сложиш ли слушалки, Windows
    сменя устройството по подразбиране, а Jarvis продължаваше да слуша стария
    микрофон и мълчеше. Тук устройството се избира в момента на натискането.

    Записва на собствената честота на картата (при Sarkis 44100 Hz) и сваля до
    16 kHz със soxr - вместо да кара драйвера да преобразува.
    """

    def __init__(self, device=None):
        self.device = device
        self.stream = None
        self.chunks = []
        self.rate = RATE
        self.started = 0.0
        self._lock = threading.Lock()

    @property
    def running(self):
        return self.stream is not None

    def start(self):
        with self._lock:
            if self.stream is not None:
                return True
            try:
                info = sd.query_devices(self.device, "input")
                self.rate = int(info["default_samplerate"]) or RATE
                self.chunks = []
                self.stream = sd.InputStream(
                    samplerate=self.rate, channels=1, dtype="float32",
                    device=self.device, blocksize=0, callback=self._on_audio)
                self.stream.start()
                self.started = time.time()
                log("запис от %r @ %d Hz" % (info["name"], self.rate))
                return True
            except Exception as exc:
                log("микрофонът не се отваря:", exc)
                self.stream = None
                return False

    def _on_audio(self, indata, frames, t, status):
        self.chunks.append(indata.copy())

    def stop(self):
        """Връща (audio16k, секунди) или (None, секунди)."""
        with self._lock:
            if self.stream is None:
                return None, 0.0
            seconds = time.time() - self.started
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            chunks, self.chunks = self.chunks, []
        if not chunks:
            return None, seconds
        audio = np.concatenate(chunks).reshape(-1)
        if self.rate != RATE:
            audio = _resample(audio, self.rate, RATE)
        return audio, seconds


def _resample(audio, src, dst):
    try:
        import soxr
        return soxr.resample(audio, src, dst)
    except Exception as exc:       # без soxr - груба линейна интерполация
        log("soxr липсва (%s), ползвам проста интерполация" % exc)
        n = int(len(audio) * dst / src)
        return np.interp(np.linspace(0, len(audio) - 1, n),
                         np.arange(len(audio)), audio).astype(np.float32)

# ─────────────────────────── реч -> текст ───────────────────────────


_cuda_dll_added = False


def _add_cuda_dll_dirs():
    """pip-инсталираните nvidia-cublas/cudnn пакети слагат DLL-ите си в site-packages,
    а не в PATH - ctranslate2 не ги намира без изрично os.add_dll_directory."""
    global _cuda_dll_added
    if _cuda_dll_added:
        return
    _cuda_dll_added = True
    try:
        import site
        for base in site.getsitepackages():
            nvidia = Path(base) / "nvidia"
            if not nvidia.is_dir():
                continue
            for bin_dir in nvidia.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bin_dir))
                except Exception:
                    pass
    except Exception as exc:
        log("не можах да добавя CUDA DLL пътища:", exc)


class Ears:
    """faster-whisper, зареден при първата команда (не бави стартирането)."""

    def __init__(self, name, device, language):
        self.name = name
        self.device = device
        self.language = language
        self.model = None
        self.where = "?"
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self.model is not None:
                return self.model
            _add_cuda_dll_dirs()
            from faster_whisper import WhisperModel
            tries = [("cuda", "float16"), ("cpu", "int8")]
            if self.device == "cuda":
                tries = tries[:1]
            elif self.device == "cpu":
                tries = tries[1:]
            last = None
            for dev, ct in tries:
                try:
                    t = time.time()
                    model = WhisperModel(self.name, device=dev, compute_type=ct)
                    # cuBLAS/cuDNN load lazily on the first real inference, not
                    # on construction - so prove the device works with a tiny
                    # dummy run before trusting it, otherwise a missing DLL
                    # only surfaces later, mid-command, with no fallback left.
                    # Silence would let vad_filter skip the encode step entirely
                    # and hide a missing DLL, so use noise and disable the VAD.
                    dummy = np.random.uniform(-0.3, 0.3, RATE // 2).astype(np.float32)
                    list(model.transcribe(dummy, language=self.language,
                                          beam_size=1, vad_filter=False)[0])
                    self.model = model
                    self.where = dev
                    log("Whisper '%s' зареден на %s за %.1f сек"
                        % (self.name, dev, time.time() - t))
                    return self.model
                except Exception as exc:
                    log("Whisper на %s не тръгна: %s" % (dev, str(exc)[:160]))
                    last = exc
            raise last

    def listen(self, audio):
        model = self.load()
        segments, _ = model.transcribe(
            audio, language=self.language, beam_size=5, vad_filter=True,
            condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

# ─────────────────────────── свързването ───────────────────────────


class Jarvis:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ui = queue.Queue()
        self.stop = threading.Event()
        self.recorder = Recorder(cfg["input_device"])
        self.ears = Ears(cfg["whisper_model"], cfg["whisper_device"],
                         cfg["language"])
        self.brain = brain.Brain()
        self.voice = Voice(cfg["tts_voice"], cfg["speak_replies"])
        self.voice.start()
        self.history = []
        self.last_turn = 0.0
        self.aborted = False
        self.icon = None

    # ── клавишът ──

    def hotkey(self):
        name = self.cfg["hotkey"]
        return getattr(keyboard.Key, name, keyboard.Key.ctrl_r)

    def on_press(self, key):
        if key == self.hotkey():
            if not self.recorder.running:
                self.aborted = False
                self.begin()
        elif self.recorder.running:
            # Десен Ctrl + друг клавиш е обикновена клавишна комбинация,
            # не команда към Jarvis.
            self.aborted = True

    def on_release(self, key):
        if key == self.hotkey() and self.recorder.running:
            self.finish()

    def begin(self):
        if not self.recorder.start():
            self.tell("Микрофонът не се отваря", hold=3.0)
            cue("error")
            return
        cue("start")
        self.ui.put(("show", "Слушам…", True, None))
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        """Ако клавишът залепне или pynput изпусне пускането - спира сам."""
        end = time.time() + MAX_SECONDS
        while self.recorder.running and time.time() < end:
            time.sleep(0.1)
        if self.recorder.running:
            log("записът стигна %d сек - спирам сам" % MAX_SECONDS)
            self.finish()

    def finish(self):
        audio, seconds = self.recorder.stop()
        cue("stop")
        if self.aborted:
            self.ui.put(("hide", None, None, None))
            return
        if audio is None or seconds < MIN_SECONDS:
            self.ui.put(("hide", None, None, None))
            return
        self.ui.put(("show", "Мисля…", True, None))
        threading.Thread(target=self.process, args=(audio, seconds),
                         daemon=True).start()

    # ── обработката ──

    def process(self, audio, seconds):
        try:
            level = float(np.sqrt(np.mean(audio ** 2)))
            t = time.time()
            text = self.ears.listen(audio)
            log("%.1f сек, ниво %.4f, %.1f сек разпознаване -> %r"
                % (seconds, level, time.time() - t, text))
            if not text:
                cue("miss")
                msg = ("Не чух нищо - микрофонът е много тих"
                       if level < 0.002 else "Не разбрах")
                self.tell(msg, hold=3.0)
                return

            if time.time() - self.last_turn > HISTORY_RESET_AFTER:
                self.history.clear()
            self.last_turn = time.time()

            self.tell("„%s“" % text, hold=None)
            self.history.append({"role": "user", "content": text})
            reply = self.brain.ask(self.history)
            self.history.append({"role": "assistant", "content": reply})
            del self.history[:-12]

            spoken, acts = brain.split_action(reply)
            results = []
            for kind, target in acts:
                res = actions.run_action(kind, target)
                log("[action]", kind, target, "->", res)
                results.append(res)

            shown = spoken or "; ".join(results) or "Готово."
            if results:
                shown = "„%s“  →  %s" % (text, "; ".join(results))
            else:
                shown = "„%s“  →  %s" % (text, spoken or "…")
            self.tell(shown, hold=4.0)
            cue("done")
            if spoken:
                self.voice.say(spoken)
        except Exception:
            log("команда се спъна:\n" + traceback.format_exc())
            cue("error")
            self.tell("Нещо се обърка - виж jarvis.log", hold=4.0)

    def tell(self, text, hold=3.0):
        self.ui.put(("show", text, False, hold))

    # ── трей ──

    def build_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:
            log("няма трей икона:", exc)
            return None

        def dot(color):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((10, 10, 54, 54), fill=color)
            return img

        menu = pystray.Menu(
            pystray.MenuItem("Провери", lambda *_: threading.Thread(
                target=self.selftest, daemon=True).start()),
            pystray.MenuItem("Покажи лога", lambda *_: os.startfile(LOG_PATH)),
            pystray.MenuItem("Настройки (config.json)",
                             lambda *_: self.open_config()),
            pystray.MenuItem("Рестартирай", lambda *_: self.restart()),
            pystray.MenuItem("Изход", lambda *_: self.quit()),
        )
        self.icon = pystray.Icon("jarvis", dot((39, 182, 232, 255)),
                                 "Jarvis - дръж десен Ctrl и говори", menu)
        return self.icon

    def open_config(self):
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(
                json.dumps(DEFAULTS, indent=2, ensure_ascii=False), encoding="utf-8")
        os.startfile(CONFIG_PATH)

    def restart(self):
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else py
        subprocess.Popen(
            'cmd /c ping -n 3 127.0.0.1 >nul & start "" "%s" "%s"'
            % (exe, Path(__file__).resolve()), shell=True)
        self.quit()

    def quit(self):
        self.stop.set()
        self.ui.put(("quit", None, None, None))

    def selftest(self):
        """Проверява по ред това, което може да се счупи, и го показва."""
        parts = []
        try:
            info = sd.query_devices(self.cfg["input_device"], "input")
            parts.append("микрофон: %s" % info["name"])
        except Exception as exc:
            parts.append("микрофон: ГРЕШКА (%s)" % exc)
        parts.append("OmniRoute: %s" % ("върви" if brain.omniroute_up() else "СПРЯН"))
        try:
            self.ears.load()
            parts.append("Whisper: %s на %s" % (self.ears.name, self.ears.where))
        except Exception as exc:
            parts.append("Whisper: ГРЕШКА (%s)" % str(exc)[:60])
        r = self.brain.ask([{"role": "user", "content": "кажи само: готов"}])
        parts.append("мозък: %s" % r.replace("\n", " ")[:60])
        text = "\n".join(parts)
        log("проверка:\n" + text)
        ctypes.windll.user32.MessageBoxW(0, text, "Jarvis - проверка", 0x40)

    # ── главният цикъл ──

    def pump(self, overlay):
        try:
            while True:
                what, text, corners, hold = self.ui.get_nowait()
                if what == "quit":
                    try:
                        if self.icon:
                            self.icon.stop()
                    except Exception:
                        pass
                    overlay.root.quit()
                    return
                if what == "hide":
                    overlay.hide()
                else:
                    overlay.show(text or "", corners=bool(corners), hold=hold)
        except queue.Empty:
            pass
        overlay.root.after(50, lambda: self.pump(overlay))


# ─────────────────────────── стартиране ───────────────────────────


def claim_single_instance():
    """Едно копие наведнъж - иначе две се бият за микрофона."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "JarvisVoiceAssistant")
    return ctypes.windll.kernel32.GetLastError() != 183


def main():
    if "--check" in sys.argv:
        j = Jarvis(CFG)
        j.selftest()
        return 0
    if not claim_single_instance():
        log("вече върви един Jarvis - излизам")
        return 0

    log("=== Jarvis тръгва ===")
    j = Jarvis(CFG)
    overlay = Overlay()

    icon = j.build_tray()
    if icon is not None:
        threading.Thread(target=icon.run, daemon=True).start()

    listener = keyboard.Listener(on_press=j.on_press, on_release=j.on_release)
    listener.start()

    threading.Thread(target=brain.ensure_omniroute, daemon=True).start()

    cue("ready")
    log("готов - дръж %s и говори" % CFG["hotkey"])
    j.pump(overlay)
    overlay.root.mainloop()
    listener.stop()
    log("=== Jarvis спира ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
