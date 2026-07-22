#!/bin/bash
# 生成 nginx 用的 Cloudflare IP 白名单 + 真实 IP 恢复配置。
# 只有走「方案 B：开 443 + nginx」时才需要；用 Tunnel 的话不需要。
#
#     bash deploy/cloudflare/gen-cf-allow.sh && nginx -t && systemctl reload nginx
#
# CF 的 IP 段偶尔会变，建议挂个月度 cron 重跑。
set -euo pipefail

OUT_ALLOW=/etc/nginx/conf.d/cloudflare-allow.conf
OUT_REAL=/etc/nginx/conf.d/cloudflare-real-ip.conf
TMP_A=$(mktemp); TMP_R=$(mktemp)
trap 'rm -f "$TMP_A" "$TMP_R"' EXIT

if [[ $EUID -ne 0 ]]; then echo "请用 root 执行"; exit 1; fi

echo "# 由 gen-cf-allow.sh 生成，勿手改。$(date '+%F %T')" > "$TMP_A"
echo "# 由 gen-cf-allow.sh 生成，勿手改。$(date '+%F %T')" > "$TMP_R"

fetched=0
for url in https://www.cloudflare.com/ips-v4 https://www.cloudflare.com/ips-v6; do
    if body=$(curl -fsS --max-time 20 "$url"); then
        while read -r cidr; do
            [[ -z "$cidr" ]] && continue
            echo "allow $cidr;"            >> "$TMP_A"
            echo "set_real_ip_from $cidr;" >> "$TMP_R"
        done <<< "$body"
        fetched=$((fetched+1))
    else
        echo "!! 拉取失败: $url"
    fi
done

if [[ $fetched -eq 0 ]]; then
    echo "!! 一个都没拉到，保留原有配置不动"; exit 1
fi

# CF-Connecting-IP 是 Cloudflare 塞的访客真实 IP；不设这个，日志和限流看到的全是 CF 的 IP
echo "real_ip_header CF-Connecting-IP;" >> "$TMP_R"

install -m 0644 "$TMP_A" "$OUT_ALLOW"
install -m 0644 "$TMP_R" "$OUT_REAL"
echo "已生成 $OUT_ALLOW（$(grep -c '^allow' "$OUT_ALLOW") 条）和 $OUT_REAL"
echo "接着执行: nginx -t && systemctl reload nginx"
