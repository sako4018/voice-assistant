"""Мозъкът: изпраща чутото на OmniRoute (локален безплатен AI) и връща
(какво да каже, какво да направи).

Всяка команда минава оттук - няма фиксирани фрази никъде в кода. Моделът връща
едно изречение на английски (Whisper е много по-точен на английски отколкото
на български - оттук идва и езикът на командите) и по избор последен ред:

    [ACTION] <вид> <цел>

OmniRoute е избран, за да не се иска нов ключ/акаунт - Sarkis вече го има
настроен. Цената е студен старт от ~75 сек, ако сървърът не върви - затова
ensure_omniroute() го вдига сам и се пуска и от автостарта на Windows.
"""
import re
import subprocess
import threading
import time

import requests

log = print          # jarvis.py го подменя с логването във файл

API_URL = "http://localhost:20128/v1/chat/completions"
PING_URL = "http://localhost:20128/"
TIMEOUT = 20

# Пробват се по ред - kr/claude-haiku-4.5 е измереният най-бърз безплатен път.
DEFAULT_MODELS = ["kr/claude-haiku-4.5", "auto/claude-sonnet"]

# Измерено на тази машина: студеният старт на "omniroute serve" отнема ~75 сек.
# Ключалката гарантира само едно вдигане - иначе всяка неуспешна команда би
# пуснала нов сървър, който се бие за същия порт.
_omni_lock = threading.Lock()
OMNI_COLD_START = 180

SYSTEM_PROMPT = (
    "You are Jarvis, a voice assistant running on Sarkis's Windows PC. "
    "Reply in English, ONE short spoken sentence, no markdown, no lists.\n"
    "If the user wants something done on the computer, append it on a FINAL "
    "new line in exactly this format:\n"
    "[ACTION] <kind> <target>\n"
    "Valid kinds:\n"
    "  open_url <url>            e.g. [ACTION] open_url https://youtube.com\n"
    "  open_app <name>           e.g. [ACTION] open_app notepad\n"
    "  search <query>            google/web search - default for 'search for', 'look up', 'google'\n"
    "  search_youtube <query>    youtube search - only when the user mentions YouTube, a video, or 'watch'\n"
    "  type <text>               type text at the cursor - copy the user's words VERBATIM. "
    "If the sentence starts with 'type'/'write', it is ALWAYS this command - whatever "
    "follows is text to type, NOT chit-chat with you, even if it sounds like a greeting "
    "or a question.\n"
    "  media <play|pause|next|prev|volup|voldown|mute>\n"
    "  lock                      lock the screen\n"
    "  sleep                     put the PC to sleep\n"
    "  shutdown | restart        (20 second cancel window)\n"
    "  cancel_shutdown\n"
    "  kill <app name>           close a program\n"
    "  run <shell command>       for anything not covered above\n"
    "Examples: 'put on music' -> [ACTION] media play | 'pause the music' -> "
    "[ACTION] media pause | 'turn it up' -> [ACTION] media volup | 'next song' -> "
    "[ACTION] media next | 'lock the pc' -> [ACTION] lock | 'open my email' -> "
    "[ACTION] open_url https://mail.google.com | 'close chrome' -> [ACTION] kill chrome.\n"
    "If no action is needed (a question, chit-chat), do NOT write an [ACTION] line, "
    "just reply."
)

_ACTION_RE = re.compile(r"^\s*\[ACTION\]\s+(\w+)\s*(.*?)\s*$", re.MULTILINE)


def split_action(reply):
    """Разделя отговора на изговорена част и списък действия."""
    actions = [(m.group(1).lower(), m.group(2).strip())
               for m in _ACTION_RE.finditer(reply)]
    spoken = _ACTION_RE.sub("", reply).strip()
    return spoken, actions


def omniroute_up():
    try:
        requests.get(PING_URL, timeout=3)
        return True
    except requests.RequestException:
        return False


def omniroute_starting():
    return _omni_lock.locked()


