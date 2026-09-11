"""Мозъкът: изпраща чутото на OmniRoute (локален безплатен AI) и връща
(какво да каже, какво да направи).

Всяка команда минава оттук - няма фиксирани фрази никъде в кода. Моделът връща
едно изречение на български и по избор последен ред:

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
    "Ти си Jarvis - гласов асистент на Windows компютъра на Саркис. "
    "Отговаряй на БЪЛГАРСКИ, с ЕДНО кратко изречение. Без markdown, без списъци.\n"
    "Ако потребителят иска нещо да се направи на компютъра, добави го на ПОСЛЕДЕН "
    "нов ред точно в този формат:\n"
    "[ACTION] <вид> <цел>\n"
    "Видовете са само тези (пиши ги на английски, точно както са):\n"
    "  open_url <адрес>          напр. [ACTION] open_url https://youtube.com\n"
    "  open_app <име>            напр. [ACTION] open_app notepad\n"
    "  search <заявка>           търсене в Google - за 'потърси', 'намери', 'гугъл'\n"
    "  search_youtube <заявка>   търсене в YouTube - само ако спомене YouTube, клип, видео\n"
    "  type <текст>              пише текст там, където е курсорът; копирай думите на "
    "потребителя ДОСЛОВНО. Ако изречението започне с 'напиши'/'пиши'/'въведи', "
    "ВИНАГИ е тази команда - каквото следва е текст за писане, НЕ разговор с тебе, "
    "дори да прилича на поздрав или въпрос.\n"
    "  media <play|pause|next|prev|volup|voldown|mute>\n"
    "  lock                      заключва екрана\n"
    "  sleep                     приспива компютъра\n"
    "  shutdown | restart        изключва/рестартира (20 сек за отказ)\n"
    "  cancel_shutdown           отказва изключването\n"
    "  kill <име>                затваря програма\n"
    "  run <команда за cmd>      за всичко останало\n"
    "Примери: 'пусни музика' -> [ACTION] media play | 'спри музиката' -> "
    "[ACTION] media pause | 'усили' -> [ACTION] media volup | 'следващата песен' -> "
    "[ACTION] media next | 'заключи компютъра' -> [ACTION] lock | "
    "'отвори ми пощата' -> [ACTION] open_url https://mail.google.com | "
    "'затвори хрома' -> [ACTION] kill chrome.\n"
    "Ако не трябва действие (въпрос, сметка, разговор) - НЕ пиши ред [ACTION], "
    "просто отговори."
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

        Връща текста на отговора. Никога не хвърля - при проблем връща
        изречение на български, което после се показва и изговаря.
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
            try:
                r = requests.post(API_URL, json=payload, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"].strip()
                log("мозъкът не върна отговор (%s): %s" % (model, str(data)[:200]))
            except requests.RequestException as exc:
                log("грешка при мозъка (%s): %s" % (model, exc))
        return "Мозъкът не отговаря в момента."
