' Starts Jarvis silently at Windows login (no console window).
' To enable: copy this file into shell:startup
'   (C:\Users\Sarkis\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup)
' To disable: delete it from that Startup folder.
Set sh = CreateObject("WScript.Shell")
pyw = "C:\Users\Sarkis\AppData\Local\Programs\Python\Python313\pythonw.exe"
script = "C:\Users\Sarkis\Desktop\VS code\speech to text\jarvis.py"
sh.Run """" & pyw & """ """ & script & """", 0, False
