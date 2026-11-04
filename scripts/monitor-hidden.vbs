Option Explicit

Dim shell, scriptPath, configPath, command, exitCode

If WScript.Arguments.Count <> 2 Then
    WScript.Quit 2
End If

scriptPath = WScript.Arguments(0)
configPath = WScript.Arguments(1)
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & QuotePath(scriptPath) & " -ConfigPath " & QuotePath(configPath)

Set shell = CreateObject("WScript.Shell")
exitCode = shell.Run(command, 0, True)
Set shell = Nothing
WScript.Quit exitCode

Function QuotePath(ByVal value)
    QuotePath = """" & Replace(value, """", """""") & """"
End Function
