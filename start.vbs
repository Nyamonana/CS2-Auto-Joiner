Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = Fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "pythonw app.py", 0, False
