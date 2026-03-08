Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\DEV\nudge"
WshShell.Run "pythonw launcher.py", 0, False
