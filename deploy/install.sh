#!/usr/bin/env bash
# 在服务器上以 root 执行一次。装依赖、建服务、起 nginx。
# 证书单独一步（见 README-deploy.md），因为它要等域名解析先指过来。
set -euo pipefail

APP=/opt/interpretdesk
USER=interpretdesk

echo "== 1/6 系统依赖 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx curl ca-certificates python3-venv python3-pip \
    poppler-utils >/dev/null
# poppler-utils 供扫描件 PDF 的文字提取降级用，没有它只是这一档失效，主流程不受影响

echo "== 2/6 Python 版本 =="
PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    echo "系统 python3 低于 3.11，装 3.12"
    apt-get install -y -qq software-properties-common >/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1
    apt-get update -qq
    apt-get install -y -qq python3.12 python3.12-venv >/dev/null
    PY=python3.12
fi
echo "用 $($PY -V)"

echo "== 3/6 应用用户与目录 =="
id -u $USER >/dev/null 2>&1 || useradd --system --home $APP --shell /usr/sbin/nologin $USER
mkdir -p $APP/data
# 解包后的代码应已在 $APP，这里只校正属主。data 里是用户上传的会议材料，权限收紧
chown -R $USER:$USER $APP
chmod 750 $APP/data

echo "== 4/6 Python 依赖 =="
$PY -m venv $APP/.venv
$APP/.venv/bin/pip install -q --upgrade pip
$APP/.venv/bin/pip install -q fastapi "uvicorn[standard]" websockets httpx \
    python-multipart pypdf python-docx
chown -R $USER:$USER $APP/.venv

echo "== 5/6 系统服务 =="
cp $APP/deploy/interpretdesk.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now interpretdesk
sleep 3
systemctl is-active --quiet interpretdesk && echo "服务已启动" || {
    echo "服务没起来，看日志："; journalctl -u interpretdesk -n 30 --no-pager; exit 1; }

echo "== 6/6 nginx =="
mkdir -p /var/www/certbot
# 证书还没有，先只放 80 端口那段，否则 nginx 会因为找不到证书而起不来
cat > /etc/nginx/sites-available/interpretdesk <<'PRE'
server {
    listen 80;
    listen [::]:80;
    server_name interpretdesk.com www.interpretdesk.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 7200s;
        proxy_send_timeout 7200s;
        proxy_buffering off;
        client_max_body_size 25m;
    }
}
PRE
ln -sf /etc/nginx/sites-available/interpretdesk /etc/nginx/sites-enabled/interpretdesk
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo
echo "装好了。现在用 IP 直接访问应该能看到界面："
echo "  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/api/health"
curl -s -o /dev/null -w "  本机自测返回：%{http_code}\n" http://127.0.0.1/api/health
echo
echo "下一步：把域名解析指过来，再签证书（见 deploy/README-deploy.md）"
