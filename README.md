# Check-JenkinsRelease

发版后一次性核对 Jenkins 上几百个 job：**哪些漏发了、哪些发错了 tag、哪些构建失败了。**

适用于「一次发版横跨多个视图、多个站点、多个组件」的场景 —— 不用再逐个点开构建历史人工比对。
**只读，不会触发任何构建。**

提供两种用法：

| | 适合 |
|---|---|
| `app.py` 网页版 | 发版收尾例行核对：把发布单整段粘进去，点一下出结果 |
| `Check-JenkinsRelease.ps1` 命令行版 | 排查问题：分支 SHA 分布、构建历史、CSV 导出 |

两者数据源和判定逻辑一致。

---

## 快速开始

### 1. 拿一个 Jenkins API Token

`https://<你的Jenkins>/me/security/` → API Token → 添加新 Token。**只显示一次，当场复制。**
认证用户名填 Jenkins **登录名**，不是邮箱。

### 2. 设环境变量

```powershell
$env:JENKINS_URL='https://your-jenkins-host'
$env:JENKINS_USER='登录名'
$env:JENKINS_API_TOKEN='刚复制的token'
```

### 3. 跑

```powershell
# 网页版
python app.py                    # 浏览器开 http://127.0.0.1:8770

# 命令行版
.\Check-JenkinsRelease.ps1 -ListBranches
```

---

## 网页版

把发布单整段复制粘进文本框 → 点「开始检查」。**站点编号和组件 tag 全部自动解析**，不用手敲。

支持的发布单写法：

```
【发布站点】
（国家）AR001→SiteA
（国家）AR002-SiteB

【发布步骤】
3.发布代码
    LotteryApi      →   tag:   master_V1.00_001
    web             →   分支:  masterBranch/main-1.00
```

也支持**站点分组**（同一组件按组发不同分支）：

```
中台站点
    （国家）AR001-SiteA
非中台站点
    （国家）AR002-SiteB
1.发代码
    web  tag（中台）  : feature/v3
    web  tag（非中台）: masterBranch/main-1.00
```

组件名容错：`web` / `Web-Pages` / `web-pages` / `WEB_PAGES` / `webpages` / `Web Pages` / `前端` / `页面`
全部识别为 `Pages`。箭头可有可无，`tag:` / `分支:` / `branch:` 都认。

站点编号容错：`AR51` / `ar-051` / `AR0051` 统一归成 `AR051`，自动去重，
**发布单里写了但 Jenkins 上不存在的编号会单独报出来**（抓发布单笔误）。

### 状态

| 状态 | 含义 |
|---|---|
| 已发布 | 时间窗内构建成功，且 tag/分支与发布单一致 |
| 未发布 | 时间窗内没有构建 —— 漏发 |
| 构建失败 | FAILURE / ABORTED / UNSTABLE |
| 版本不符 | 发了，但 tag/分支与发布单不一致 |
| 构建中 | 还在跑 |
| 无此任务 | 该站点没有这个组件的 job |

### 「分支名一样但代码不一样」提示

同一分支下出现多个 SHA 时会额外提示。这是**分支发版特有的坑**：

分支是可变指针，Jenkins 构建时拿的是那一刻的分支顶端。一个站点一个站点手工点发，
整个过程若跨了一两个小时，中间有人往分支推了代码，先发的站点拿到的就是旧代码 ——
**分支名完全相同，报告全绿，但线上跑着好几份不同的代码。**

用 tag 发版则不会有这个问题，tag 是不可变快照。

这个提示不参与红绿判定（那几个提交可能是无关改动），只是提醒你去 GitLab 比一下：
`<你的GitLab>/<项目>/-/compare/<旧SHA>...<新SHA>`

---

## 命令行版

```powershell
# 1) 全局体检：各分支的 SHA 分布，看有没有 job 停在旧代码
.\Check-JenkinsRelease.ps1 -ListBranches

# 2) 按分支核对：以该分支最新 SHA 为基准，找出没跟上的
.\Check-JenkinsRelease.ps1 -Branch 'masterBranch/main-1.00' -OnlyProblem

# 3) 构建历史：某天/某几天，可按视图、站点、组件过滤
.\Check-JenkinsRelease.ps1 -History -View 'Web-Pages'
.\Check-JenkinsRelease.ps1 -History -Date '2026-01-01' -Days 3 -OnlyProblem
.\Check-JenkinsRelease.ps1 -History -Site 'AR001','AR002'
.\Check-JenkinsRelease.ps1 -History -Component 'LotteryApi','ThirdJob'

# 4) 按发布单核对：出「站点 × 组件」矩阵
.\Check-JenkinsRelease.ps1 -Manifest .\manifests\example-release.json
.\Check-JenkinsRelease.ps1 -Manifest .\my.json -FromClipboard    # 复制发布单后直接跑
.\Check-JenkinsRelease.ps1 -Manifest .\my.json -ParseOnly        # 先看站点抠对没
```

通用参数：`-View` `-Site` `-Component` `-Job`（通配符）`-IgnoreJob` `-ExcludeSite`
`-OnlyProblem` `-CsvPath` `-MaxPerJob` `-SkipCertCheck`

