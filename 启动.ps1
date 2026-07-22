<#
  一键启动「Jenkins 发版核对」网页版。
  第一次用：先在 https://<你的Jenkins>/me/security/ 生成 API Token，
  然后设一次用户级环境变量（只需一次，之后开机都在）：
      [Environment]::SetEnvironmentVariable('JENKINS_URL','https://你的jenkins','User')
      [Environment]::SetEnvironmentVariable('JENKINS_USER','你的登录名','User')
      [Environment]::SetEnvironmentVariable('JENKINS_API_TOKEN','你的token','User')
  注意：这样 Token 会明文存在注册表里。不想留痕就每次临时设 $env: 变量。

  也可以在仓库根目录放一个 config.ps1（已被 .gitignore 忽略），内容例如：
      $env:JENKINS_URL='https://你的jenkins'
      $env:JENKINS_USER='你的登录名'
      $env:JENKINS_API_TOKEN='你的token'
#>
param([int]$Port = 8770)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 本地私有配置（不入库），有就先加载
$cfg = Join-Path $here 'config.ps1'
if (Test-Path $cfg) { . $cfg }

if (-not $env:JENKINS_URL)       { $env:JENKINS_URL       = Read-Host 'Jenkins 地址 (如 https://jenkins.example.com)' }
if (-not $env:JENKINS_USER)      { $env:JENKINS_USER      = Read-Host 'Jenkins 登录名' }
if (-not $env:JENKINS_API_TOKEN) { $env:JENKINS_API_TOKEN = Read-Host 'Jenkins API Token' }
$env:JENKINS_WEB_PORT = $Port

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw '没找到 python，请先安装 Python 3' }

Start-Process "http://127.0.0.1:$Port"
& $py (Join-Path $here 'app.py')
