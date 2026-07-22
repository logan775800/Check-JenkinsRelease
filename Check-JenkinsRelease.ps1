<#
.SYNOPSIS
    Jenkins 发版核对 —— 一次性扫描所有视图 / 所有 job，找出漏发、失败、停在旧代码的任务。

.DESCRIPTION
    走 Jenkins REST API，只读，不会触发任何构建。

    核心判法是「SHA 基准法」而不是比对 tag 字符串：
    像 master / uat 这类浮动分支，参数值永远是 "master"，看它等于没看；
    只有比对 Git 插件记录的实际 checkout SHA1，才能真正回答「我这次发全了没」。

    状态含义：
        OK     SHA 与基准一致           —— 已跟上本次发版
        STALE  SHA 落后于基准           —— 大概率漏发
        FAIL   失败 / 不稳定 / 被中止
        RUN    正在构建

    两个必须做的归一化（否则会产生几百条假告警）：
      1. Git 插件把分支记成 refs/remotes/origin/xxx，参数里写的是 xxx，要先剥前缀再比。
      2. 参数名不统一：Api/Job/Web-Agent 类用 TAG，Pages 类用 BRANCH_NAME，两个都要试。

.PARAMETER Branch
    本次发版的分支 / tag，如 masterBranch/main-3.04、master_V3.08_003。
    留空则等同 -ListBranches，先列出分支和 SHA 分布供你挑。

.PARAMETER BaseSha
    基准 SHA（7 位短 SHA）。留空 = 自动取该分支下最新一次构建的 SHA。

.PARAMETER IgnoreJob
    忽略清单，支持通配符。用于排除本来就不跟版的 job，例如 -IgnoreJob 'RebateJob-*'

.EXAMPLE
    # 第一步：看有哪些分支、各自 SHA 散成几拨
    $env:JENKINS_USER='logan'; $env:JENKINS_API_TOKEN='xxxx'
    .\Check-JenkinsRelease.ps1 -ListBranches

.EXAMPLE
    # 第二步：核对某次发版，只看有问题的
    .\Check-JenkinsRelease.ps1 -Branch 'masterBranch/main-3.04' -OnlyProblem

.EXAMPLE
    # 缩到指定视图 + 导出 CSV
    .\Check-JenkinsRelease.ps1 -Branch 'master_V3.08_003' -View 'LotteryApi','Web-Pages' -CsvPath .\release.csv

.NOTES
    API Token 生成：https://your-jenkins-host/me/security/ -> API Token -> 添加新 Token
    Token 只显示一次，当场复制。认证用户名填 Jenkins 登录名，不是邮箱。

    首次运行若报「禁止运行脚本」，先执行：Set-ExecutionPolicy -Scope Process Bypass
#>
[CmdletBinding()]
param(
    [string]   $JenkinsUrl = $(if ($env:JENKINS_URL) { $env:JENKINS_URL } else { 'https://your-jenkins-host' }),
    [string]   $User       = $env:JENKINS_USER,
    [string]   $ApiToken   = $env:JENKINS_API_TOKEN,
    [string]   $Branch,
    [string]   $BaseSha,
    [string[]] $View       = @(),
    [string[]] $IgnoreJob  = @(),
    [string[]] $Site       = @(),
    [string[]] $ExcludeSite = @(),
    [string[]] $Component  = @(),
    [string[]] $Job        = @(),
    [string]   $Manifest,
    [string]   $SiteFile,
    [switch]   $FromClipboard,
    [switch]   $ParseOnly,
    [switch]   $ListBranches,
    [switch]   $History,
    [datetime] $Date       = [datetime]::Today,
    [int]      $Days       = 1,
    [int]      $MaxPerJob  = 30,
    [switch]   $OnlyProblem,
    [string]   $CsvPath,
    [switch]   $SkipCertCheck
)

$ErrorActionPreference = 'Stop'
# 叠加而不是覆盖：直接赋 Tls12 会把系统默认里的 TLS 1.3 抹掉，导致「未能创建 SSL/TLS 安全通道」
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if ([enum]::GetNames([Net.SecurityProtocolType]) -contains 'Tls13') {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls13
}

