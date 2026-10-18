# 一键打包：生成图标 → 打 exe → 报体积和校验和。
# 用法： pwsh -File tools\build_exe.ps1
# 依赖： Python 3.10+、Pillow（只用于画图标）、PyInstaller
#       python -m pip install pillow pyinstaller
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$name    = '学习进度看板'
$version = (Select-String -Path "$root\dashboard.py" -Pattern '^__version__\s*=\s*"([^"]+)"' |
            Select-Object -First 1).Matches.Groups[1].Value
if (-not $version) { throw 'dashboard.py 里找不到 __version__' }
Write-Host "版本：$version"

# 1) 图标（纯代码绘制，可重复生成）
Write-Host "`n[1/3] 生成图标…"
python tools\make_icon.py

# 2) 打包
#    注意：给了 --specpath 之后，--icon/--add-data/--version-file 都会相对 spec 所在目录解析，
#    所以这里一律传绝对路径，否则会报 "Unable to find ...build\templates"。
Write-Host "`n[2/3] PyInstaller 打包…"
python -m PyInstaller --noconfirm --clean --onefile `
  --name "$name" `
  --icon "$root\assets\icon.ico" `
  --version-file "$root\tools\version_info.txt" `
  --add-data "$root\templates;templates" `
  --exclude-module tkinter --exclude-module PIL --exclude-module numpy `
  --exclude-module pandas --exclude-module matplotlib --exclude-module unittest `
  --exclude-module pydoc --exclude-module doctest --exclude-module distutils `
  --exclude-module setuptools --exclude-module pip --exclude-module tornado `
  --distpath "$root\dist" --workpath "$root\build\work" --specpath "$root\build" `
  "$root\dashboard.py"

# 3) 结果
Write-Host "`n[3/3] 产物："
$exe = Join-Path $root "dist\$name.exe"
if (-not (Test-Path $exe)) { throw "没生成 $exe" }
$item = Get-Item $exe
Write-Host ("  {0}  {1} MB" -f $item.Name, [math]::Round($item.Length / 1MB, 2))
Write-Host ("  SHA256 {0}" -f (Get-FileHash $exe -Algorithm SHA256).Hash)
Write-Host ("  版本资源 {0}" -f $item.VersionInfo.FileVersion)
