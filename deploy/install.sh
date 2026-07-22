#!/bin/bash
# CentOS 7.9 一键部署 Jenkins 发版核对网页版。
# 用法（在服务器上，root 执行）：
#     bash deploy/install.sh https://你的jenkins地址
#
# 装完：systemctl status check-jenkins-release
#       浏览器访问 http://<服务器IP>:8770
set -euo pipefail

JENKINS_URL="${1:-}"
APP_DIR=/opt/check-jenkins-release
ENV_FILE=/etc/check-jenkins-release.env
SVC=check-jenkins-release
PORT="${PORT:-8770}"
RUN_USER=jenkinscheck

if [[ $EUID -ne 0 ]]; then echo "请用 root 执行"; exit 1; fi
if [[ -z "$JENKINS_URL" ]]; then
    echo "用法: bash deploy/install.sh https://你的jenkins地址"; exit 1
fi

echo "==> 检查 Python 3"
# CentOS 7.9 自带 python3 是 3.6.8，够用（代码里对 3.6 做了兼容处理）。
# 注意 CentOS 7 已 EOL，官方 yum 源已下线，装包可能要先切 vault 源。
if ! command -v python3 >/dev/null 2>&1; then
    echo "没有 python3，尝试安装..."
    if ! yum install -y python3; then
        cat <<'EOF'
yum 安装失败。CentOS 7 已 EOL，官方源已下线，先切到 vault 源再重试：
    sed -i 's|^mirrorlist=|#mirrorlist=|g'                       /etc/yum.repos.d/CentOS-*.repo
    sed -i 's|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*.repo
    yum clean all && yum makecache && yum install -y python3
EOF
        exit 1
    fi
fi
python3 -c 'import sys; print("    python3 =", sys.version.split()[0])'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)' \
    || { echo "需要 Python 3.6 以上"; exit 1; }

echo "==> 创建运行用户 $RUN_USER（无登录 shell、无家目录）"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd -r -s /sbin/nologin -M "$RUN_USER"

echo "==> 安装程序到 $APP_DIR"
mkdir -p "$APP_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -m 0644 "$SRC/app.py" "$APP_DIR/app.py"
chown -R root:root "$APP_DIR"

echo "==> 写配置 $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
    echo "    已存在，保留不覆盖（要改直接编辑它）"
else
    cat > "$ENV_FILE" <<EOF
JENKINS_URL=$JENKINS_URL
JENKINS_WEB_HOST=0.0.0.0
JENKINS_WEB_PORT=$PORT

# 关键：服务器部署必须为 0。
# 设成 1 的话，所有访问者都会用下面这份服务端凭据查询 Jenkins，
# 等于绕过 Jenkins 权限体系 —— 谁能打开网页谁就有这个账号的可见范围。
# 为 0 时每个人在网页上填自己的用户名 + API Token，服务端不保存。
JENKINS_ALLOW_SERVER_CREDS=0

# 每个 job 往回翻多少次构建历史
JENKINS_MAX_BUILDS=30

# Jenkins 用自签证书时打开
# JENKINS_SKIP_CERT=1
EOF
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE"
fi

echo "==> 安装 systemd 服务"
install -m 0644 "$SRC/deploy/$SVC.service" "/etc/systemd/system/$SVC.service"
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 2

echo "==> 放行防火墙端口 $PORT"
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null && firewall-cmd --reload >/dev/null
    echo "    firewalld 已放行"
else
    echo "    firewalld 未运行，跳过（若用 iptables/云安全组，请自行放行）"
fi

echo
if systemctl is-active --quiet "$SVC"; then
    echo "==> 部署完成"
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null && echo "    健康检查 OK"
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "    访问: http://${IP:-<服务器IP>}:$PORT"
    echo "    日志: journalctl -u $SVC -f"
    echo
    echo "    每个使用者首次打开网页时，需要填自己的 Jenkins 用户名和 API Token"
    echo "    （在 $JENKINS_URL/me/security/ 生成）。凭据只存浏览器标签页，服务器不保存。"
else
    echo "!! 启动失败，看日志："
    echo "   journalctl -u $SVC -n 50 --no-pager"
    exit 1
fi
