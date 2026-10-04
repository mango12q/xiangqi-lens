' XiangQiLens 无窗口启动器
' 用 Chr(34) 构造引号，避免 VBScript 里成串引号转义出错

Option Explicit

Dim fso, shell, base, pyw, script, q, cmd, rc

q = Chr(34)

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base   = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(base, "app_vision.py")
pyw    = "C:\Users\mango\AppData\Local\Programs\Python\Python313\pythonw.exe"

If Not fso.FileExists(script) Then
    MsgBox "找不到主程序: " & script, vbCritical, "XiangQiLens"
    WScript.Quit 1
End If

If Not fso.FileExists(pyw) Then
    MsgBox "找不到 pythonw.exe: " & pyw, vbCritical, "XiangQiLens"
    WScript.Quit 1
End If

cmd = q & pyw & q & " " & q & script & q
rc = shell.Run(cmd, 1, False)

If rc = 0 Then
    WScript.Quit 0
Else
    MsgBox "启动失败, 退出码 " & rc & vbCrLf & cmd, vbCritical, "XiangQiLens"
    WScript.Quit rc
End If
