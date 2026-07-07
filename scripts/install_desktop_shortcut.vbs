Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
desktop = WshShell.SpecialFolders("Desktop")

Set Shortcut = WshShell.CreateShortcut(desktop & "\Video Studio.lnk")
Shortcut.TargetPath = "wscript.exe"
Shortcut.Arguments = """" & scriptDir & "\launch_studio_silent.vbs"""
Shortcut.WorkingDirectory = scriptDir
Shortcut.IconLocation = "%SystemRoot%\System32\shell32.dll,137"
Shortcut.Description = "Launch Video Studio (http://localhost:5056)"
Shortcut.Save

MsgBox "Desktop shortcut created: ""Video Studio"". Double-click it to launch the app.", 64, "Video Studio Setup"
