Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
studioPy = scriptDir & "\studio.py"

' Free the port if a previous run is still hanging around, so double-clicking
' the icon again just works instead of erroring on "address in use".
WshShell.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -ano ^| findstr :5056 ^| findstr LISTENING') do taskkill /PID %a /F >nul 2>&1", 0, True

' Launch the server with no visible console window.
WshShell.Run "cmd /c python """ & studioPy & """", 0, False

' Give it a moment to bind the port, then open the browser.
WScript.Sleep 1800
WshShell.Run "http://localhost:5056"
