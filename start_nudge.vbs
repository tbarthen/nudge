Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Determine the Nudge directory (wherever this script lives)
NudgeDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' Safety check: make sure launcher.py exists before trying to run it
If Not FSO.FileExists(NudgeDir & "\launcher.py") Then
    WScript.Quit
End If

' Try pythonw first (no console window), fall back to python
WshShell.CurrentDirectory = NudgeDir
On Error Resume Next
WshShell.Run "pythonw launcher.py", 0, False
If Err.Number <> 0 Then
    Err.Clear
    WshShell.Run "python launcher.py", 0, False
End If
On Error GoTo 0