if ($SkipCertCheck) {
    Add-Type @"
using System.Net; using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
    [Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy
}

if (-not $User -or -not $ApiToken) {
    throw "缺认证。先执行：`$env:JENKINS_USER='登录名'; `$env:JENKINS_API_TOKEN='APIToken'"
}

$base    = $JenkinsUrl.TrimEnd('/')
$headers = @{ Authorization = 'Basic ' + [Convert]::ToBase64String(
                 [Text.Encoding]::ASCII.GetBytes("${User}:${ApiToken}")) }

function Get-Json([string]$Url) {
    # PS5.1 的 Invoke-RestMethod 有时按 ISO-8859-1 解码，中文视图名会乱码，所以自己按 UTF-8 解字节
    $r = Invoke-WebRequest -Uri $Url -Headers $headers -UseBasicParsing -TimeoutSec 120
    [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
}

function Normalize-Ref([string]$s) {
    if (-not $s) { return '' }
    $s -replace '^refs/remotes/', '' -replace '^refs/heads/', '' -replace '^origin/', ''
}

# 终端里中文占 2 个显示宽度，但 .Length 只算 1，用 "{0,-44}" 这类格式化会错位。
# job 名和状态标记都含中文，所以统一走下面这两个函数补空格。
function Get-DisplayWidth([string]$s) {
    if (-not $s) { return 0 }
    $w = 0
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        if ($c -ge 0x1100 -and (
              $c -le 0x115F -or
             ($c -ge 0x2E80 -and $c -le 0xA4CF) -or   # CJK 部首、汉字、注音
             ($c -ge 0xAC00 -and $c -le 0xD7A3) -or   # 韩文
             ($c -ge 0xF900 -and $c -le 0xFAFF) -or   # CJK 兼容
             ($c -ge 0xFE30 -and $c -le 0xFE6F) -or   # CJK 标点
             ($c -ge 0xFF00 -and $c -le 0xFF60) -or   # 全角
             ($c -ge 0xFFE0 -and $c -le 0xFFE6))) { $w += 2 } else { $w += 1 }
    }
    $w
}
function Pad([string]$s, [int]$n) { "$s" + (' ' * [Math]::Max(0, $n - (Get-DisplayWidth $s))) }

# job 命名规约：AR{编号}-{组件}-{国家}-{站点名}，组件共 8 种
# （Pages / LotteryApi / AgentApi / Web / WebExtendApi / WebIntranetApi / ThirdApi / ThirdJob）
# 发布单上写的「web」指的是前端，对应组件 Pages。
# 从任意文本里抠 AR 编号 —— 发布单原文直接扔进来就行，不用手敲。
# 兼容 AR51 / ar051 / AR0051 这类写法，统一补成 3 位大写（AR051）。
function Get-SitesFromText([string]$Text) {
    if (-not $Text) { return @() }
    $found = [regex]::Matches($Text, '(?i)\bAR[-_ ]?0*(\d{1,4})\b') |
             ForEach-Object { 'AR' + $_.Groups[1].Value.PadLeft(3, '0') }
    @($found | Select-Object -Unique | Sort-Object)
}

function Get-SiteCode([string]$JobName) {
    if ($JobName -match '^(AR\d+)-') { $Matches[1] } else { '' }
}
function Get-Component([string]$JobName) {
    if ($JobName -match '^AR\d+-([^-]+)-') { $Matches[1] } else { '' }
}

# 各模式共用的 job 过滤：-IgnoreJob / -Site / -Component / -Job
function Test-JobWanted([string]$JobName) {
    if ($IgnoreJob.Count -and ($IgnoreJob | Where-Object { $JobName -like $_ })) { return $false }
    if ($Job.Count       -and -not ($Job       | Where-Object { $JobName -like $_ })) { return $false }
    if ($Site.Count      -and -not ($Site      | Where-Object { (Get-SiteCode  $JobName) -eq $_ })) { return $false }
    if ($Component.Count -and -not ($Component | Where-Object { (Get-Component $JobName) -eq $_ })) { return $false }
    $true
}

# 从一次构建的 actions 里挖出：版本参数、实际 checkout 分支/SHA、触发人、全部参数
function Read-BuildInfo($b) {
    $ps = @()
    foreach ($a in @($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'parameters' -and $_.parameters })) { $ps += $a.parameters }
    # 参数名不统一：Api/Job/Web 类叫 TAG，Pages 类叫 BRANCH_NAME，两个都试
    $wanted = ($ps | Where-Object { $_.name -in 'TAG', 'BRANCH_NAME' } | Select-Object -First 1).value
    $rev = @($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'lastBuiltRevision' -and $_.lastBuiltRevision } |
             Select-Object -First 1).lastBuiltRevision
    $sha = ''; $revName = ''
    if ($rev) {
        $revName = ($rev.branch | Select-Object -First 1).name
        if ($rev.SHA1) { $sha = $rev.SHA1.Substring(0, 7) }
    }
    $cause = (@($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'causes' -and $_.causes } |
                Select-Object -First 1).causes | Select-Object -First 1)
    [pscustomobject]@{
        Time   = [datetimeoffset]::FromUnixTimeMilliseconds([int64]$b.timestamp).LocalDateTime
        Num    = $b.number
        Result = $(if ($b.building) { 'BUILDING' } else { $b.result })
        Dur    = [math]::Round($b.duration / 1000)
        Want   = (Normalize-Ref $wanted)
        Got    = (Normalize-Ref $revName)
        Sha    = $sha
        Who    = $(if ($cause.userName) { $cause.userName } else { $cause.shortDescription })
        Params = (($ps | ForEach-Object { "$($_.name)=$($_.value)" }) -join '; ')
        Url    = $b.url
    }
}

