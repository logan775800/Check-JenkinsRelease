#!/bin/bash
# 用 Cloudflare Tunnel 把本机的 8770 暴露成 https://<子域>.<你的域名>
#
# 相比「开 80/443 + nginx 反代」，Tunnel 的好处：
#   - 服务器不用开任何入站端口，公网扫不到你（cloudflared 是主动往外连）
#   - 不用申请/续期证书，HTTPS 由 Cloudflare 边缘终止
#   - 不用暴露服务器真实 IP
#
# 用法（服务器上 root 执行）：
#     bash deploy/cloudflare/install-cloudflared.sh jc.logan775800.top
#
# 过程中会弹出一个链接，复制到你自己电脑的浏览器打开，登录 Cloudflare 并授权该域名。
set -euo pipefail

HOSTNAME_FQDN="${1:-}"
TUNNEL_NAME="${TUNNEL_NAME:-jenkins-check}"
LOCAL_PORT="${LOCAL_PORT:-8770}"

if [[ $EUID -ne 0 ]]; then echo "请用 root 执行"; exit 1; fi
if [[ -z "$HOSTNAME_FQDN" ]]; then
    echo "用法: bash deploy/cloudflare/install-cloudflared.sh jc.logan775800.top"; exit 1
fi

echo "==> 安装 cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
    # cloudflared 是 Go 静态编译的单文件，CentOS 7.9 的老 glibc 也能跑
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  PKG=cloudflared-linux-x86_64.rpm ;;
        aarch64) PKG=cloudflared-linux-aarch64.rpm ;;
        *) echo "不支持的架构: $ARCH"; exit 1 ;;
    esac
    curl -fsSL -o /tmp/cloudflared.rpm \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/$PKG"
    yum localinstall -y /tmp/cloudflared.rpm || rpm -Uvh --nodeps /tmp/cloudflared.rpm
    rm -f /tmp/cloudflared.rpm
fi
cloudflared --version

echo
echo "==> 登录 Cloudflare（会打印一个链接，复制到浏览器打开并选择 logan775800.top）"
if [[ ! -f /root/.cloudflared/cert.pem ]]; then
    cloudflared tunnel login
fi

echo
echo "==> 创建 Tunnel: $TUNNEL_NAME"
if ! cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
    cloudflared tunnel create "$TUNNEL_NAME"
fi
TUNNEL_ID=$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2==n {print $1}' | head -1)
if [[ -z "$TUNNEL_ID" ]]; then echo "没拿到 Tunnel ID"; exit 1; fi
echo "    Tunnel ID = $TUNNEL_ID"

echo "==> 绑定域名 $HOSTNAME_FQDN（自动写 CNAME 到 Cloudflare DNS）"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME_FQDN"

echo "==> 写配置 /etc/cloudflared/config.yml"
mkdir -p /etc/cloudflared
cp -f "/root/.cloudflared/$TUNNEL_ID.json" /etc/cloudflared/
chmod 600 "/etc/cloudflared/$TUNNEL_ID.json"
cat > /etc/cloudflared/config.yml <<EOF
tunnel: $TUNNEL_ID
credentials-file: /etc/cloudflared/$TUNNEL_ID.json

ingress:
  - hostname: $HOSTNAME_FQDN
    service: http://127.0.0.1:$LOCAL_PORT
    originRequest:
      # 首次查询要拉几百个 job 的构建历史，可能几十秒，别用默认 30s
      connectTimeout: 30s
      tlsTimeout: 30s
      noHappyEyeballs: true
  - service: http_status:404
EOF
chmod 600 /etc/cloudflared/config.yml

echo "==> 装成系统服务并启动"
cloudflared service install 2>/dev/null || true
systemctl enable cloudflared >/dev/null 2>&1 || true
systemctl restart cloudflared
sleep 3

echo
echo "==> 应用改回只听回环（外部只能经 Tunnel 进来）"
ENV_FILE=/etc/check-jenkins-release.env
if [[ -f "$ENV_FILE" ]]; then
    sed -i 's|^JENKINS_WEB_HOST=.*|JENKINS_WEB_HOST=127.0.0.1|' "$ENV_FILE"
    systemctl restart check-jenkins-release
    echo "    JENKINS_WEB_HOST=127.0.0.1，已重启"
fi
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --remove-port="$LOCAL_PORT/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    echo "    已收回防火墙上的 $LOCAL_PORT 端口（不再需要）"
fi

echo
if systemctl is-active --quiet cloudflared; then
    echo "==> 完成：https://$HOSTNAME_FQDN"
    echo "    Tunnel 日志: journalctl -u cloudflared -f"
    echo "    应用日志:    journalctl -u check-jenkins-release -f"
    echo
    echo "!! 强烈建议再加一层 Cloudflare Access（Zero Trust → Access → Applications），"
    echo "!! 否则这个地址任何人都能打开。配置见 deploy/cloudflare/README.md"
else
    echo "!! cloudflared 启动失败：journalctl -u cloudflared -n 50 --no-pager"
    exit 1
fi