---

## 依赖前提

工具假设 job 命名遵循 `AR{编号}-{组件}-{国家}-{站点名}`，组件取第二段。
**命名规约不同的话要改 `site_of()` / `component_of()`（Python）
和 `Get-SiteCode` / `Get-Component`（PowerShell）。**

组件名到 Jenkins 视图名的映射在 `COMPONENT_VIEW`，别名在 `COMPONENT_ALIAS`。

- Python 3.8+（只用标准库，无第三方依赖）
- PowerShell 5.1+

---

## 踩过的坑（改代码前先看）

1. **WAF 按 User-Agent 拦截** —— `python-urllib` 默认 UA 直接 403，必须伪装浏览器 UA。
   PowerShell 的 `Invoke-WebRequest` 不受影响，所以只在 Python 侧会踩到。
2. **参数名不统一** —— Api/Job/Web 类的版本参数叫 `TAG`，Pages 类叫 `BRANCH_NAME`，两个都要试。
3. **分支名必须归一化** —— Git 插件把分支记成 `refs/remotes/origin/xxx`，参数里写的是 `xxx`，
   不剥前缀会产生几百条假告警。
4. **判定要用 SHA 而不是 tag 字符串** —— `master` / `uat` 这类浮动分支，tag 值永远相同，看它等于没看。
5. **同一 job 会挂多个视图** —— 按 job url 去重，否则重复统计。
6. **PowerShell：变量名不区分大小写** —— 内部变量 `$views` 会和参数 `$View` 撞成同一个。
7. **PowerShell：`.ps1` 含中文必须存成 UTF-8 with BOM**，否则 PS 5.1 解析报错。
8. **PowerShell：`Group-Object` 只有一个分组时 `.Count` 返回组内元素数**，不是分组数，要用 `@()` 包住。
9. **终端里中文占 2 个显示宽度**，`.Length` 算 1，用 `"{0,-44}"` 格式化会错位。

---

## 安全

- **Token 只从环境变量读，任何文件里都没有硬编码。**
- 网页版**只监听 `127.0.0.1`**：后端持 Token 转发，Token 不出本机。
- `local/` 已在 `.gitignore` 里 —— 真实发布单、真实站点清单放这儿，不会入库。

### 凭据的两种模式

| 模式 | `JENKINS_ALLOW_SERVER_CREDS` | 谁的 Token | 适用 |
|---|---|---|---|
| 本机自用 | `1`（默认） | 服务端环境变量里那份 | 自己电脑上跑 |
| 多用户 | `0` | 每个人在网页上填自己的 | **部署到服务器必须用这个** |

多用户模式下，凭据只存在使用者浏览器的 `sessionStorage`（关标签页即失效），
随请求头 `X-Jenkins-User` / `X-Jenkins-Token` 传给后端，**服务器不保存、不落盘**。

这样不用另做一套账号体系：**谁能看到什么，直接由他自己在 Jenkins 里的权限决定。**

监听地址默认 `127.0.0.1`。要给别人访问必须显式设 `JENKINS_WEB_HOST=0.0.0.0` ——
故意做成显式的，顺带强制你想一下认证问题。若在对外监听的同时还开着服务端凭据，
启动时会打印醒目告警。

---

## 部署到 CentOS 7.9

```bash
git clone https://github.com/logan775800/Check-JenkinsRelease.git
cd Check-JenkinsRelease
bash deploy/install.sh https://你的jenkins地址
```

脚本做的事：建无登录权限的运行用户 `jenkinscheck` → 装到 `/opt/check-jenkins-release`
→ 写 `/etc/check-jenkins-release.env`（`chmod 600`）→ 装 systemd 服务并开机自启
→ firewalld 放行端口 → 健康检查。

装完：

```bash
systemctl status check-jenkins-release
journalctl -u check-jenkins-release -f
```

浏览器开 `http://<服务器IP>:8770`，每个人首次使用填一次自己的 Jenkins 用户名 + API Token。

### CentOS 7 的 TLS 限制（可能直接卡住你）

CentOS 7.9 自带 **OpenSSL 1.0.2k，最高只到 TLS 1.2**。如果你的 Jenkins 前面有
WAF/CDN 且只接受 TLS 1.3，宿主机上无论 Python 还是 curl 都握手失败：

```
URLError: [SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version
curl: (35) Peer reports incompatible or unsupported protocol version
```

一分钟确认是不是这个问题：

```bash
openssl version                      # 1.0.x 就有嫌疑
curl -sS -o /dev/null https://你的jenkins/api/json   # 报 (35) 即中招
openssl s_client -connect 你的jenkins:443 -servername 你的jenkins -tls1_2 </dev/null 2>&1 | grep Cipher
# 出现 "Cipher is (NONE)" = 对端拒绝了 TLS 1.2，只收 1.3
```

**解法（按省事程度）：**