# 一次 tree 查询拿全：构建结果、时间、所有参数、Git 实际 revision、触发人
$jobTree = 'jobs[name,url,lastBuild[number,result,building,timestamp,duration,url,actions[' +
           'parameters[name,value],lastBuiltRevision[SHA1,branch[name,SHA1]],' +
           'causes[userName,shortDescription]]]]'

# ---------------------------------------------------------------- 采集
# 注意：PowerShell 变量名不区分大小写，这里不能叫 $views —— 会和参数 $View 撞成同一个变量
$targetViews = @((Get-Json "$base/api/json?tree=views[name,url]").views)
if ($View.Count) { $targetViews = @($targetViews | Where-Object { $View -contains $_.name }) }
if (-not $targetViews) {
    throw "没匹配到视图。可用：$(((Get-Json "$base/api/json?tree=views[name]").views.name) -join ', ')"
}

# ---------------------------------------------------------------- 模式四：按发布单核对
# 输入一份发布单 JSON（站点清单 + 组件->期望tag），输出「站点 × 组件」核对矩阵。
# 这是发版收尾真正要用的模式：直接回答「发布单上每一格发了没、tag 对不对、成没成功」。
if ($Manifest) {
    if (-not (Test-Path $Manifest)) { throw "发布单不存在：$Manifest" }
    $mf = Get-Content -Path $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json

    # -Date 显式传了就以命令行为准，否则用发布单里的 date
    if (-not $PSBoundParameters.ContainsKey('Date') -and $mf.date) { $Date = [datetime]$mf.date }
    if (-not $PSBoundParameters.ContainsKey('Days') -and $mf.days) { $Days = [int]$mf.days }
    $from = $Date.Date; $to = $from.AddDays($Days)

    $comps = @($mf.components.PSObject.Properties | ForEach-Object { [pscustomobject]@{ Name = $_.Name; Expect = $_.Value } })
    if (-not $comps) { throw "发布单里 components 为空" }

    # 一次性把全部 job 的构建历史拉下来（顶层 jobs，不按视图，避免跨视图重复）
    $bTree = 'jobs[name,url,builds[number,result,building,timestamp,duration,url,actions[' +
             'parameters[name,value],lastBuiltRevision[SHA1,branch[name,SHA1]],' +
             "causes[userName,shortDescription]]]{0,$MaxPerJob}]"
    $allJobs = @((Get-Json "$base/api/json?tree=$bTree").jobs)

    # ---- 站点清单来源，优先级从高到低。核心思路：能不手敲就不手敲 ----
    #   -FromClipboard  直接复制发布单再跑，正则抠 AR 编号
    #   -Site           命令行显式指定
    #   -SiteFile       发布单原文存成 txt
    #   发布单 sitesFile 字段（相对发布单所在目录）
    #   发布单 sites = "*" / "all"  ->  自动取「所有拥有这些组件的站点」，全站发版不用列清单
    #   发布单 sites = [...]        ->  手填清单（最后兜底）
    $srcTxt = ''
    if     ($FromClipboard) { $sites = Get-SitesFromText ((Get-Clipboard -Raw) -join "`n"); $srcTxt = '剪贴板' }
    elseif ($Site.Count)    { $sites = @($Site); $srcTxt = '命令行 -Site' }
    elseif ($SiteFile)      {
        if (-not (Test-Path $SiteFile)) { throw "站点清单文件不存在：$SiteFile" }
        $sites = Get-SitesFromText (Get-Content -Path $SiteFile -Raw -Encoding UTF8); $srcTxt = "文件 $(Split-Path $SiteFile -Leaf)"
    }
    elseif ($mf.sitesFile)  {
        $sf = if ([IO.Path]::IsPathRooted($mf.sitesFile)) { $mf.sitesFile } else { Join-Path (Split-Path $Manifest -Parent) $mf.sitesFile }
        if (-not (Test-Path $sf)) { throw "发布单 sitesFile 指向的文件不存在：$sf" }
        $sites = Get-SitesFromText (Get-Content -Path $sf -Raw -Encoding UTF8); $srcTxt = "发布单 sitesFile ($(Split-Path $sf -Leaf))"
    }
    elseif (@($mf.sites) -contains '*' -or @($mf.sites) -contains 'all') {
        # 全量：凡是拥有发布单里任一组件的站点，都纳入核对
        $sites = @($allJobs.name | ForEach-Object {
                      $sc = Get-SiteCode $_; $cp = Get-Component $_
                      if ($sc -and ($comps.Name -contains $cp)) { $sc } } |
                  Select-Object -Unique | Sort-Object)
        $srcTxt = '全量（所有拥有这些组件的站点）'
    }
    else { $sites = @($mf.sites); $srcTxt = '发布单 sites 清单' }

    $exclude = @($ExcludeSite) + @($mf.excludeSites) | Where-Object { $_ }
    if ($exclude) { $sites = @($sites | Where-Object { $exclude -notcontains $_ }) }
    if (-not $sites) { throw "站点清单为空（来源：$srcTxt）" }

    # 抠出来的编号里可能有 Jenkins 上根本不存在的（发布单笔误、或该站点还没建 job）
    $known   = @($allJobs.name | ForEach-Object { Get-SiteCode $_ } | Where-Object { $_ } | Select-Object -Unique)
    $unknown = @($sites | Where-Object { $known -notcontains $_ })

    if ($ParseOnly) {
        Write-Host "站点来源：$srcTxt" -ForegroundColor White
        Write-Host "共解析出 $($sites.Count) 个站点：" -ForegroundColor White
        Write-Host ("  " + ($sites -join ', '))
        if ($exclude) { Write-Host "已排除：$($exclude -join ', ')" -ForegroundColor DarkGray }
        if ($unknown) { Write-Host "`nJenkins 上不存在的编号（请核对发布单）：$($unknown -join ', ')" -ForegroundColor Yellow }
        return
    }

    $rangeTxt = if ($Days -eq 1) { $from.ToString('yyyy-MM-dd') }
                else { "$($from.ToString('yyyy-MM-dd')) ~ $($to.AddDays(-1).ToString('yyyy-MM-dd'))" }
    Write-Host "发布单  : $(if($mf.name){$mf.name}else{Split-Path $Manifest -Leaf})" -ForegroundColor White
    Write-Host "时间窗  : $rangeTxt" -ForegroundColor White
    Write-Host "站点来源: $srcTxt（$($sites.Count) 个）" -ForegroundColor DarkGray
    Write-Host "组件 $($comps.Count) 个：$(($comps | ForEach-Object { "$($_.Name)=$($_.Expect)" }) -join '  ')" -ForegroundColor DarkGray
    if ($unknown) { Write-Host "⚠ 发布单里这些编号 Jenkins 上不存在：$($unknown -join ', ')" -ForegroundColor Yellow }
    Write-Host ''

    $cells = New-Object System.Collections.Generic.List[object]
    foreach ($s in $sites) {
        foreach ($c in $comps) {
            # 一个站点的同一组件可能有多个 job（如 某站点的 Pages 有 5 个），逐个判定
            $matched = @($allJobs | Where-Object { $_.name -match "^$([regex]::Escape($s))-$([regex]::Escape($c.Name))(-|$)" })
            if (-not $matched) {
                $cells.Add([pscustomobject]@{Site=$s;Comp=$c.Name;Job='';State='NOJOB';Num='';Result='';Time=$null;Expect=$c.Expect;Actual='';Sha='';Who='';Url=''})
                continue
            }
            foreach ($j in $matched) {
                if ($IgnoreJob.Count -and ($IgnoreJob | Where-Object { $j.name -like $_ })) { continue }
                $inWin = @($j.builds | ForEach-Object { Read-BuildInfo $_ } |
                           Where-Object { $_.Time -ge $from -and $_.Time -lt $to } | Sort-Object Time -Descending)
                if (-not $inWin) {
                    $cells.Add([pscustomobject]@{Site=$s;Comp=$c.Name;Job=$j.name;State='MISS';Num='';Result='';Time=$null;Expect=$c.Expect;Actual='';Sha='';Who='';Url=$j.url})
                    continue
                }
                $b = $inWin[0]      # 窗口内最后一次构建为准
                $exp = Normalize-Ref $c.Expect
                $st = if     ($b.Result -eq 'BUILDING') { 'RUN'  }
                      elseif ($b.Result -ne 'SUCCESS')  { 'FAIL' }
                      elseif ($exp -and $b.Want -and $b.Want -ne $exp) { 'VER' }
                      elseif ($exp -and -not $b.Want -and $b.Got -and $b.Got -ne $exp) { 'VER' }
                      else { 'OK' }
                $cells.Add([pscustomobject]@{Site=$s;Comp=$c.Name;Job=$j.name;State=$st;Num=$b.Num;Result=$b.Result
                    Time=$b.Time;Expect=$c.Expect;Actual=$(if($b.Want){$b.Want}else{$b.Got});Sha=$b.Sha;Who=$b.Who;Url=$b.Url})
            }
        }
    }

    # ---- 矩阵 ----
    $mark = @{ OK='OK'; MISS='未发'; FAIL='失败'; VER='版本错'; RUN='构建中'; NOJOB='--' }
    $cc   = @{ OK='Green'; MISS='Red'; FAIL='Red'; VER='Yellow'; RUN='Cyan'; NOJOB='DarkGray' }
    # 列宽按最长组件名算，别写死（WebIntranetApi 就有 14 字）
    $w   = ((@($comps.Name | ForEach-Object { $_.Length }) | Measure-Object -Maximum).Maximum) + 3
    $ord = @{ FAIL=0; MISS=1; VER=2; RUN=3; OK=4; NOJOB=5 }

    # 站点几十上百个时，-OnlyProblem 让矩阵只留有问题的行
    $rowSites = if ($OnlyProblem) {
        @($sites | Where-Object { $sx = $_; @($cells | Where-Object { $_.Site -eq $sx -and $_.State -in 'MISS','FAIL','VER','RUN' }) })
    } else { $sites }

    if (-not $rowSites) { Write-Host "（所有站点均通过，无需展示矩阵）" -ForegroundColor Green }
    else {
    Write-Host (Pad '站点' 9) -NoNewline -ForegroundColor White
    foreach ($c in $comps) { Write-Host (Pad $c.Name $w) -NoNewline -ForegroundColor White }
    Write-Host ''
    foreach ($s in $rowSites) {
        Write-Host (Pad $s 9) -NoNewline
        foreach ($c in $comps) {
            $g = @($cells | Where-Object { $_.Site -eq $s -and $_.Comp -eq $c.Name })
            # 同组件多 job 时（如 某站点的 Pages 有 5 个），取最差的那个状态展示
            $worst = ($g | Sort-Object @{e={$ord[$_.State]}} | Select-Object -First 1).State
            $txt = $mark[$worst]
            if ($g.Count -gt 1) { $txt = "$txt x$($g.Count)" }
            Write-Host (Pad $txt $w) -NoNewline -ForegroundColor $cc[$worst]
        }
        Write-Host ''
    }
    }

    # ---- 问题明细 ----
    $prob = @($cells | Where-Object { $_.State -in 'MISS','FAIL','VER','RUN' })
    Write-Host "`n---- 需要处理 ----" -ForegroundColor White
    if (-not $prob) { Write-Host "  (无，发布单全部核对通过)" -ForegroundColor Green }
    else {
        foreach ($p in ($prob | Sort-Object @{e={@{FAIL=0;MISS=1;VER=2;RUN=3}[$_.State]}}, Site, Comp)) {
            $d = switch ($p.State) {
                'MISS' { "时间窗内没有构建" }
                'FAIL' { "#$($p.Num) $($p.Result)  $($p.Time.ToString('MM-dd HH:mm'))  by $($p.Who)" }
                'VER'  { "#$($p.Num) 期望 $($p.Expect) 实际 $($p.Actual)  $($p.Time.ToString('MM-dd HH:mm'))" }
                'RUN'  { "#$($p.Num) 正在构建中" }
            }
            $jn = if ($p.Job) { $p.Job } else { "$($p.Site)-$($p.Comp) (无此任务)" }
            Write-Host ("  [$(Pad $p.State 5)] $(Pad $jn 46) $d") -ForegroundColor $cc[$p.State]
        }
    }

    Write-Host "`n---- 汇总 ----" -ForegroundColor White
    foreach ($s in 'OK','VER','FAIL','MISS','RUN','NOJOB') {
        $n = @($cells | Where-Object State -eq $s).Count
        if ($n) { Write-Host ("  {0,-6} {1}" -f $s, $n) -ForegroundColor $cc[$s] }
    }
    $bad = @($cells | Where-Object State -in 'MISS','FAIL','VER').Count
    Write-Host $(if ($bad) { "`n发布单有 $bad 项没过，见上面明细。" } else { "`n发布单全部通过。" }) `
               -ForegroundColor $(if ($bad) { 'Red' } else { 'Green' })

    if ($CsvPath) {
        $cells | Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
        Write-Host "已导出：$CsvPath" -ForegroundColor DarkGray
    }
    return
}

# ---------------------------------------------------------------- 模式零：构建历史
# 前面几个模式只看 lastBuild（每个 job 最后一次）；这里拉 builds[] 全部历史再按时间窗过滤，
# 等价于把每个 job 的「构建历史」页汇总到一起。{0,N} 是 Jenkins tree 的范围限定符，
# 不加会把几百次历史全拉下来，请求会非常慢。
if ($History) {
    $from = $Date.Date
    $to   = $from.AddDays($Days)
    $hTree = 'jobs[name,url,builds[number,result,building,timestamp,duration,url,actions[' +
             'parameters[name,value],lastBuiltRevision[SHA1,branch[name,SHA1]],' +
             "causes[userName,shortDescription]]]{0,$MaxPerJob}]"

    $hits = New-Object System.Collections.Generic.List[object]
    $hSeen = @{}
    foreach ($v in $targetViews) {
        try   { $jobs = (Get-Json ($v.url.TrimEnd('/') + "/api/json?tree=$hTree")).jobs }
        catch { Write-Warning "视图【$($v.name)】读取失败: $($_.Exception.Message)"; continue }
        foreach ($j in $jobs) {
            if ($hSeen.ContainsKey($j.url)) { continue }
            $hSeen[$j.url] = $true
            if (-not (Test-JobWanted $j.name)) { continue }
            foreach ($b in @($j.builds)) {
                $i = Read-BuildInfo $b
                if ($i.Time -lt $from -or $i.Time -ge $to) { continue }
                $hits.Add([pscustomobject]@{
                    View   = $v.name; Job = $j.name; Num = $i.Num; Result = $i.Result
                    Time   = $i.Time; Dur = $i.Dur
                    Branch = $(if ($i.Want) { $i.Want } else { $i.Got }); Sha = $i.Sha
                    Who    = $i.Who; Params = $i.Params; Url = $i.Url })
            }
        }
    }

    $rangeTxt = if ($Days -eq 1) { $from.ToString('yyyy-MM-dd') }
                else { "$($from.ToString('yyyy-MM-dd')) ~ $($to.AddDays(-1).ToString('yyyy-MM-dd'))" }
    Write-Host "构建历史  $rangeTxt   视图：$(($targetViews.name) -join ', ')" -ForegroundColor White
    Write-Host "共 $($hits.Count) 次构建，涉及 $((@($hits | Group-Object Job)).Count) 个 job`n" -ForegroundColor DarkGray

    if (-not $hits.Count) { Write-Host "  (这段时间没有构建)" -ForegroundColor DarkGray; return }

    $show = if ($OnlyProblem) { $hits | Where-Object { $_.Result -notin 'SUCCESS' } } else { $hits }
    $c = @{ SUCCESS='Green'; FAILURE='Red'; ABORTED='DarkYellow'; UNSTABLE='Yellow'; BUILDING='Cyan' }
    foreach ($r in ($show | Sort-Object Time)) {
        Write-Host ("  $($r.Time.ToString('MM-dd HH:mm:ss'))  $(Pad $r.Job 46)#$(Pad $r.Num 6)$(Pad $r.Result 10)$(('{0,5}s' -f $r.Dur))  $(Pad $r.Branch 26)$(Pad $r.Sha 9)$($r.Who)") `
            -ForegroundColor $(if ($c[$r.Result]) { $c[$r.Result] } else { 'Gray' })
    }

    Write-Host "`n---- 按结果 ----" -ForegroundColor White
    $hits | Group-Object Result | Sort-Object Count -Descending | ForEach-Object {
        Write-Host ("  {0,-9} {1}" -f $_.Name, $_.Count) -ForegroundColor $(if ($c[$_.Name]) { $c[$_.Name] } else { 'Gray' }) }
    Write-Host "`n---- 按 SHA ----" -ForegroundColor White
    $hits | Where-Object Sha | Group-Object Sha | Sort-Object { ($_.Group.Time | Measure-Object -Maximum).Maximum } -Descending | ForEach-Object {
        Write-Host ("  {0}  {1,3} 次   {2} ~ {3}   分支 {4}" -f $_.Name, $_.Count,
            ($_.Group.Time | Measure-Object -Minimum).Minimum.ToString('HH:mm'),
            ($_.Group.Time | Measure-Object -Maximum).Maximum.ToString('HH:mm'),
            (($_.Group.Branch | Sort-Object -Unique) -join ',')) -ForegroundColor Cyan }
    Write-Host "`n---- 按触发人 ----" -ForegroundColor White
    $hits | Group-Object Who | Sort-Object Count -Descending | ForEach-Object {
        Write-Host ("  {0,-20} {1}" -f $_.Name, $_.Count) }

    if ($CsvPath) {
        $hits | Select-Object View,Job,Num,Result,Time,Dur,Branch,Sha,Who,Params,Url |
            Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
        Write-Host "`n已导出：$CsvPath" -ForegroundColor DarkGray
    }
    return
}

$rows = New-Object System.Collections.Generic.List[object]
$seen = @{}   # 同一 job 挂在多个视图时只统计一次（按 job url 去重）

foreach ($v in $targetViews) {
    try   { $jobs = (Get-Json ($v.url.TrimEnd('/') + "/api/json?tree=$jobTree")).jobs }
    catch { Write-Warning "视图【$($v.name)】读取失败: $($_.Exception.Message)"; continue }

    foreach ($j in $jobs) {
        if ($seen.ContainsKey($j.url)) { continue }
        $seen[$j.url] = $true
        if ($IgnoreJob.Count -and ($IgnoreJob | Where-Object { $j.name -like $_ })) { continue }

        $b = $j.lastBuild
        if (-not $b) {
            # 文件夹型 / Multibranch / 从未构建过的 job 没有 lastBuild
            $rows.Add([pscustomobject]@{ View=$v.name; Job=$j.name; Num=''; Result='NOBUILD'
                                         Time=$null; Want=''; Got=''; Sha=''; Params=''; Who=''; Url=$j.url })
            continue
        }

        $when = [datetimeoffset]::FromUnixTimeMilliseconds([int64]$b.timestamp).LocalDateTime

        # 全部构建参数（不关心有几个、叫什么），另外单独挑出版本参数
        $ps = @()
        foreach ($a in @($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'parameters' -and $_.parameters })) {
            $ps += $a.parameters
        }
        $paramStr = (($ps | ForEach-Object { "$($_.name)=$($_.value)" }) -join '; ')
        $wanted   = ($ps | Where-Object { $_.name -in 'TAG', 'BRANCH_NAME' } | Select-Object -First 1).value

        # Git 插件记录的真实 checkout revision —— 参数填对 != 真的拉了那个分支
        $rev = @($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'lastBuiltRevision' -and $_.lastBuiltRevision } |
                 Select-Object -First 1).lastBuiltRevision
        $revName = ''; $sha = ''
        if ($rev) {
            $revName = ($rev.branch | Select-Object -First 1).name
            if ($rev.SHA1) { $sha = $rev.SHA1.Substring(0, 7) }
        }

        $cause = (@($b.actions | Where-Object { $_.PSObject.Properties.Name -contains 'causes' -and $_.causes } |
                    Select-Object -First 1).causes | Select-Object -First 1)

        $rows.Add([pscustomobject]@{
            View   = $v.name
            Job    = $j.name
            Num    = $b.number
            Result = $(if ($b.building) { 'BUILDING' } else { $b.result })
            Time   = $when
            Want   = (Normalize-Ref $wanted)     # 参数里写的分支（已归一化）
            Got    = (Normalize-Ref $revName)    # 实际 checkout 的分支（已归一化）
            Sha    = $sha
            Params = $paramStr
            Who    = $(if ($cause.userName) { $cause.userName } else { $cause.shortDescription })
            Url    = $b.url
        })
    }
}

