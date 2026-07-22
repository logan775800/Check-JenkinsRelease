#!/bin/bash
# 用 docker compose 部署。适用于宿主机 TLS 栈太老连不上 Jenkins 的情况
# （CentOS 7.9 的 OpenSSL 1.0.2k 只到 TLS 1.2，对端若强制 TLS 1.3 就握手失败）。
#
#     bash deploy/install-docker.sh https://你的jenkins地址
#
# 容器只绑 127.0.0.1:8770，外部访问仍走宿主机上的 nginx 反代，nginx 配置不用改。
# 之后日常操作直接在仓库根目录用 docker compose 即可。
set -euo pipefail

JENKINS_URL="${1:-}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$SRC/.env"
NAME=jenkins-check
PORT=8770

if [[ -z "$JENKINS_URL" ]]; then
    echo "用法: bash deploy/install-docker.sh https://你的jenkins地址"; exit 1
fi

# --- compose 命令：v2 是 `docker compose`，v1 是 `docker-compose`，两种都兼容 ---
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    cat <<'EOF'
没找到 docker compose。CentOS 7 安装参考：
    yum install -y yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
装好后重跑本脚本。
EOF
    exit 1
fi
echo "==> 使用 $DC"

echo "==> 写配置 $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
    sed -i "s|^JENKINS_URL=.*|JENKINS_URL=$JENKINS_URL|" "$ENV_FILE"
    echo "    已存在，只更新 JENKINS_URL（其余保留）"
else
    sed "s|^JENKINS_URL=.*|JENKINS_URL=$JENKINS_URL|" "$SRC/.env.example" > "$ENV_FILE"
    echo "    已从 .env.example 生成"
fi
chmod 600 "$ENV_FILE"

echo "==> 构建镜像"
cd "$SRC"
$DC build

echo "==> 先验证容器内能否握手成功（这正是宿主机做不到的那一步）"
if docker run --rm --env-file "$ENV_FILE" "$NAME:latest" python -c "
import os,ssl,sys,urllib.request,urllib.error
print('    容器内 OpenSSL:', ssl.OPENSSL_VERSION)
u=os.environ['JENKINS_URL'].rstrip('/')+'/login'
r=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
try:
    with urllib.request.urlopen(r, timeout=30) as f: print('    TLS 握手 OK, HTTP', f.status)
except urllib.error.HTTPError as e: print('    TLS 握手 OK, HTTP', e.code)
except Exception as e: print('    失败:', e); sys.exit(1)
"; then
    echo "    ✓ 容器内可以连通 Jenkins"
else
    echo "    ✗ 容器里也连不上，问题不在 TLS 版本，先排查网络/防火墙/JENKINS_URL"
    exit 1
fi

# 之前用 systemd 直接跑的话会占着同一个端口
if systemctl is-active --quiet check-jenkins-release 2>/dev/null; then
    systemctl disable --now check-jenkins-release
    echo "==> 已停用旧的 systemd 服务（端口冲突）"
fi

echo "==> 启动"
$DC up -d
sleep 3

if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    echo
    echo "==> 完成。健康检查 OK"
    echo "    日志:   cd $SRC && $DC logs -f"
    echo "    重启:   cd $SRC && $DC restart"
    echo "    停止:   cd $SRC && $DC down"
    echo "    更新:   cd $SRC && git pull && $DC up -d --build"
    echo "    改配置: 编辑 $SRC/.env 后 $DC up -d"
    echo
    echo "    nginx 配置不用改，仍然是 proxy_pass http://127.0.0.1:$PORT"
else
    echo "!! 起来了但健康检查不过：cd $SRC && $DC logs"
    exit 1
fi
