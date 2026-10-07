Set-Location -LiteralPath $PSScriptRoot
& 'D:\Programs\python\Python312\python.exe' -u -m Com.load_second 2>&1 |
    Out-File -LiteralPath (Join-Path $PSScriptRoot 'scheduled_0557.log') -Append -Encoding Unicode
exit $LASTEXITCODE
