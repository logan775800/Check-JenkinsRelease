# 用途：绕开老系统的 TLS 限制。
# CentOS 7.9 自带 OpenSSL 1.0.2k，最高只到 TLS 1.2；若 Jenkins 前面的 WAF 只接受 TLS 1.3，
# 宿主机上无论 python 还是 curl 都握手失败。容器镜像自带新版 OpenSSL（3.x，支持 TLS 1.3），
# 换个壳就通了，不用动宿主机的系统库。
#
# 本服务只用 Python 标准库，没有任何第三方依赖，所以不需要 pip install。
FROM python:3.12-alpine

WORKDIR /app
COPY app.py /app/app.py

# 不用 root 跑
RUN adduser -D -H -u 10001 appuser
USER appuser

# 容器内固定听 0.0.0.0；对外暴露范围由 docker -p 绑定 127.0.0.1 来控制
ENV JENKINS_WEB_HOST=0.0.0.0 \
    JENKINS_WEB_PORT=8770 \
    JENKINS_ALLOW_SERVER_CREDS=0

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8770/healthz || exit 1

CMD ["python", "/app/app.py"]