def ensure_omniroute():
    """Пуска локалния OmniRoute сървър, ако не върви. Може да се вика от
    навсякъде - паралелни извиквания се сливат в едно пускане."""
    if omniroute_up():
        return True
    if not _omni_lock.acquire(blocking=False):
        return False                      # друга нишка вече го вдига
    try:
        if omniroute_up():
            return True
        log("вдигам OmniRoute (студеният старт отнема над минута)...")
        try:
            subprocess.Popen("omniroute serve", shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            log("не мога да пусна omniroute:", exc)
            return False
        started = time.time()
        while time.time() - started < OMNI_COLD_START:
            time.sleep(2.0)
            if omniroute_up():
                log("OmniRoute тръгна за %.0f сек" % (time.time() - started))
                return True
        log("OmniRoute не тръгна в рамките на %d сек" % OMNI_COLD_START)
        return False
    finally:
        _omni_lock.release()


class Brain:
    def __init__(self, models=None):
        self.models = list(models) if models else list(DEFAULT_MODELS)

    @property
    def ready(self):
        return True          # без ключ - готовността зависи само от сървъра

    def ask(self, history):
        """history = [{'role': 'user'|'assistant', 'content': ...}, ...]

        Връща текста на отговора. Никога не хвърля и никога не виси -
        видяно на живо: заявка към OmniRoute увисна над 5 минути (по-дълго
        от requests-таймаута, вероятно мрежов проблем на самата машина).
        Затова заявката се пуска в отделна нишка с твърд краен срок -
        ask() винаги се връща, каквото и да прави мрежата.
        """
        if not omniroute_up():
            threading.Thread(target=ensure_omniroute, daemon=True).start()
            if omniroute_starting():
                return "Мозъкът се стартира, изчакай минута и опитай пак."
            return "Мозъкът е изключен, пускам го - опитай пак след малко."

        for model in self.models:
            payload = {
                "model": model, "stream": False, "max_tokens": 300,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
            }
            for attempt in range(2):    # kiro понякога излиза от роля - втори опит лекува повечето случаи
                try:
                    r = _post_with_hard_deadline(payload, TIMEOUT + 5)
                    r.raise_for_status()
                    data = r.json()
                    if "choices" not in data:
                        log("мозъкът не върна отговор (%s): %s" % (model, str(data)[:200]))
                        break
                    text = data["choices"][0]["message"]["content"].strip()
                    if _in_character(text):
                        return text
                    log("мозъкът излезе от роля (%s, опит %d): %s"
                        % (model, attempt + 1, text[:120]))
                except _HardTimeout:
                    log("мозъкът увисна над твърдия срок (%s) - продължавам без отговор"
                        % model)
                    break
                except requests.RequestException as exc:
                    log("грешка при мозъка (%s): %s" % (model, exc))
                    break
        return "Мозъкът не отговори правилно, опитай пак."


_OUT_OF_CHARACTER = re.compile(
    r"\bkiro\b|development environment|core instructions|i can'?t discuss|"
    r"i operate under|i can'?t respond to messages",
    re.IGNORECASE,
)


def _in_character(text):
    """Командите вече са на английски, затова езикът на отговора не издава
    вече излизане от роля (виж историята на комита с денилиста за
    подробности). Kiro издава себе си с фиксирани фрази, наблюдавани живо:
    'I'm Kiro, an AI-powered development environment...', 'I operate under
    my actual guidelines', 'I can't discuss that' - разпознаваме по тях."""
    return not _OUT_OF_CHARACTER.search(text)


class _HardTimeout(Exception):
    pass


def _post_with_hard_deadline(payload, deadline):
    """requests(timeout=...) вече не помогна веднъж - заявката увисна много
    по-дълго от подадения таймаут. Тук заявката тръгва в daemon нишка;
    ask() спира да я чака след deadline секунди, каквото и да прави тя после
    (заявката евентуално приключва сама на заден план и просто се изхвърля)."""
    box = {}

    def worker():
        try:
            box["r"] = requests.post(API_URL, json=payload, timeout=TIMEOUT)
        except Exception as exc:
            box["exc"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        raise _HardTimeout()
    if "exc" in box:
        raise box["exc"]
    return box["r"]
