' Starts the OmniRoute brain server silently at Windows login.
' Jarvis can start it himself, but a cold start needs ~75 seconds - launching
' it here in parallel with Jarvis means it is already warm by the time you
' speak the first command.
' To enable: copy this file into shell:startup
' To disable: delete it from that Startup folder.
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c omniroute serve", 0, False