Write-Host "扫描 $($rows.Count) 个 job / $($targetViews.Count) 个视图（$(($targetViews.name) -join ', ')）`n" -ForegroundColor DarkGray

# ---------------------------------------------------------------- 模式一：分支 SHA 分布
if ($ListBranches -or -not $Branch) {
    Write-Host "各分支 SHA 分布（同一分支出现多个 SHA = 有 job 停在旧代码）:" -ForegroundColor White
    $rows | Where-Object { $_.Got -and $_.Sha } | Group-Object Got | Sort-Object Count -Descending | ForEach-Object {
        # 必须用 @() 包起来：只有一个分组时 $shas.Count 返回的是组内元素数，不是分组数
        $shas = @($_.Group | Group-Object Sha)
        Write-Host ("`n  [{0}]  {1} 个 job，{2} 个 SHA" -f $_.Name, $_.Count, $shas.Count) -ForegroundColor Cyan
        $shas | Sort-Object { ($_.Group.Time | Measure-Object -Maximum).Maximum } -Descending | ForEach-Object {
            $mx = ($_.Group.Time | Measure-Object -Maximum).Maximum
            $mn = ($_.Group.Time | Measure-Object -Minimum).Minimum
            Write-Host ("    {0}  {1,4} 个   {2} ~ {3}" -f $_.Name, $_.Count,
                        $mn.ToString('MM-dd HH:mm'), $mx.ToString('MM-dd HH:mm'))
            if ($_.Count -le 8) { Write-Host ("          " + (($_.Group.Job) -join ', ')) -ForegroundColor DarkGray }
        }
    }
    # 参数与实际不一致的（归一化之后仍不一致才是真问题）
    $mismatch = $rows | Where-Object { $_.Want -and $_.Got -and $_.Want -ne $_.Got }
    Write-Host "`n参数分支 与 实际 checkout 不一致:" -ForegroundColor White
    if ($mismatch) { $mismatch | ForEach-Object { Write-Host ("  {0,-44} 参数={1}  实际={2}" -f $_.Job, $_.Want, $_.Got) -ForegroundColor Yellow } }
    else           { Write-Host "  (无)" -ForegroundColor Green }

    Write-Host "`n拿上面的分支名再跑：-Branch '分支名' -OnlyProblem" -ForegroundColor DarkGray
    return
}

