Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\info-hub"
Set fs = CreateObject("Scripting.FileSystemObject")
If Not fs.FileExists("D:\info-hub\data\PAUSED") Then
  sh.Run """D:\info-hub\.venv\Scripts\python.exe"" ""D:\info-hub\infohub.py""", 0, False
End If
