import os
import speech_recognition as sr

r = sr.Recognizer()

def record_text():
    while True:
        try:
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.2)
                print("listening...", flush=True)
                audio2 = r.listen(source)
                return r.recognize_google(audio2, language="en-US")

        except sr.RequestError as e:
            print("Could not request results; {0}".format(e))

        except sr.UnknownValueError:
            print("unknown error occured")

def run_command(text):
    t = text.lower()

    if "play music" in t or "open spotify" in t:
        os.startfile("spotify:")
        print(">> opening Spotify", flush=True)
        return True

    return False

def output_text(text):
    f = open("output.txt", "a", encoding="utf-8")
    f.write(text)
    f.write("\n")
    f.close()
    return

try:
    while True:
        text = record_text()
        output_text(text)
        print(text, flush=True)
        run_command(text)
except KeyboardInterrupt:
    print("\nstopped")
