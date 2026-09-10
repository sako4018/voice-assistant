# Voice Assistant — Jarvis

Гласов асистент за Windows. Казваш **"hey jarvis"** където и да си, по ъглите на
екрана светва тънка рамка, казваш какво искаш на английски — и той го прави.

Разбира свободна реч, не фиксирани фрази: "put on some music", "lock the computer",
"look up the weather" работят, без да са записани като команди никъде.

## Изисквания

Всичко върви с **Python 3.13** (`py -3.13`). Python 3.14 не компилира PyAudio.

```
py -3.13 -m pip install openwakeword onnxruntime SpeechRecognition PyAudio ^
    pyautogui pyperclip pyttsx3 requests numpy pystray Pillow
```

Мозъкът е [OmniRoute](https://www.npmjs.com/package/omniroute) на `localhost:20128`.
Jarvis го пуска сам, ако не върви.

## Пускане

```
py -3.13 jarvis.py
```

Три тона = готов. Кажи "hey jarvis" → един тон и рамката светва → казвай командата.

Може и на един дъх: **"hey jarvis, open spotify"**.

Иконата до часовника показва състоянието (синьо = чака, зелено = слуша).
Десен бутон → Quit. Или на глас: "shut down jarvis".

## Какво разбира

| казваш | прави |
|---|---|
| open youtube / open my email | отваря сайт |
| open notepad / open spotify | пуска програма |
| search for … / look up … | търси в Google |
| find … video on youtube | търси в YouTube |
| type … | пише го където е курсорът |
| pause / skip this song / turn it up / mute | медийни клавиши |
| lock the computer | заключва |
| put the pc to sleep | приспива |
| shut down / restart | изключва (20 сек за отказ) |
| close chrome | затваря програма |
| what is 17 times 23 | просто отговаря |

Опасни команди (`format c:`, `del /s`, `diskpart` …) се отказват, дори моделът да ги
предложи — микрофонът може да те чуе грешно.

## Автостарт

Копирай `jarvis_autostart.vbs` в `shell:startup`. Върви скрито, без конзола.
Логът е в `jarvis.log` до скрипта.

## Файлове

- `jarvis.py` — асистентът
- `speech_to_texy.py` — простата база: микрофон → текст в терминала
- `jarvis_autostart.vbs` — стартиране с Windows
