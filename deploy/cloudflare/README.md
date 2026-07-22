# 用 Cloudflare 托管 HTTPS

以 `jc.logan775800.top` 为例（换成你自己的子域即可）。

先看清楚一件事：**加了域名之后，这个页面就在公网上了。** 页面本身要求每个人填自己的
Jenkins Token 才能查数据，但页面能被谁打开、发布单能被谁粘贴，是另一回事。
所以下面第 3 步的 Cloudflare Access **不是可选项**。

---

## 方案 A：Cloudflare Tunnel（推荐）

服务器**一个入站端口都不用开** —— `cloudflared` 主动向 Cloudflare 建立出站连接。
没有公网端口就没有被扫描、被绕过 CF 直连真实 IP 的问题，也不用管证书。

### 1. 建隧道

```bash
bash deploy/cloudflare/install-cloudflared.sh jc.logan775800.top
```

过程中会打印一个链接 —— 复制到**你自己电脑**的浏览器打开，登录 Cloudflare，
选择 `logan775800.top` 授权。之后脚本会自动：

- 创建 Tunnel、写好 `/etc/cloudflared/config.yml`
- 在 Cloudflare DNS 自动加好 `jc` 的 CNAME 记录
- 装成 systemd 服务并开机自启
- 把应用监听改回 `127.0.0.1`，并收回防火墙上的 8770（不再需要）

装完就是 `https://jc.logan775800.top`。

### 2. 验证

```bash
systemctl status cloudflared
journalctl -u cloudflared -f
curl -I https://jc.logan775800.top
```

### 3. 加 Cloudflare Access（必做）

不加的话，这个地址**任何人都能打开**。Access 是一道登录网关，
用户先过 Cloudflare 的身份验证才能碰到你的页面。50 人以内免费。

Cloudflare 控制台 → **Zero Trust** → **Access** → **Applications** → **Add an application**：

| 项 | 填什么 |
|---|---|
| 类型 | Self-hosted |
| Application name | Jenkins Release Check |
| Session Duration | 8 小时（或按你们习惯） |
| Subdomain / Domain | `jc` / `logan775800.top` |

然后 **Add policy**：

| 项 | 填什么 |
|---|---|
| Policy name | team |
| Action | Allow |
| Include | **Emails** → 逐个填团队成员邮箱<br>或 **Emails ending in** → `@你们公司域名` |

登录方式默认是 One-time PIN（邮箱收验证码），够用；也可以接飞书/企业微信/Google 等 IdP。

配好之后再打开 `https://jc.logan775800.top`，会先跳到 Cloudflare 的登录页。

### 4.（可选）再收紧

- **WAF → Rate limiting**：`/api/check` 限每 IP 每分钟 10 次，防误点狂刷把 Jenkins 拖垮
- **Zero Trust → Settings → 地区限制**：只放行你们实际办公所在地区

---

## 方案 B：开 443 + nginx，Cloudflare 橙云代理

只有在不能用 Tunnel 时才走这条。缺点：服务器必须对公网开 443，
真实 IP 一旦从别的渠道泄露（历史 DNS 记录、邮件头、同 IP 上的其他服务），
攻击者就能绕过 Cloudflare 直连。

### 步骤

1. **DNS**：Cloudflare 加 A 记录 `jc` → 服务器 IP，**橙云（Proxied）**
2. **SSL/TLS 模式**：选 **Full (strict)**。
   ⚠️ **千万别选 Flexible** —— 那样 CF 到你服务器这一段是明文 HTTP，
   浏览器显示绿锁，但实际上半程裸奔，是最常见的错误配置。
3. **证书**：SSL/TLS → Origin Server → Create Certificate（有效期可选 15 年，免费），
   证书存 `/etc/pki/tls/certs/cf-origin.pem`，私钥存 `/etc/pki/tls/private/cf-origin.key`
4. **nginx**：
   ```bash
   yum install -y nginx
   cp deploy/cloudflare/nginx-cf.conf.example /etc/nginx/conf.d/jenkins-check.conf
   bash deploy/cloudflare/gen-cf-allow.sh      # 生成 CF IP 白名单 + 真实 IP 恢复
   nginx -t && systemctl enable --now nginx
   ```
5. **应用改回只听回环**，让外部只能经 nginx 进来：
   ```bash
   sed -i 's|^JENKINS_WEB_HOST=.*|JENKINS_WEB_HOST=127.0.0.1|' /etc/check-jenkins-release.env
   systemctl restart check-jenkins-release
   firewall-cmd --permanent --remove-port=8770/tcp && firewall-cmd --reload
   firewall-cmd --permanent --add-service=https && firewall-cmd --reload
   ```
6. **Access 同样要加**（方案 A 第 3 步，一模一样）

CF 的 IP 段偶尔变动，建议挂个月度 cron：

```
0 4 1 * * root /path/to/gen-cf-allow.sh >/dev/null 2>&1 && nginx -s reload
```

---

## 两个容易踩的坑

**1. 首次查询超时。** 拉几百个 job 的构建历史要几十秒，而 Cloudflare 边缘的
**100 秒超时是硬限制，改不了**（Enterprise 才能调）。目前实测约 10~20 秒，够用；
但 job 数继续涨的话可能撞线。真撞上了有两个办法：把 `JENKINS_MAX_BUILDS` 调小
（默认 30，改成 10 能显著减少传输量），或者改成异步任务 + 轮询。

**2. Access 会拦住 `/healthz`。** 如果你挂了外部监控，要么在 Access 里给
`/healthz` 单开一条 Bypass 策略，要么监控直接打服务器内网地址。
