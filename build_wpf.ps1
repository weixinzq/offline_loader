$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$output = Join-Path $PSScriptRoot "dist\AolaLoader"

python -m PyInstaller --noconfirm --clean --distpath $output .\aola_backend.spec
if ($LASTEXITCODE -ne 0) {
    throw "Python backend build failed with exit code $LASTEXITCODE"
}

dotnet publish .\csharp\AolaLoader\AolaLoader.csproj `
    -c Release `
    -r win-x64 `
    --self-contained false `
    -o $output
if ($LASTEXITCODE -ne 0) {
    throw "C# desktop build failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath .\config.example.json -Destination (Join-Path $output "config.example.json") -Force
Remove-Item -LiteralPath (Join-Path $output "AolaLoader.pdb") -Force -ErrorAction SilentlyContinue

Write-Output "C# desktop build created: $output\AolaLoader.exe"
