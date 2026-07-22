#!/bin/bash
# 用 Docker 部署。适用于宿主机 TLS 栈太老连不上 Jenkins 的情况
# （CentOS 7.9 的 OpenSSL 1.0.2k 只到 TLS 1.2，对端若强制 TLS 1.3 就握手失败）。
#
#     bash deploy/install-docker.sh https://你的jenkins地址
#
# 容器只绑 127.0.0.1，外部访问仍走宿主机上的 nginx 反代，nginx 配置不用改。
set -euo pipefail

JENKINS_URL="${1:-}"
PORT="${PORT:-8770}"
NAME=jenkins-check
ENV_FILE=/etc/check-jenkins-release.env

if [[ $EUID -ne 0 ]]; then echo "请用 root 执行"; exit 1; fi
if [[ -z "$JENKINS_URL" ]]; then
    echo "用法: bash deploy/install-docker.sh https://你的jenkins地址"; exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    cat <<'EOF'
没装 docker。CentOS 7 安装参考：
    yum install -y yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    yum install -y docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
装好后重跑本脚本。
EOF
    exit 1
fi

echo "==> 写配置 $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
JENKINS_URL=$JENKINS_URL
JENKINS_MAX_BUILDS=30
# 多用户模式：每人在网页填自己的 Token，服务端不保存
JENKINS_ALLOW_SERVER_CREDS=0
EOF
    chmod 600 "$ENV_FILE"
else
    sed -i "s|^JENKINS_URL=.*|JENKINS_URL=$JENKINS_URL|" "$ENV_FILE"
    echo "    已存在，只更新了 JENKINS_URL"
fi
# 这两项由容器/端口映射决定，留在 env 里会互相打架
sed -i '/^JENKINS_WEB_HOST=/d;/^JENKINS_WEB_PORT=/d' "$ENV_FILE"

echo "==> 构建镜像"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker build -t "$NAME:latest" "$SRC"

echo "==> 先验证容器内能否握手成功（这正是宿主机做不到的那一步）"
if docker run --rm --env-file "$ENV_FILE" "$NAME:latest" python -c "
import os,ssl,urllib.request,sys
print('容器内 OpenSSL:', ssl.OPENSSL_VERSION)
u=os.environ['JENKINS_URL'].rstrip('/')+'/login'
r=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
try:
    with urllib.request.urlopen(r, timeout=30) as f: print('TLS 握手 OK, HTTP', f.status)
except urllib.error.HTTPError as e: print('TLS 握手 OK, HTTP', e.code)
except Exception as e: print('失败:', e); sys.exit(1)
"; then
    echo "    ✓ 容器内可以连通 Jenkins"
else
    echo "    ✗ 容器里也连不上，问题不在 TLS 版本，先排查网络/防火墙"
    exit 1
fi

echo "==> 启动容器（只绑 127.0.0.1，外部经 nginx 进来）"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart always \
    --env-file "$ENV_FILE" \
    -p "127.0.0.1:$PORT:8770" \
    "$NAME:latest"

# 之前用 systemd 直接跑的话会占着同一个端口，停掉
if systemctl is-active --quiet check-jenkins-release 2>/dev/null; then
    systemctl disable --now check-jenkins-release
    echo "    已停用旧的 systemd 服务（端口冲突）"
fi

sleep 3
if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    echo
    echo "==> 完成。健康检查 OK"
    echo "    日志:   docker logs -f $NAME"
    echo "    重启:   docker restart $NAME"
    echo "    更新:   git pull && bash deploy/install-docker.sh $JENKINS_URL"
    echo "    nginx 配置不用改，仍然是 proxy_pass http://127.0.0.1:$PORT"
else
    echo "!! 起来了但健康检查不过：docker logs $NAME"
    exit 1
fi