1. **Jenkins 就在本机** → 直连 HTTP 绕开 TLS，最省事：
   ```bash
   sed -i 's|^JENKINS_URL=.*|JENKINS_URL=http://127.0.0.1:8080|' /etc/check-jenkins-release.env
   systemctl restart check-jenkins-release
   ```
2. **用 Docker 跑**（镜像自带 OpenSSL 3.x，支持 TLS 1.3），不动宿主机系统库：
   ```bash
   bash deploy/install-docker.sh https://你的jenkins地址
   ```
   容器只绑 `127.0.0.1:8770`，**nginx 配置不用改**。脚本会先在容器里试一次握手，
   通了才启动，免得白折腾；同时自动停掉抢端口的 systemd 服务。

   之后日常操作在仓库根目录用 compose 即可（`docker compose` 和 `docker-compose` 都兼容）：

   ```bash
   docker compose logs -f          # 看日志
   docker compose restart          # 改完 .env 后重启
   docker compose up -d --build    # git pull 之后更新
   docker compose down             # 停
   ```

   配置在仓库根目录的 `.env`（从 `.env.example` 复制，已被 gitignore 忽略）。
3. 升级宿主机 OpenSSL / Python —— CentOS 7 上很折腾，不推荐。

### Jenkins 前面有 WAF：换台机器部署就 403

同一个 Token 在自己电脑上好好的，部署到服务器就 `HTTP 403`。因为 WAF 是按**来源 IP**
放行的，你的办公网在名单里，服务器不在。

判断方法是看 403 的**响应头**（`friendly_neterr` 只能说明状态码，看不出是谁拒的）：

```bash
docker run --rm --env-file .env python:3.12-alpine python -c "
import base64,os,urllib.request,urllib.error
U,T=os.environ['JENKINS_USER'],os.environ['JENKINS_API_TOKEN']
r=urllib.request.Request(os.environ['JENKINS_URL'].rstrip('/')+'/api/json?tree=views[name]')
r.add_header('Authorization','Basic '+base64.b64encode(f'{U}:{T}'.encode()).decode())
r.add_header('User-Agent','Mozilla/5.0')
try:
    print('OK', urllib.request.urlopen(r,timeout=30).status)
except urllib.error.HTTPError as e:
    print('HTTP',e.code); [print(' ',k,':',v) for k,v in e.headers.items()]
"
# 顺便拿服务器出口 IP
docker run --rm python:3.12-alpine python -c "import urllib.request;print(urllib.request.urlopen('https://api.ipify.org',timeout=15).read().decode())"
```

| 响应头特征 | 结论 |
|---|---|
| `Server: cloudflare` + `CF-RAY` + `Attention Required!` | **Cloudflare WAF 在边缘拦的**，请求没到 Jenkins |
| `X-Jenkins: 2.xxx` | Jenkins 本体回的 —— Authorization 头被中间设备剥了，被当成匿名 |

Cloudflare 拦截的解法：控制台 → **Security → WAF → Tools → IP Access Rules**
→ 添加服务器出口 IP，Action 选 **Allow**。
若仍 403，说明是 **Custom rules** 里的规则在拦（IP Access Rules 覆盖不了它），
需要在那条规则表达式里加 `and ip.src ne <服务器IP>`，或加一条 Skip 规则排在它前面。

注意云服务器的出口 IP 要是**固定 EIP**，否则白名单会随 IP 变动失效。

另外顺带一提：这个 WAF 通常也会按 User-Agent 拦 —— `python-urllib` 的默认 UA 直接 403，
所以代码里固定伪装了浏览器 UA。

### CentOS 7.9 的另外两个坑

1. **自带 python3 是 3.6.8**，没有 `ThreadingHTTPServer`（3.7 才有）。
   代码里已做兼容回退（`ThreadingMixIn + HTTPServer`），**不需要装新版 Python**。
2. **CentOS 7 已 EOL**，官方 yum 源下线，`yum install python3` 可能失败。
   先切 vault 源（install.sh 检测到失败会把命令打出来）：
   ```bash
   sed -i 's|^mirrorlist=|#mirrorlist=|g' /etc/yum.repos.d/CentOS-*.repo
   sed -i 's|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*.repo
   yum clean all && yum makecache && yum install -y python3
   ```

### 加域名 / HTTPS

**纯内网**：用 `deploy/nginx.conf.example`（带内网网段白名单），配完把
`JENKINS_WEB_HOST` 改回 `127.0.0.1` 并关掉防火墙上的 8770。

**用 Cloudflare 托管**：见 [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md)。
推荐 Cloudflare Tunnel —— 服务器一个入站端口都不用开：

```bash
bash deploy/cloudflare/install-cloudflared.sh jc.你的域名
```

⚠️ **加了公网域名之后，必须再配 Cloudflare Access**（Zero Trust → Access → Applications，
50 人以内免费），否则这个地址任何人都能打开。页面本身要求填各自的 Jenkins Token 才能查数据，
但「谁能打开这个页面」是另一层，得由 Access 来管。

### 更新

```bash
cd Check-JenkinsRelease && git pull
install -m 0644 app.py /opt/check-jenkins-release/app.py
systemctl restart check-jenkins-release
```
