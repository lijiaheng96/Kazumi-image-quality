Option Explicit
Dim shell, fso, root, python
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
python = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(python) Then
  MsgBox "Please run setup.ps1 first.", 48, "Kazumi Quality Helper"
  WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & python & Chr(34) & " -X utf8 " & Chr(34) & root & "\launcher.py" & Chr(34), 0, False
