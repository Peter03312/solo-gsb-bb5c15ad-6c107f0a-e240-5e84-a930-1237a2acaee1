#!/usr/bin/env bash
# 计划公开发布端到端示例（UTC 公开时刻 public_at）：
#   1. 单份：指定未来公开时刻 -> 到点前 404 -> 到点后自动可读（无需二次操作）
#   2. 批次：全批次统一公开时刻，发布响应与批次查询均返回 public_at
#   3. 非法时间（无时区/已过期/超过 30 天）-> 400，且不留版本
#   4. 幂等：同键同内容（含时间）原样重放；同键不同时间 -> 409
#   5. 撤回优先：公开前撤回 -> 410，时间到达也不恢复
# 依赖：curl、python3（仅用于解析 JSON、生成示例图与计算 UTC 时刻）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
SLEEP_BEFORE="${SLEEP_BEFORE:-3}"   # 计划公开距当前的秒数（脚本会 sleep 等其到点）

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/sched_single.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/sched_a.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/sched_b.png")
EOF

# UTC ISO8601 时刻助手：$1 = 距当前的秒数（可负），输出如 2026-10-05T08:30:00+00:00
at_offset() {
  python3 - "$1" <<'EOF'
import sys
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=int(sys.argv[1])))
      .replace(microsecond=0).isoformat())
EOF
}

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 单份：指定 ${SLEEP_BEFORE} 秒后的 UTC 公开时刻发布 =="
SINGLE=$(upload /tmp/sched_single.png)
PUB_AT=$(at_offset "$SLEEP_BEFORE")
echo "doc=$SINGLE  public_at=$PUB_AT"
curl -sS -X POST "$BASE/documents/$SINGLE/publish" \
  -H 'Content-Type: application/json' \
  -d "{\"public_at\":\"$PUB_AT\"}" | python3 -m json.tool

echo "-- 到点前：详情与图片均 404 publication_not_yet_public（不泄露遮盖/成品） --"
curl -sS -w " <- HTTP %{http_code}\n" "$BASE/publications/$SINGLE/1"
curl -sS -o /dev/null -w "image <- HTTP %{http_code}\n" \
  "$BASE/publications/$SINGLE/1/image"

echo "-- 省略 public_at（或空请求体）仍立即公开：发布另一份 --"
NOW_DOC=$(upload /tmp/sched_single.png)
curl -sS -X POST "$BASE/documents/$NOW_DOC/publish" \
  -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;b=json.load(sys.stdin);print('version',b['version'],'public_at',b['public_at'])"
curl -sS -o /dev/null -w "immediate doc readable <- HTTP %{http_code}\n" \
  "$BASE/publications/$NOW_DOC/1"

echo "== 2. 批次：统一公开时刻（+ $((SLEEP_BEFORE + 2)) 秒） =="
A=$(upload /tmp/sched_a.png)
B=$(upload /tmp/sched_b.png)
BATCH_AT=$(at_offset "$((SLEEP_BEFORE + 2))")
RESP=$(curl -sS -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"public_at\":\"$BATCH_AT\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":0},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}")
echo "$RESP" | python3 -m json.tool
BATCH_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")

echo "-- 批次查询：顶层与每条都返回 public_at --"
curl -sS "$BASE/publication-batches/$BATCH_ID" \
  | python3 -c "import sys,json;b=json.load(sys.stdin);print('batch public_at:',b['public_at']);[print(' ',d['doc_id'][:8],'v'+str(d['version']),d['public_at']) for d in b['documents']]"

echo "== 3. 非法时间 -> 400，不留下版本（下次发布仍是 v1） =="
curl -sS -w " <- HTTP %{http_code}\n" -X POST "$BASE/documents/$SINGLE/publish" \
  -H 'Content-Type: application/json' \
  -d '{"public_at":"2026-10-10T12:00:00"}'   # 朴素时间，缺时区
curl -sS -w " <- HTTP %{http_code}\n" -X POST "$BASE/documents/$SINGLE/publish" \
  -H 'Content-Type: application/json' \
  -d "{\"public_at\":\"$(at_offset -60)\"}"   # 已在过去
curl -sS -w " <- HTTP %{http_code}\n" -X POST "$BASE/documents/$SINGLE/publish" \
  -H 'Content-Type: application/json' \
  -d "{\"public_at\":\"$(at_offset $((31*24*3600)))\"}"  # 超过 30 天

echo "== 4. 幂等：public_at 纳入幂等内容 =="
KEY="sched-$(date +%s)"
KEY_AT=$(at_offset 3600)
KEY_BODY="{\"idempotency_key\":\"$KEY\",\"public_at\":\"$KEY_AT\",\"documents\":[
  {\"doc_id\":\"$A\",\"expected_revision\":0},{\"doc_id\":\"$B\",\"expected_revision\":0}]}"
# 注意：A/B 已在上面的批次里发布为 v1；这里用新文档演示键语义
C=$(upload /tmp/sched_a.png); D=$(upload /tmp/sched_b.png)
KEY_BODY="{\"idempotency_key\":\"$KEY\",\"public_at\":\"$KEY_AT\",\"documents\":[
  {\"doc_id\":\"$C\",\"expected_revision\":0},{\"doc_id\":\"$D\",\"expected_revision\":0}]}"
echo "-- 首次 201 --"
curl -sS -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "$KEY_BODY" | python3 -c "import sys,json;print('batch:',json.load(sys.stdin)['batch_id'])"
echo "-- 同键同内容重试 200（原样返回，即使时间已过也不重新校验） --"
curl -sS -o /dev/null -w "replay <- HTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' -d "$KEY_BODY"
echo "-- 同键不同时间 409 idempotency_key_conflict --"
curl -sS -w " <- HTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY\",\"public_at\":\"$(at_offset 7200)\",\"documents\":[
    {\"doc_id\":\"$C\",\"expected_revision\":0},{\"doc_id\":\"$D\",\"expected_revision\":0}]}"

echo "== 5. 撤回优先：对单份计划版本在公开前撤回 -> 410，到期不恢复 =="
WD=$(upload /tmp/sched_single.png)
W_AT=$(at_offset "$SLEEP_BEFORE")
curl -sS -X POST "$BASE/documents/$WD/publish" \
  -H 'Content-Type: application/json' -d "{\"public_at\":\"$W_AT\"}" >/dev/null
curl -sS -X POST "$BASE/documents/$WD/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"公开前复查发现遮盖遗漏"}' \
  | python3 -c "import sys,json;b=json.load(sys.stdin);print('withdrawn:',b['withdrawn'],b['withdrawn_at'])"
curl -sS -o /dev/null -w "before due <- HTTP %{http_code}\n" \
  "$BASE/publications/$WD/1"

echo "== 6. 等待计划时刻到点：未撤回版本自动可读；已撤回版本仍 410 =="
echo "sleep $((SLEEP_BEFORE + 3)) ..."
sleep "$((SLEEP_BEFORE + 3))"
curl -sS -o /dev/null -w "single v1 detail  <- HTTP %{http_code}\n" "$BASE/publications/$SINGLE/1"
curl -sS -o /dev/null -w "single v1 image   <- HTTP %{http_code}\n" "$BASE/publications/$SINGLE/1/image"
curl -sS -o /dev/null -w "batch A v1 image  <- HTTP %{http_code}\n" "$BASE/publications/$A/1/image"
curl -sS -o /dev/null -w "batch B v1 image  <- HTTP %{http_code}\n" "$BASE/publications/$B/1/image"
curl -sS -o /dev/null -w "withdrawn v1      <- HTTP %{http_code} (期望 410)\n" "$BASE/publications/$WD/1"
