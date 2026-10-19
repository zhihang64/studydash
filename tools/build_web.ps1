# 构建网页版前端：装依赖 → vite build → 产物落进 Python 包的静态目录。
#
# 用法：
#   pwsh -File tools\build_web.ps1               # 正常构建
#   pwsh -File tools\build_web.ps1 -SkipInstall  # 依赖没变时跳过 npm install
#
# 产物必须落进 src/studydash/web/static/，因为后端就是这样托管它的
# （web/app.py 的 AppState.static_path()），而 wheel / PyInstaller 打包都从
# 那里取。所以这个脚本是「前端接进 Python 包」的唯一入口，别手动拷。
#
# 依赖： Node.js 18+ 与 npm。
param([switch]$SkipInstall)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root 'web'
$dest = Join-Path $root 'src\studydash\web\static'

if (-not (Test-Path (Join-Path $web 'package.json'))) {
  throw "找不到 $web\package.json —— 这个脚本要在仓库里跑"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw 'PATH 里没有 npm。装 Node.js 18+ 再来：https://nodejs.org/'
}

Set-Location $web

if (-not $SkipInstall) {
  Write-Host '[1/3] 安装前端依赖…'
  # 有 lock 就用 ci（保证可复现，且不会偷偷改 package.json）；没有才 install。
  if (Test-Path (Join-Path $web 'package-lock.json')) { & npm ci } else { & npm install }
  if ($LASTEXITCODE -ne 0) { throw "npm 装依赖失败，退出码 $LASTEXITCODE" }
} else {
  Write-Host '[1/3] 跳过装依赖（-SkipInstall）'
}

Write-Host "`n[2/3] vite build…"
& npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码 $LASTEXITCODE" }

Write-Host "`n[3/3] 产物："
if (-not (Test-Path $dest)) { throw "构建完了却没看到 $dest" }
$files = Get-ChildItem -Recurse -File $dest
$total = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host ("  {0}  {1} 个文件  {2} KB" -f $dest.Replace("$root\", ''), $files.Count, [math]::Round($total / 1KB, 1))

$index = Join-Path $dest 'index.html'
if (-not (Test-Path $index)) { throw '没生成 index.html —— 后端托管的就是它' }

# 注入点还在不在：后端要往 window.__STUDYDASH__ 里塞令牌，占位符丢了整个站就废
$boot = Get-Content $index -Raw
if ($boot -notmatch 'window\.__STUDYDASH__') {
  throw 'index.html 里没有 window.__STUDYDASH__ 注入点 —— 检查 web/index.html'
}
Write-Host '  ✓ index.html 带 window.__STUDYDASH__ 注入点'
