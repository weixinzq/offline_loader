$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$output = Join-Path $PSScriptRoot "dist\AolaLoader"
$buildId = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $PSScriptRoot "build\wpf-publish\$buildId"
$publish = Join-Path $staging "app"
$work = Join-Path $staging "work"

$drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($PSScriptRoot))
if ($drive.AvailableFreeSpace -lt 512MB) {
    throw "构建磁盘空间不足：请至少预留 512 MB。正式程序文件未替换。"
}

python -m PyInstaller --noconfirm --clean --distpath $publish --workpath $work .\aola_backend.spec
if ($LASTEXITCODE -ne 0) {
    throw "Python backend build failed with exit code $LASTEXITCODE"
}

dotnet publish .\csharp\AolaLoader\AolaLoader.csproj `
    -c Release `
    -r win-x64 `
    --self-contained false `
    -o $publish
if ($LASTEXITCODE -ne 0) {
    throw "C# desktop build failed with exit code $LASTEXITCODE"
}

$verifyBackend = @'
import subprocess
import sys
from PyInstaller.archive.readers import CArchiveReader

archive = CArchiveReader(sys.argv[1])
for name in archive.toc:
    archive.extract(name)
pyz = archive.open_embedded_archive("PYZ.pyz")
for name in ("src.ipc.server", "scripts.auto_battle"):
    pyz.extract(name)
result = subprocess.run(
    [sys.argv[1], "--help"], timeout=30,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
if result.returncode:
    raise RuntimeError(f"Backend startup failed: exit code {result.returncode}")
print("Backend archive and startup verification passed.")
'@
$verifyBackend | python - (Join-Path $publish "AolaBackend.exe")
if ($LASTEXITCODE -ne 0) {
    throw "Backend verification failed; installed program files were not replaced."
}

Copy-Item -LiteralPath .\config.example.json -Destination (Join-Path $publish "config.example.json") -Force
if (@(Get-Process -Name AolaLoader,AolaBackend -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "加载器已启动，停止替换程序。已校验的新程序位于 $publish"
}

$backup = Join-Path $PSScriptRoot "dist\program-backup-$buildId"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
New-Item -ItemType Directory -Path $output -Force | Out-Null
$files = @(Get-ChildItem -LiteralPath $publish -File)
foreach ($file in $files) {
    $target = Join-Path $output $file.Name
    if (Test-Path -LiteralPath $target) {
        $saved = Join-Path $backup $file.Name
        Copy-Item -LiteralPath $target -Destination $saved
        if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $saved).Hash) {
            throw "Backup verification failed: $($file.Name)"
        }
    }
}
foreach ($file in $files) {
    $target = Join-Path $output $file.Name
    $pending = "$target.pending-$buildId"
    Copy-Item -LiteralPath $file.FullName -Destination $pending
    if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $pending).Hash) {
        throw "Copy verification failed: $($file.Name)"
    }
    if (Test-Path -LiteralPath $target) {
        [System.IO.File]::Replace($pending, $target, [NullString]::Value)
    } else {
        [System.IO.File]::Move($pending, $target)
    }
}

Write-Output "Previous program files preserved: $backup"
Write-Output "C# desktop build created: $output\AolaLoader.exe"

$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "build\wpf-publish"))
$resolvedStaging = [System.IO.Path]::GetFullPath($staging)
if (-not $resolvedStaging.StartsWith("$stagingRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝清理构建目录之外的路径：$resolvedStaging"
}
Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
