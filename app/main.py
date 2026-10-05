"""HTTP API：上传、标注（乐观锁修订）、发布（事务内涂实）、撤回、公开读取。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .config import Settings
from .db import init_db, read_tx, write_tx
from .imaging import (
    SUPPORTED_FORMATS,
    ImageDecodeError,
    UnsupportedFormatError,
    load_oriented,
    render_publication,
)
from .models import (
    AnnotationsPut,
    BatchPublishRequest,
    SinglePublishRequest,
    WithdrawRequest,
)

MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}

# 计划公开时刻允许提前的最大天数（首次提交时不超过 30 天）
MAX_PUBLIC_AHEAD = timedelta(days=30)


def _now() -> datetime:
    """当前时刻（感知 UTC）。公开时刻的校验与公开门控统一以此为时钟。"""
    return datetime.now(timezone.utc)


def _utcnow() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_public_at(raw: str | None) -> datetime | None:
    """解析调用方提供的公开时刻：须为 ISO 8601 且显式携带时区。

    成功后统一换算为感知 UTC 时刻；省略/None 表示立即公开（返回 None）；
    格式非法或缺少时区信息时抛 invalid_public_at（400）。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        _error(400, "invalid_public_at", "public_at must be a non-empty ISO 8601"
               " timestamp with explicit timezone (e.g."
               " '2026-10-10T12:00:00Z')")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _error(
            400,
            "invalid_public_at",
            f"public_at '{raw}' is not a valid ISO 8601 timestamp; use an"
            " explicit timezone such as '2026-10-10T12:00:00Z'",
        )
    if dt.tzinfo is None:
        _error(
            400,
            "invalid_public_at",
            "public_at must carry explicit timezone information"
            " (e.g. append 'Z' or an offset like '+08:00')",
        )
    return dt.astimezone(timezone.utc)


def _validate_public_at_window(
    public_at: datetime | None, now: datetime | None = None
) -> datetime | None:
    """校验时间窗：晚于当前时刻且不超过 30 天；立即公开（None）原样返回。"""
    if public_at is None:
        return None
    now = now or _now()
    if public_at <= now:
        _error(
            400,
            "public_at_not_in_future",
            "public_at must be strictly later than the current time"
            f" (now {now.isoformat(timespec='seconds')})",
        )
    if public_at > now + MAX_PUBLIC_AHEAD:
        _error(
            400,
            "public_at_too_far_ahead",
            "public_at must not be more than 30 days in the future"
            f" (now {now.isoformat(timespec='seconds')})",
        )
    return public_at


