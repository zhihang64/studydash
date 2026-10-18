# 一键打包：生成图标 → 打 exe → 报体积和校验和。
#
# 用法：
#   pwsh -File tools\build_exe.ps1                 # 默认同时出 onedir 和 onefile
#   pwsh -File tools\build_exe.ps1 -Mode onedir    # 只出文件夹版（启动最快、最不容易被误杀）
#   pwsh -File tools\build_exe.ps1 -Mode onefile   # 只出单文件版（好分发）
#
# 两个版本都发：onefile 方便下载，onedir 是「单文件版双击没反应」时的兜底
# （免去每次启动解包，且体积大、行为规矩，杀软误报明显更少）。
#
# 依赖： Python 3.10+、Pillow（只用于画图标）、PyInstaller
#       python -m pip install pillow pyinstaller
param(
  [ValidateSet('onedir', 'onefile', 'both')]
  [string]$Mode = 'both'
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 版本号单一来源：src/studydash/__init__.py。绝不在这里手写。
$version = (Select-String -Path "$root\src\studydash\__init__.py" `
            -Pattern '^__version__\s*=\s*"([^"]+)"' |
            Select-Object -First 1).Matches.Groups[1].Value
if (-not $version) { throw 'src\studydash\__init__.py 里找不到 __version__' }
Write-Host "版本：$version"

# 版本资源由模板渲染，保证和 __version__ 永远一致
New-Item -ItemType Directory -Force -Path "$root\build" | Out-Null
$v4 = '2,0,0,0'
if ($version -match '^(\d+)\.(\d+)\.(\d+)') { $v4 = "$($Matches[1]),$($Matches[2]),$($Matches[3]),0" }
(Get-Content "$root\tools\version_info.tmpl.txt" -Raw) `
  -replace '@VERSION4@', $v4 -replace '@VERSION@', "$version.0" |
  Set-Content "$root\build\version_info.txt" -Encoding utf8

# 1) 图标（纯代码绘制，可重复生成）
Write-Host "`n[1/3] 生成图标…"
python tools\make_icon.py

# 2) 打包
#    注意：给了 --specpath 之后，--icon/--add-data/--version-file 都会相对 spec 所在目录
#    解析，所以这里一律传绝对路径，否则会报 "Unable to find ...build\templates"。
$common = @(
  '--noconfirm', '--clean',
  '--name', 'studydash',
  '--icon', "$root\assets\icon.ico",
  '--version-file', "$root\build\version_info.txt",
  '--paths', "$root\src",
  '--add-data', "$root\src\studydash\templates;templates",
  '--exclude-module', 'tkinter', '--exclude-module', 'PIL', '--exclude-module', 'numpy',
  '--exclude-module', 'pandas', '--exclude-module', 'matplotlib', '--exclude-module', 'unittest',
  '--exclude-module', 'pydoc', '--exclude-module', 'doctest', '--exclude-module', 'distutils',
  '--exclude-module', 'setuptools', '--exclude-module', 'pip', '--exclude-module', 'tornado',
  '--distpath', "$root\dist", '--workpath', "$root\build\work", '--specpath', "$root\build"
)

function Build([string]$flavour, [string[]]$extra) {
  Write-Host "`n[2/3] PyInstaller 打包（$flavour）…"
  $args = @('-m', 'PyInstaller') + $extra + $common + @("$root\tools\entry.py")
  & python @args
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（$flavour），退出码 $LASTEXITCODE" }
}

if ($Mode -eq 'onedir' -or $Mode -eq 'both') { Build 'onedir' @() }
if ($Mode -eq 'onefile' -or $Mode -eq 'both') { Build 'onefile' @('--onefile') }

# 3) 结果
Write-Host "`n[3/3] 产物："
$single = Join-Path $root 'dist\studydash.exe'
$folder = Join-Path $root 'dist\studydash\studydash.exe'
foreach ($exe in @($folder, $single)) {
  if (Test-Path $exe) {
    $item = Get-Item $exe
    Write-Host ("  {0}  {1} MB" -f $item.FullName.Replace("$root\", ''), [math]::Round($item.Length / 1MB, 2))
    Write-Host ("    SHA256 {0}" -f (Get-FileHash $exe -Algorithm SHA256).Hash)
    Write-Host ("    版本资源 {0}" -f $item.VersionInfo.FileVersion)
  }
}
if (-not (Test-Path $folder) -and -not (Test-Path $single)) { throw '没生成任何 exe' }
