' Parley windowless installer (iteration 26 A2).
' Double-click this to install Parley with NO console window at any point.
' It just launches the tk GUI installer (util\install_gui.py) under pythonw.
' If Python isn't installed yet, it points you at python.org.
Option Explicit
Dim sh, fso, scriptDir, gui
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
gui = scriptDir & "\util\install_gui.py"

On Error Resume Next
' Window style 1 (normal): pythonw has no console, so the tk window shows and
' no black box ever appears. False = don't block this launcher.
sh.Run "pythonw """ & gui & """", 1, False
If Err.Number <> 0 Then
    MsgBox "Could not start Python (pythonw.exe was not found)." & vbCrLf & vbCrLf & _
           "Install Python 3.11+ from python.org (tick 'Add python.exe to PATH')," & vbCrLf & _
           "then double-click this file again - or run scripts\util\install-windows.bat.", _
           48, "Install Parley"
End If