def _public_at_text(public_at: datetime | None) -> str:
    """落库/响应的公开时刻：立即公开取当前时刻，计划公开保留输入时刻；
    统一截到秒精度，与 created_at 的时间表示一致。"""
    return (public_at or _now()).replace(microsecond=0).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _batch_request_hash(items: Any, public_at: datetime | None) -> str:
    """幂等键绑定的内容摘要：按请求顺序排列的 (doc_id, expected_revision)
    及统一公开时刻。

    公开时刻按归一化后的 UTC 秒级瞬时参与摘要：省略（立即公开）记为 None，
    与显式给出任何时刻都不同；同一时刻的不同时区写法（如 Z 与 +08:00
    表示同一瞬时）视为相同内容，可正常重放。
    """
    public_at_key = (
        public_at.replace(microsecond=0).isoformat() if public_at is not None else None
    )
    canonical = json.dumps(
        {
            "documents": [
                {"doc_id": it.doc_id, "expected_revision": it.expected_revision}
                for it in items
            ],
            "public_at": public_at_key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _error(status: int, code: str, message: str, **extra: Any) -> None:
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def _rect_out(row: Any) -> dict:
    return {
        "x": row["x"],
        "y": row["y"],
        "w": row["w"],
        "h": row["h"],
        "reason": row["reason"],
    }


def _pub_entry(row: Any, keys: list[str]) -> dict:
    """发布记录条目：原字段按序输出，再追加撤回状态三字段与公开时刻。

    字段顺序契约：原字段（keys）保持不变在前；撤回三字段紧随其后；
    public_at 追加在最末（旧字段顺序不受影响，新字段均向后追加）。
    查询须以 LEFT JOIN 带出 w_at / w_reason 两列并选出 p.public_at
    （见各列表端点）。
    """
    out = {k: row[k] for k in keys}
    out["withdrawn"] = row["w_at"] is not None
    out["withdrawn_at"] = row["w_at"]
    out["withdrawal_reason"] = row["w_reason"]
    out["public_at"] = row["public_at"]
    return out


# 列表查询共用的撤回状态联表片段
_WITHDRAWAL_JOIN = (
    " LEFT JOIN publication_withdrawals w"
    " ON w.doc_id = {t}.doc_id AND w.version = {t}.version"
)
_WITHDRAWAL_COLS = ", w.withdrawn_at AS w_at, w.reason AS w_reason"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        os.makedirs(settings.originals_dir, exist_ok=True)
        os.makedirs(settings.publications_dir, exist_ok=True)
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="档案影像脱敏发布服务",
        version="1.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    def _get_doc(conn: Any, doc_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            _error(404, "document_not_found", f"document '{doc_id}' does not exist")
        return row

    def _get_publication(conn: Any, doc_id: str, version: int) -> Any:
        """取发布记录；未知文档/版本按既有的不存在错误返回。"""
        pub = conn.execute(
            "SELECT * FROM publications WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if pub is None:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if exists is None:
                _error(
                    404, "document_not_found", f"document '{doc_id}' does not exist"
                )
            _error(
                404,
                "unknown_version",
                f"document '{doc_id}' has no publication version {version}",
            )
        return pub

    def _ensure_not_withdrawn(conn: Any, doc_id: str, version: int) -> None:
        """已撤回版本对公开面不可见（410），不返回成品与遮盖详情。

        撤回只登记记录、不删除成品文件：生效前已开始的下载因此能够
        完成，只有新发起的公开读取被拒绝。
        """
        row = conn.execute(
            "SELECT 1 FROM publication_withdrawals"
            " WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if row is not None:
            _error(
                410,
                "publication_withdrawn",
                f"document '{doc_id}' publication version {version} has been"
                " withdrawn and is no longer publicly available",
            )

    def _ensure_public_available(conn: Any, pub: Any) -> None:
        """公开面门控：永久撤回优先（410），其次检查计划公开时刻（404）。

        未到公开时刻的版本其存在、遮盖信息与成品文件都不得泄露：
        统一按"不存在"返回 404 publication_not_yet_public；
        已撤回版本即使已到/超过公开时刻仍返回 410，时间到达不恢复访问。
        """
        doc_id, version = pub["doc_id"], pub["version"]
        _ensure_not_withdrawn(conn, doc_id, version)
        public_at = datetime.fromisoformat(pub["public_at"])
        if _now() < public_at:
            _error(
                404,
                "publication_not_yet_public",
                f"document '{doc_id}' publication version {version} is not yet"
                " publicly available",
            )

    # ---------------- 基本 ----------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---------------- 档案上传 ----------------

    @app.post("/documents", status_code=201)
    async def upload_document(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        if not data:
            _error(400, "empty_upload", "uploaded file is empty")
        if len(data) > settings.max_upload_bytes:
            _error(
                413,
                "payload_too_large",
                f"file is {len(data)} bytes, limit is {settings.max_upload_bytes}",
            )
        try:
            oriented, fmt = load_oriented(data)
        except ImageDecodeError as exc:
            _error(400, "image_decode_failed", f"cannot decode image: {exc}")
        except UnsupportedFormatError as exc:
            _error(
                415,
                "unsupported_media_type",
                f"only PNG and JPEG are supported, got format: {exc}",
            )

        doc_id = uuid.uuid4().hex
        digest = _sha256_bytes(data)
        original_path = os.path.abspath(
            os.path.join(settings.originals_dir, doc_id + SUPPORTED_FORMATS[fmt])
        )
        with open(original_path, "wb") as f:
            f.write(data)

        width, height = oriented.size
        now = _utcnow()
        try:
            with write_tx(settings.db_path) as conn:
                conn.execute(
                    "INSERT INTO documents (id, filename, media_type, original_path,"
                    " original_sha256, width, height, annotation_revision, created_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?)",
                    (
                        doc_id,
                        file.filename or "",
                        MEDIA_TYPES[fmt],
                        original_path,
                        digest,
                        width,
                        height,
                        now,
                    ),
                )
        except Exception:
            if os.path.exists(original_path):
                os.unlink(original_path)
            raise
        return {
            "doc_id": doc_id,
            "filename": file.filename or "",
            "media_type": MEDIA_TYPES[fmt],
            "width": width,
            "height": height,
            "original_sha256": digest,
            "annotation_revision": 0,
            "created_at": now,
        }

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.output_sha256,"
                " p.created_at, p.public_at" + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc["id"],
            "filename": doc["filename"],
            "media_type": doc["media_type"],
            "width": doc["width"],
            "height": doc["height"],
            "original_sha256": doc["original_sha256"],
            "annotation_revision": doc["annotation_revision"],
            "created_at": doc["created_at"],
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "output_sha256",
                        "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 标注（修订号乐观锁） ----------------

    @app.get("/documents/{doc_id}/annotations")
    def get_annotations(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, doc["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "revision": doc["annotation_revision"],
            "rects": [_rect_out(r) for r in rects],
        }

    @app.put("/documents/{doc_id}/annotations")
    def put_annotations(doc_id: str, body: AnnotationsPut) -> dict:
        with write_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            current = doc["annotation_revision"]
            if body.revision != current:
                _error(
                    409,
                    "stale_revision",
                    f"annotation revision mismatch: expected {current},"
                    f" got {body.revision}; re-fetch and retry",
                    current_revision=current,
                )
            width, height = doc["width"], doc["height"]
            for i, r in enumerate(body.rects):
                if r.w <= 0 or r.h <= 0:
                    _error(
                        400,
                        "empty_rectangle",
                        f"rect #{i} has non-positive size (w={r.w}, h={r.h})",
                    )
                if r.x < 0 or r.y < 0 or r.x + r.w > width or r.y + r.h > height:
                    _error(
                        400,
                        "rectangle_out_of_bounds",
                        f"rect #{i} (x={r.x}, y={r.y}, w={r.w}, h={r.h}) exceeds"
                        f" image bounds {width}x{height}",
                    )
                if not r.reason.strip():
                    _error(
                        400,
                        "reason_required",
                        f"rect #{i} must carry a non-empty reason",
                    )
            new_revision = current + 1
            for idx, r in enumerate(body.rects):
                conn.execute(
                    "INSERT INTO annotation_rects"
                    " (doc_id, revision, idx, x, y, w, h, reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        new_revision,
                        idx,
                        r.x,
                        r.y,
                        r.w,
                        r.h,
                        r.reason.strip(),
                    ),
                )
            conn.execute(
                "UPDATE documents SET annotation_revision = ? WHERE id = ?",
                (new_revision, doc_id),
            )
        return {
            "doc_id": doc_id,
            "revision": new_revision,
            "rects": [r.model_dump() for r in body.rects],
        }

    # ---------------- 发布 ----------------

    @app.post("/documents/{doc_id}/publish", status_code=201)
    def publish_document(
        doc_id: str, body: SinglePublishRequest | None = None
    ) -> dict:
        # 公开时刻先于事务解析校验：非法时间直接 400，不渲染、不留版本。
        public_at_dt = _validate_public_at_window(
            _parse_public_at(body.public_at if body is not None else None)
        )
        output_path: str | None = None
        try:
            # 整个"读修订+读矩形+渲染+落盘+写记录"在一个写事务内，
            # 与标注修改互斥，保证成品与记录的修订号一致。
            with write_tx(settings.db_path) as conn:
                doc = _get_doc(conn, doc_id)
                revision = doc["annotation_revision"]
                rects = conn.execute(
                    "SELECT x, y, w, h, reason FROM annotation_rects"
                    " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                    (doc_id, revision),
                ).fetchall()
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                    " FROM publications WHERE doc_id = ?",
                    (doc_id,),
                ).fetchone()["v"]

                try:
                    with open(doc["original_path"], "rb") as f:
                        original_bytes = f.read()
                    oriented, _ = load_oriented(original_bytes)
                except Exception as exc:
                    _error(
                        500,
                        "original_unavailable",
                        f"stored original cannot be decoded: {exc}",
                    )

                image = render_publication(oriented, rects)
                doc_pub_dir = os.path.join(settings.publications_dir, doc_id)
                os.makedirs(doc_pub_dir, exist_ok=True)
                output_path = os.path.abspath(
                    os.path.join(doc_pub_dir, f"v{version}.png")
                )
                image.save(output_path, format="PNG")  # 无元数据的独立 PNG
                output_sha256 = _sha256_file(output_path)

                now = _utcnow()
                # 发布当下即固化不可变成品并占用正常版本号；计划公开只影响
                # 公开读取的放行时刻，不改版本序列。
                public_at = _public_at_text(public_at_dt)
                conn.execute(
                    "INSERT INTO publications (doc_id, version, annotation_revision,"
                    " original_sha256, output_sha256, output_path, created_at,"
                    " public_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        version,
                        revision,
                        doc["original_sha256"],
                        output_sha256,
                        output_path,
                        now,
                        public_at,
                    ),
                )
        except Exception:
            # 事务回滚时清理可能已落盘的半成品文件
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise
        return {
            "doc_id": doc_id,
            "version": version,
            "annotation_revision": revision,
            "original_sha256": doc["original_sha256"],
            "output_sha256": output_sha256,
            "redaction_count": len(rects),
            "created_at": now,
            "public_at": public_at,
        }

    @app.get("/documents/{doc_id}/publications")
    def list_publications(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.original_sha256,"
                " p.output_sha256, p.created_at, p.public_at" + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "original_sha256",
                        "output_sha256", "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 批次发布（整批原子） ----------------

    @app.post("/publication-batches", status_code=201)
    def publish_batch(body: BatchPublishRequest) -> Any:
        items = body.documents
        key = body.idempotency_key
        # 解析（格式）可在事务外先做：与批次内容无关的纯语法校验；
        # 时间窗校验放在幂等检查之后（重放不重新校验）。
        public_at_dt = _parse_public_at(body.public_at)
        request_hash = (
            _batch_request_hash(items, public_at_dt) if key is not None else None
        )

        batch_id = uuid.uuid4().hex
        now = _utcnow()
        written_paths: list[str] = []
        # 整批校验 + 渲染 + 落盘 + 记录全部在同一个 BEGIN IMMEDIATE 事务中，
        # 与标注修改、单份发布、其他批次发布互斥串行；
        # 任一项失败即回滚全部 INSERT，并清理已落盘文件，不留下可公开的
        # 部分版本，MAX(version)+1 也不会被失败批次推进（记录从未提交）。
        # 幂等键的"查重 -> 发布 -> 登记"也在该事务内：并发重试被写锁串行化，
        # 最多发布一次；任何失败回滚时键记录一并撤销，键不被占用，可原键重试。
        try:
            with write_tx(settings.db_path) as conn:
                # 第零阶段：幂等键检查，先于一切业务校验——
                # 相同键+相同内容的重试直接返回首次成功结果，
                # 即使标注随后变化，也不再校验当前修订号、不重新渲染。
                if key is not None:
                    prior = conn.execute(
                        "SELECT request_hash, response FROM batch_idempotency"
                        " WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if prior is not None:
                        if prior["request_hash"] == request_hash:
                            # 重放：原样返回持久化的首次成功响应（HTTP 200）
                            return Response(
                                content=prior["response"],
                                media_type="application/json",
                                status_code=200,
                            )
                        # 相同键+不同内容：明确冲突，原批次不受影响
                        _error(
                            409,
                            "idempotency_key_conflict",
                            f"idempotency key '{key}' was already used with"
                            " different batch content; the original batch is"
                            " unchanged",
                        )

                # 语义校验：时间窗、数量与重复（失败即回滚，不占用幂等键、不涉及版本号）
                # 同键同内容重放已在上面直接返回：即使此刻公开时间已过，也不重新校验。
                _validate_public_at_window(public_at_dt)
                if not 2 <= len(items) <= 20:
                    _error(
                        400,
                        "invalid_batch_size",
                        f"batch must contain between 2 and 20 documents,"
                        f" got {len(items)}",
                        count=len(items),
                    )
                seen: set[str] = set()
                for pos, item in enumerate(items):
                    if item.doc_id in seen:
                        _error(
                            400,
                            "duplicate_document_in_batch",
                            f"document '{item.doc_id}' appears more than once"
                            " in the batch",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    seen.add(item.doc_id)

                # 第一阶段：按请求顺序逐份校验（存在性 + 修订号匹配），
                # 先定位失败项，再渲染任何成品。
                prepared: list[tuple[Any, int, Any]] = []
                for pos, item in enumerate(items):
                    doc = conn.execute(
                        "SELECT * FROM documents WHERE id = ?", (item.doc_id,)
                    ).fetchone()
                    if doc is None:
                        _error(
                            404,
                            "document_not_found",
                            f"document '{item.doc_id}' does not exist",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    revision = doc["annotation_revision"]
                    if item.expected_revision != revision:
                        _error(
                            409,
                            "stale_revision",
                            f"annotation revision mismatch for '{item.doc_id}':"
                            f" expected {revision}, got {item.expected_revision};"
                            " re-fetch and retry",
                            position=pos,
                            doc_id=item.doc_id,
                            current_revision=revision,
                            supplied_revision=item.expected_revision,
                        )
                    rects = conn.execute(
                        "SELECT x, y, w, h, reason FROM annotation_rects"
                        " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                        (doc["id"], revision),
                    ).fetchall()
                    version = conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                        " FROM publications WHERE doc_id = ?",
                        (doc["id"],),
                    ).fetchone()["v"]
                    prepared.append((doc, version, revision, rects))

                # 第二阶段：全部校验通过后逐份渲染、落盘、登记
                # 全批次统一公开时刻：立即公开时即当前时刻（同 created_at）。
                public_at_text = _public_at_text(public_at_dt)
                result_items: list[dict] = []
                for pos, (doc, version, revision, rects) in enumerate(prepared):
                    try:
                        with open(doc["original_path"], "rb") as f:
                            original_bytes = f.read()
                        oriented, _ = load_oriented(original_bytes)
                    except Exception as exc:
                        _error(
                            500,
                            "original_unavailable",
                            f"stored original for '{doc['id']}' cannot be decoded:"
                            f" {exc}",
                            position=pos,
                            doc_id=doc["id"],
                        )

                    image = render_publication(oriented, rects)
                    doc_pub_dir = os.path.join(settings.publications_dir, doc["id"])
                    output_path = os.path.abspath(
                        os.path.join(doc_pub_dir, f"v{version}.png")
                    )
                    try:
                        os.makedirs(doc_pub_dir, exist_ok=True)
                        image.save(output_path, format="PNG")  # 无元数据独立 PNG
                        output_sha256 = _sha256_file(output_path)
                    except Exception as exc:
                        # 成品写入失败：清理可能已部分写入的文件
                        if os.path.exists(output_path):
                            try:
                                os.unlink(output_path)
                            except OSError:
                                pass
                        _error(
                            500,
                            "artifact_write_failed",
                            f"failed to write publication artifact for"
                            f" '{doc['id']}' version {version}: {exc}",
                            position=pos,
                            doc_id=doc["id"],
                            version=version,
                        )
                    written_paths.append(output_path)

                    conn.execute(
                        "INSERT INTO publications (doc_id, version, annotation_revision,"
                        " original_sha256, output_sha256, output_path, created_at,"
                        " batch_id, public_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            doc["id"],
                            version,
                            revision,
                            doc["original_sha256"],
                            output_sha256,
                            output_path,
                            now,
                            batch_id,
                            public_at_text,
                        ),
                    )
                    result_items.append(
                        {
                            "position": pos,
                            "doc_id": doc["id"],
                            "version": version,
                            "annotation_revision": revision,
                            "original_sha256": doc["original_sha256"],
                            "output_sha256": output_sha256,
                            "redaction_count": len(rects),
                            "created_at": now,
                            "public_at": public_at_text,
                        }
                    )

                conn.execute(
                    "INSERT INTO publication_batches (id, created_at, public_at)"
                    " VALUES (?,?,?)",
                    (batch_id, now, public_at_text),
                )
                for out in result_items:
                    conn.execute(
                        "INSERT INTO batch_publications (batch_id, position, doc_id,"
                        " version, annotation_revision, original_sha256, output_sha256)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            out["position"],
                            out["doc_id"],
                            out["version"],
                            out["annotation_revision"],
                            out["original_sha256"],
                            out["output_sha256"],
                        ),
                    )

                response_body = {
                    "batch_id": batch_id,
                    "created_at": now,
                    "count": len(result_items),
                    "public_at": public_at_text,
                    "documents": result_items,  # 顺序与请求一致
                }
                if key is not None:
                    # 首次成功：持久化键与完整成功结果，同事务提交
                    conn.execute(
                        "INSERT INTO batch_idempotency (key, request_hash, batch_id,"
                        " response, created_at) VALUES (?,?,?,?,?)",
                        (
                            key,
                            request_hash,
                            batch_id,
                            json.dumps(response_body, ensure_ascii=False),
                            now,
                        ),
                    )
        except Exception:
            # 事务回滚（不留下任何批次/版本/幂等键记录），并清理已落盘半成品
            for path in written_paths:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise
        return response_body

    @app.get("/publication-batches/{batch_id}")
    def get_publication_batch(batch_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            batch = conn.execute(
                "SELECT id, created_at, public_at FROM publication_batches"
                " WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                _error(
                    404,
                    "batch_not_found",
                    f"publication batch '{batch_id}' does not exist",
                )
            rows = conn.execute(
                "SELECT bp.position, bp.doc_id, bp.version, bp.annotation_revision,"
                " bp.original_sha256, bp.output_sha256, p.public_at" + _WITHDRAWAL_COLS +
                " FROM batch_publications bp"
                " JOIN publications p ON p.doc_id = bp.doc_id AND p.version = bp.version"
                + _WITHDRAWAL_JOIN.format(t="bp") +
                " WHERE bp.batch_id = ? ORDER BY bp.position",
                (batch_id,),
            ).fetchall()
        return {
            "batch_id": batch["id"],
            "created_at": batch["created_at"],
            "count": len(rows),
            "public_at": batch["public_at"],
            "documents": [
                _pub_entry(
                    r, ["position", "doc_id", "version", "annotation_revision",
                        "original_sha256", "output_sha256"]
                )
                for r in rows
            ],  # 顺序与原请求一致
        }

    # ---------------- 版本撤回（永久，幂等） ----------------

    def _withdrawal_out(doc_id: str, version: int, row: Any) -> dict:
        return {
            "doc_id": doc_id,
            "version": version,
            "withdrawn": row is not None,
            "withdrawn_at": row["withdrawn_at"] if row is not None else None,
            "withdrawal_reason": row["reason"] if row is not None else None,
        }

    @app.post(
        "/documents/{doc_id}/publications/{version}/withdraw",
        status_code=201,
    )
    def withdraw_publication(
        doc_id: str, version: int, body: WithdrawRequest, response: Response
    ) -> dict:
        # 存在性校验、原因校验、"查重 -> 插入"在同一个 BEGIN IMMEDIATE
        # 写事务内完成：与发布、标注修改、其他撤回互斥串行，并发重复撤回
        # 恰好一个创建记录（201），其余返回首次记录（200）；任何失败整体
        # 回滚，不留下部分撤回记录。撤回只插入 publication_withdrawals 行，
        # 不删除发布记录与成品文件，版本号也不会被后续发布复用。
        with write_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            reason = body.reason.strip()
            if not reason:
                _error(
                    400,
                    "reason_required",
                    "withdrawal reason must be non-empty",
                )
            existing = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
            if existing is not None:
                # 重复撤回：返回首次记录，不改写原因与时间
                response.status_code = 200
                return _withdrawal_out(doc_id, version, existing)
            now = _utcnow()
            conn.execute(
                "INSERT INTO publication_withdrawals"
                " (doc_id, version, reason, withdrawn_at) VALUES (?,?,?,?)",
                (doc_id, version, reason, now),
            )
        return _withdrawal_out(
            doc_id, version, {"reason": reason, "withdrawn_at": now}
        )

    @app.get("/documents/{doc_id}/publications/{version}/withdrawal")
    def get_withdrawal(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            row = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
        return _withdrawal_out(doc_id, version, row)

    # ---------------- 公开读取（只暴露发布版，不暴露原图） ----------------

    @app.get("/publications/{doc_id}/{version}")
    def get_publication(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            _ensure_public_available(conn, pub)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, pub["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "version": pub["version"],
            "annotation_revision": pub["annotation_revision"],
            "original_sha256": pub["original_sha256"],
            "output_sha256": pub["output_sha256"],
            "created_at": pub["created_at"],
            "public_at": pub["public_at"],
            "redactions": [_rect_out(r) for r in rects],
        }

    @app.get("/publications/{doc_id}/{version}/image")
    def get_publication_image(doc_id: str, version: int) -> FileResponse:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            _ensure_public_available(conn, pub)
        path = pub["output_path"]
        if not os.path.exists(path):
            _error(
                500,
                "artifact_missing",
                f"published artifact for version {version} is missing on disk",
            )
        return FileResponse(
            path,
            media_type="image/png",
            filename=f"{doc_id}-v{version}.png",
        )

    return app


app = create_app()
