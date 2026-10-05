"""计划公开（UTC 公开时刻）测试：

- 单份：省略立即公开；指定时刻后自动可读；公开前 404 不泄露；
  非法/过期/过远时间拒绝且不占版本号；版本号在发布当下即占用。
- 批次：统一 public_at 返回与门控；任一时间/文档失败整批不留痕；
  public_at 纳入幂等内容（同键同内容原样重放不重新校验；同键不同时间冲突）。
- 撤回优先：计划公开期间撤回 -> 410，时间到达不恢复；已撤回到期仍 410。
"""
import hashlib
import io
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F402  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _iso(dt):
    return dt.isoformat()


def _in(seconds=0):
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _annotate(client, doc_id, revision=0,
              rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


# ---------------- 单份：立即公开（兼容） ----------------

def test_publish_without_body_publishes_immediately(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    # 无请求体（旧调用方式）
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201
    body = r.json()
    assert body["public_at"] == body["created_at"]
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 200


def test_publish_explicit_null_body_publishes_immediately(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.post(f"/documents/{doc_id}/publish", json={"public_at": None})
    assert r.status_code == 201
    body = r.json()
    assert body["public_at"] == body["created_at"]


def test_timezone_offset_normalized_to_utc(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    # 显式 +00:00 偏移与 Z 等价
    target = datetime.now(timezone.utc) + timedelta(hours=1)
    r = client.post(
        f"/documents/{doc_id}/publish",
        json={"public_at": target.replace(microsecond=0).isoformat()},
    )
    assert r.status_code == 201
    assert r.json()["public_at"].endswith("+00:00")


# ---------------- 单份：计划公开门控 ----------------

def test_scheduled_publish_blocks_then_opens_without_any_action(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    r = client.post(f"/documents/{doc_id}/publish",
                    json={"public_at": target.isoformat()})
    assert r.status_code == 201
    pub = r.json()
    assert pub["version"] == 1
    assert pub["public_at"] == target.replace(microsecond=0).isoformat()

    # 到点前：详情与图片均 404，且不泄露遮盖信息/摘要/成品
    for path in (f"/publications/{doc_id}/1",
                 f"/publications/{doc_id}/1/image"):
        blocked = client.get(path)
        assert blocked.status_code == 404
        assert blocked.json()["detail"]["code"] == "publication_not_yet_public"
        assert "redactions" not in blocked.json()
        assert blocked.headers.get("content-type", "").startswith("application/json")
    # 图片 404 响应体不得是 PNG 字节
    assert not client.get(f"/publications/{doc_id}/1/image").content.startswith(b"\x89PNG")

    # 档案员视角列表能看到该版本及其公开时刻
    listed = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert listed[0]["version"] == 1
    assert listed[0]["public_at"] == pub["public_at"]

    # 到点后无需任何操作即可读取（含图片）
    time.sleep(1.1)
    detail = client.get(f"/publications/{doc_id}/1")
    assert detail.status_code == 200
    assert detail.json()["public_at"] == pub["public_at"]
    img = client.get(f"/publications/{doc_id}/1/image")
    assert img.status_code == 200
    assert hashlib.sha256(img.content).hexdigest() == pub["output_sha256"]


def test_scheduled_version_consumes_normal_version_number_at_publish_time(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    target = datetime.now(timezone.utc) + timedelta(hours=1)
    client.post(f"/documents/{doc_id}/publish",
                json={"public_at": target.isoformat()})  # v1 计划中
    # 再发布立即拿 v2（v1 发布当下已占号），且 v2 立即可读而 v1 仍 404
    r2 = client.post(f"/documents/{doc_id}/publish")
    assert r2.status_code == 201 and r2.json()["version"] == 2
    assert client.get(f"/publications/{doc_id}/2").status_code == 200
    assert client.get(f"/publications/{doc_id}/1").status_code == 404
    # 成品文件在发布当下已固化
    settings, db = _db(client)
    path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id=? AND version=1",
        (doc_id,)).fetchone()["output_path"]
    db.close()
    assert os.path.exists(path)


def test_scheduled_publication_is_immutable(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0,
              rects=[{"x": 0, "y": 0, "w": 8, "h": 8, "reason": "旧标注"}])
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    v1 = client.post(f"/documents/{doc_id}/publish",
                     json={"public_at": target.isoformat()}).json()
    _annotate(client, doc_id, 1,
              rects=[{"x": 30, "y": 20, "w": 5, "h": 5, "reason": "新标注"}])
    client.post(f"/documents/{doc_id}/publish")  # v2

    time.sleep(1.1)
    meta = client.get(f"/publications/{doc_id}/1").json()
    assert meta["annotation_revision"] == 1
    assert meta["redactions"] == [
        {"x": 0, "y": 0, "w": 8, "h": 8, "reason": "旧标注"}]
    img = client.get(f"/publications/{doc_id}/1/image").content
    assert hashlib.sha256(img).hexdigest() == v1["output_sha256"]


# ---------------- 单份：时间校验失败不留版本 ----------------

@pytest.mark.parametrize("bad", [
    "not-a-timestamp",
    "2026-13-99T00:00:00Z",
    "2026-10-10T12:00:00",     # 朴素时间（无时区）
    "2026/10/10 12:00:00Z",
    "",
    "   ",
])
def test_invalid_public_at_rejected(client, bad):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.post(f"/documents/{doc_id}/publish", json={"public_at": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_public_at"
    _assert_no_versions(client, doc_id)


def test_public_at_in_the_past_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = client.post(f"/documents/{doc_id}/publish",
                    json={"public_at": past.isoformat()})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "public_at_not_in_future"
    _assert_no_versions(client, doc_id)


def test_public_at_exactly_now_rejected_must_be_strictly_later(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.post(f"/documents/{doc_id}/publish",
                    json={"public_at": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "public_at_not_in_future"


def test_public_at_more_than_30_days_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    far = datetime.now(timezone.utc) + timedelta(days=30, seconds=1)
    r = client.post(f"/documents/{doc_id}/publish",
                    json={"public_at": far.isoformat()})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "public_at_too_far_ahead"
    _assert_no_versions(client, doc_id)


def test_public_at_30_days_boundary_accepted(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    edge = datetime.now(timezone.utc) + timedelta(days=30)
    r = client.post(f"/documents/{doc_id}/publish",
                    json={"public_at": edge.isoformat()})
    assert r.status_code == 201


def test_invalid_time_unknown_document_does_not_mask_validation(client):
    # 时间格式错误先于文档存在性返回 400（不泄露文档是否存在）
    r = client.post(f"/documents/{'0' * 32}/publish",
                    json={"public_at": "nonsense"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_public_at"


def _assert_no_versions(client, doc_id):
    _, db = _db(client)
    n = db.execute("SELECT COUNT(*) c FROM publications WHERE doc_id=?",
                   (doc_id,)).fetchone()["c"]
    db.close()
    assert n == 0
    # 修正后仍从 v1 开始（失败未占号）
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201 and r.json()["version"] == 1


# ---------------- 撤回优先于公开时间 ----------------

def test_withdraw_scheduled_version_blocks_even_after_time_arrives(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    pub = client.post(f"/documents/{doc_id}/publish",
                      json={"public_at": target.isoformat()}).json()

    # 公开前撤回：先看到 404（未公开），撤回后按既有约定 410
    assert client.get(f"/publications/{doc_id}/1").status_code == 404
    r = client.post(f"/documents/{doc_id}/publications/1/withdraw",
                    json={"reason": "公开前发现遮盖遗漏"})
    assert r.status_code == 201

    for path in (f"/publications/{doc_id}/1",
                 f"/publications/{doc_id}/1/image"):
        blocked = client.get(path)
        assert blocked.status_code == 410
        assert blocked.json()["detail"]["code"] == "publication_withdrawn"

    # 时间到达也不得恢复访问
    time.sleep(1.1)
    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410

    # 档案员列表仍保留记录、撤回状态与公开时刻
    p = client.get(f"/documents/{doc_id}/publications").json()["publications"][0]
    assert p["withdrawn"] is True
    assert p["public_at"] == pub["public_at"]


def test_withdraw_immediately_public_version_still_410_at_scheduled_time(client):
    # v1 立即公开后撤回；即使其 public_at（=created_at）早已到达，仍 410
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.post(f"/documents/{doc_id}/publish")
    client.post(f"/documents/{doc_id}/publications/1/withdraw",
                json={"reason": "x"})
    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410


# ---------------- 批次：统一公开时刻 ----------------

def _batch(client, pairs, public_at=None, key=None):
    body = {"documents": [{"doc_id": d, "expected_revision": r}
                          for d, r in pairs]}
    if public_at is not None:
        body["public_at"] = public_at
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/publication-batches", json=body)


def test_batch_scheduled_publish_returns_and_gates_public_at(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    r = _batch(client, [(a, 0), (b, 0)], public_at=target.isoformat())
    assert r.status_code == 201
    body = r.json()
    expected = target.replace(microsecond=0).isoformat()
    assert body["public_at"] == expected
    assert [d["public_at"] for d in body["documents"]] == [expected, expected]

    # 批次查询返回公开时刻（顶层 + 每条）
    got = client.get(f"/publication-batches/{body['batch_id']}").json()
    assert got["public_at"] == expected
    assert [d["public_at"] for d in got["documents"]] == [expected, expected]

    # 到点前整批每份都 404（详情+图片）
    for d in (a, b):
        assert client.get(f"/publications/{d}/1").status_code == 404
        assert client.get(f"/publications/{d}/1/image").status_code == 404

    # 到点后整批可读，无需二次操作
    time.sleep(1.1)
    for d in (a, b):
        assert client.get(f"/publications/{d}/1").status_code == 200
        assert client.get(f"/publications/{d}/1/image").status_code == 200


def test_batch_without_public_at_publishes_immediately(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (b, 0)])
    assert r.status_code == 201
    assert r.json()["public_at"] == r.json()["created_at"]
    assert client.get(f"/publications/{a}/1").status_code == 200


def test_batch_invalid_public_at_fails_without_trace(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (b, 0)], public_at="garbage")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_public_at"
    _assert_batch_clean(client, [a, b])


def test_batch_past_public_at_fails_without_trace(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = _batch(client, [(a, 0), (b, 0)], public_at=past)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "public_at_not_in_future"
    _assert_batch_clean(client, [a, b])


def test_batch_far_public_at_fails_without_trace(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    far = (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()
    r = _batch(client, [(a, 0), (b, 0)], public_at=far)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "public_at_too_far_ahead"
    _assert_batch_clean(client, [a, b])


def test_batch_business_failure_with_future_time_leaves_no_trace(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    missing = "0" * 32
    target = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = _batch(client, [(a, 0), (missing, 0)], public_at=target)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"
    _assert_batch_clean(client, [a])
    # 修正后 a 仍从 v1 开始
    other = upload(client, png_bytes()).json()["doc_id"]
    r2 = _batch(client, [(a, 0), (other, 0)], public_at=target)
    assert r2.status_code == 201
    assert [d["version"] for d in r2.json()["documents"]] == [1, 1]


def _assert_batch_clean(client, doc_ids):
    """失败请求不得留下版本、批次、成品文件或幂等键。"""
    settings, db = _db(client)
    for d in doc_ids:
        assert db.execute(
            "SELECT COUNT(*) c FROM publications WHERE doc_id=?",
            (d,)).fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency").fetchone()["c"] == 0
    db.close()


# ---------------- 批次幂等：公开时刻纳入幂等内容 ----------------

def test_batch_replay_same_key_same_time_replays_even_after_time_passed(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    body = {
        "idempotency_key": "k-sched",
        "public_at": target.isoformat(),
        "documents": [
            {"doc_id": a, "expected_revision": 0},
            {"doc_id": b, "expected_revision": 0},
        ],
    }
    r1 = client.post("/publication-batches", json=body)
    assert r1.status_code == 201
    first = r1.json()

    # 时间已过：同键同内容原样重放，不重新校验（若校验必因过去时间 400）
    time.sleep(1.1)
    assert client.get(f"/publications/{a}/1").status_code == 200
    r2 = client.post("/publication-batches", json=body)
    assert r2.status_code == 200
    assert r2.json() == first

    # 没有产生第二批版本
    _, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 1
    for d in (a, b):
        assert db.execute(
            "SELECT COUNT(*) c FROM publications WHERE doc_id=?",
            (d,)).fetchone()["c"] == 1
    db.close()


def test_batch_same_key_different_time_conflicts(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    t1 = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    t2 = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    r1 = _batch(client, [(a, 0), (b, 0)], public_at=t1, key="k-time")
    assert r1.status_code == 201

    # 同键、文档相同，但时间不同 -> 冲突
    r = _batch(client, [(a, 0), (b, 0)], public_at=t2, key="k-time")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"

    # 省略 public_at 与显式给出时间也属于不同内容 -> 冲突
    r = _batch(client, [(a, 0), (b, 0)], key="k-time")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"

    # 原批次公开时刻不变
    got = client.get(f"/publication-batches/{r1.json()['batch_id']}").json()
    assert got["public_at"] == r1.json()["public_at"]


def test_batch_invalid_time_does_not_occupy_key(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (b, 0)], public_at="nope", key="k-iv")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_public_at"
    _, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency").fetchone()["c"] == 0
    db.close()

    # 原键 + 合法时间 -> 首次成功
    target = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r2 = _batch(client, [(a, 0), (b, 0)], public_at=target, key="k-iv")
    assert r2.status_code == 201


def test_batch_replay_accepts_same_instant_in_different_offset_notation(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    # 同一 UTC 瞬时：首次用 +00:00 表示，重试用 +08:00 表示（相差 8 小时）
    instant = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    z = instant.isoformat()                              # ...+00:00
    offset8 = instant.astimezone(timezone(timedelta(hours=8))).isoformat()
    assert z != offset8

    r1 = _batch(client, [(a, 0), (b, 0)], public_at=z, key="k-tz")
    assert r1.status_code == 201
    r2 = _batch(client, [(a, 0), (b, 0)], public_at=offset8, key="k-tz")
    assert r2.status_code == 200
    assert r2.json() == r1.json()  # 原样重放首次结果（含 UTC 形式的 public_at）

    # 相差 1 秒即为不同瞬时 -> 冲突
    other = (instant + timedelta(seconds=1)).isoformat()
    r3 = _batch(client, [(a, 0), (b, 0)], public_at=other, key="k-tz")
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "idempotency_key_conflict"


# ---------------- 批次 + 撤回优先 ----------------

def test_withdraw_scheduled_batch_version_remains_gone_after_due(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    target = datetime.now(timezone.utc) + timedelta(seconds=1)
    r = _batch(client, [(a, 0), (b, 0)], public_at=target.isoformat())
    assert r.status_code == 201
    bid = r.json()["batch_id"]

    # 公开前撤回 a
    rw = client.post(f"/documents/{a}/publications/1/withdraw",
                     json={"reason": "批次内撤回"})
    assert rw.status_code == 201
    assert client.get(f"/publications/{a}/1").status_code == 410
    assert client.get(f"/publications/{b}/1").status_code == 404

    time.sleep(1.1)
    # 到点：b 可读，a 永久 410 不恢复；批次查询仍带公开时刻与撤回标识
    assert client.get(f"/publications/{b}/1").status_code == 200
    assert client.get(f"/publications/{b}/1/image").status_code == 200
    assert client.get(f"/publications/{a}/1").status_code == 410
    assert client.get(f"/publications/{a}/1/image").status_code == 410
    docs = client.get(f"/publication-batches/{bid}").json()["documents"]
    assert docs[0]["withdrawn"] is True
    assert docs[1]["withdrawn"] is False
    assert all(d["public_at"] for d in docs)


# ---------------- 兼容：字段追加不破坏原字段顺序 ----------------

def test_listing_entries_keep_original_prefix_and_append_public_at(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    client.post(f"/documents/{doc_id}/publish",
                json={"public_at": (
                    datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})

    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert list(pubs[0].keys())[:5] == [
        "version", "annotation_revision", "original_sha256",
        "output_sha256", "created_at"]
    assert list(pubs[0].keys())[-4:] == [
        "withdrawn", "withdrawn_at", "withdrawal_reason", "public_at"]

    detail = client.get(f"/documents/{doc_id}").json()["publications"]
    assert list(detail[0].keys())[:4] == [
        "version", "annotation_revision", "output_sha256", "created_at"]
    assert detail[0]["withdrawn"] is False


# ---------------- 并发：计划公开与标注/发布互斥不被破坏 ----------------

def test_concurrent_scheduled_and_immediate_publish_versions_contiguous(client):
    doc_id = upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
    target = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    errors = []

    def scheduled():
        r = client.post(f"/documents/{doc_id}/publish",
                        json={"public_at": target})
        if r.status_code != 201:
            errors.append(f"scheduled: {r.status_code} {r.text}")

    def immediate():
        for _ in range(5):
            r = client.post(f"/documents/{doc_id}/publish")
            if r.status_code != 201:
                errors.append(f"immediate: {r.status_code} {r.text}")

    threads = [threading.Thread(target=scheduled)] + [
        threading.Thread(target=immediate) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    _, db = _db(client)
    versions = [r["version"] for r in db.execute(
        "SELECT version FROM publications WHERE doc_id=? ORDER BY version",
        (doc_id,))]
    db.close()
    assert versions == list(range(1, 12))
    # 计划版本未到点仍 404；其余立即可读
    meta = client.get(f"/publications/{doc_id}/1")
    assert meta.status_code in (200, 404)
