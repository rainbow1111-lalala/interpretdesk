#!/usr/bin/env bash
# 打一个可上传的部署包。绝不包含 data/：那里是会议底稿、API key 和会议记录。
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=/tmp/interpretdesk-deploy.tar.gz

echo "== 构建前端 =="
(cd web && npm run build)

echo "== 打包 =="
rm -f "$OUT"
tar -czf "$OUT" \
    --exclude='data' --exclude='.venv' --exclude='node_modules' --exclude='.git' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
    --exclude='web/src' --exclude='web/node_modules' \
    --exclude='server/tests/fixtures' \
    server web/dist deploy pyproject.toml README.md

echo "== 包内清单（确认没有 data/、没有 settings.json）=="
tar -tzf "$OUT" | grep -iE "data/|settings.json|\.env|meetings.db" && {
    echo "包里有不该有的东西，已中止"; rm -f "$OUT"; exit 1; } || echo "  干净"
echo
echo "共 $(tar -tzf "$OUT" | wc -l | tr -d ' ') 个文件，$(du -h "$OUT" | cut -f1)"
echo "包在 $OUT"
