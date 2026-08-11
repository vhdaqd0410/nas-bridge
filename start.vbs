' NAS Bridge Desktop launcher — NO black window
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

strThisDir = fso.GetParentFolderName(WScript.ScriptFullName)

' ---- 找桌面版入口 ----
strDesktopPy = fso.BuildPath(strThisDir, "desktop.py")
strAppPy = fso.BuildPath(strThisDir, "app.py")

If fso.FileExists(strDesktopPy) Then
    strTarget = strDesktopPy
ElseIf fso.FileExists(strAppPy) Then
    strTarget = strAppPy
Else
    WshShell.Popup "找不到入口文件 desktop.py 或 app.py", 5, "NAS Bridge", 16
    WScript.Quit 1
End If

' ---- 找 python.exe ----
arrPreferred = Array( _
    "C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe", _
    "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe", _
    "C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe", _
    "C:\Users\Admin\AppData\Local\Programs\Python\Python310\python.exe" _
)
strPyExe = ""
For Each strExe In arrPreferred
    If fso.FileExists(strExe) Then
        strPyExe = strExe
        Exit For
    End If
Next
If strPyExe = "" Then strPyExe = "python.exe"

' ---- 检查是否已在运行 ----
On Error Resume Next
Set objHttp = CreateObject("MSXML2.XMLHTTP")
objHttp.Open "GET", "http://127.0.0.1:8089/api/projects", False
objHttp.Send
If Err.Number = 0 And objHttp.Status = 200 Then
    On Error GoTo 0
    Set objHttp = Nothing
    WshShell.Popup "NAS Bridge 已在运行" & vbCrLf & "如果桌面窗口没显示，请打开 http://127.0.0.1:8089", 4, "NAS Bridge", 64
    WScript.Quit 0
End If
On Error GoTo 0
Set objHttp = Nothing

' ---- LAUNCH ----
WshShell.CurrentDirectory = strThisDir
' python.exe + 隐藏窗口 (0, False) => 无黑窗，桌面版由 pywebview 自带原生窗口
WshShell.Run Chr(34) & strPyExe & Chr(34) & " " & Chr(34) & strTarget & Chr(34), 0, False
