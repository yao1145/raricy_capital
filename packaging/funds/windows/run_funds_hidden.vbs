' 隐藏启动 Raricy 双基金资金服务（不弹出控制台窗口）。
'
' 由 Windows 计划任务调用：wscript.exe run_funds_hidden.vbs [config路径]
' 优先用 venv 的 pythonw.exe（无控制台），退回 python.exe 并以隐藏窗口运行。
' 需要包已安装（pip install -e . 或 pip install .），且解释器为 Python 3.12+。
' 例：仓库根为 D:\Study\Code\raricy_capital（安装脚本默认也按此结构推断）。
Option Explicit

Dim shell, fso, scriptDir, repoRoot, python, config, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 脚本位于 <仓库根>\packaging\funds\windows\，仓库根在上溯三级。
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(scriptDir)))

python = repoRoot & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(python) Then
    python = repoRoot & "\.venv\Scripts\python.exe"
End If
If Not fso.FileExists(python) Then
    python = "python.exe"
End If

config = ""
If WScript.Arguments.Count >= 1 Then
    config = WScript.Arguments(0)
End If

command = """" & python & """ -m raricy_capital"
If Len(config) > 0 Then
    command = command & " --config """ & config & """"
End If

shell.CurrentDirectory = repoRoot
' 等待子进程并传回退出码，计划任务才能在 Python 故障时自动重启。
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