# ---------------------------------------------------------------- 模式二：按分支核对
$onBranch = $rows | Where-Object { $_.Got -eq (Normalize-Ref $Branch) }
if (-not $onBranch) {
    throw "没有 job 的最后一次构建落在分支 [$Branch] 上。先跑 -ListBranches 看可选分支。"
}
if (-not $BaseSha) { $BaseSha = ($onBranch | Sort-Object Time -Descending | Select-Object -First 1).Sha }
$baseTime = ($onBranch | Where-Object Sha -eq $BaseSha | Sort-Object Time -Descending | Select-Object -First 1).Time

Write-Host "分支    : $Branch"                                                   -ForegroundColor White
Write-Host "基准SHA : $BaseSha  (最新构建 $($baseTime.ToString('yyyy-MM-dd HH:mm')))`n" -ForegroundColor White

$res = $onBranch | ForEach-Object {
    $state = if     ($_.Result -eq 'BUILDING')  { 'RUN'   }
             elseif ($_.Result -ne 'SUCCESS')   { 'FAIL'  }
             elseif ($_.Sha    -ne $BaseSha)    { 'STALE' }
             else                               { 'OK'    }
    $_ | Add-Member -NotePropertyName State -NotePropertyValue $state -PassThru
}

$color = @{ OK='Green'; STALE='Yellow'; FAIL='Red'; RUN='Cyan' }
$order = @{ FAIL=0; STALE=1; RUN=2; OK=3 }
$show  = if ($OnlyProblem) { $res | Where-Object State -in 'FAIL','STALE' } else { $res }

foreach ($g in ($show | Sort-Object @{e={$order[$_.State]}}, View, Job | Group-Object View)) {
    Write-Host "`n== $($g.Name) ==" -ForegroundColor White
    foreach ($r in $g.Group) {
        Write-Host ("  [$(Pad $r.State 5)] $(Pad $r.Job 46)#$(Pad $r.Num 6)$(Pad $r.Result 10)$($r.Time.ToString('MM-dd HH:mm'))  $($r.Sha)") -ForegroundColor $color[$r.State]
    }
}

Write-Host "`n---- 汇总 ----" -ForegroundColor White
foreach ($s in 'OK','STALE','FAIL','RUN') {
    $n = @($res | Where-Object State -eq $s).Count
    if ($n) { Write-Host ("  {0,-6} {1}" -f $s, $n) -ForegroundColor $color[$s] }
}
$bad = @($res | Where-Object State -in 'FAIL','STALE').Count
Write-Host $(if ($bad) { "`n有 $bad 个 job 没跟上基准 SHA，需要确认。" } else { "`n全部一致。" }) `
           -ForegroundColor $(if ($bad) { 'Red' } else { 'Green' })

if ($CsvPath) {
    $res | Select-Object State,View,Job,Num,Result,Time,Want,Got,Sha,Who,Params,Url |
        Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
    Write-Host "已导出：$CsvPath" -ForegroundColor DarkGray
}
