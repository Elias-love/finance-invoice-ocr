"""增值税发票识别与台账系统 · 应用入口。

路由保持与前端约定的接口契约不变：
  POST /api/recognize      -> {"items": [...], "error": null|str}
  POST /api/export/excel   <- {"data": [...]}
  POST /api/export/csv     <- {"data": [...]}
"""

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

import config
from invoice import ocr, storage
from invoice.extract import field
from invoice.review import assess_review
from invoice.validate import manual_approval_blockers, validate_invoice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("invoice_app")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.FLASK_SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    )
    storage.init_db()
    # 升级前的演示台账没有 review_json；启动时按当前确定性规则补齐，
    # 让旧数据也能进入新的分流看板。原始 OCR JSON 保持不变。
    source_cache: dict[str, bytes] = {}
    for record in storage.get_all(newest_first=False):
        if not record.get("review"):
            control = record.get("validation") or validate_invoice(record["data"])
            review = assess_review(
                record["data"],
                control,
                source_sha256=record.get("source_sha256") or "",
                source_page=record.get("source_page"),
            )
            storage.initialize_review_metadata(
                record["id"], validation=control, review=review
            )
        elif (
            record["review"].get("policy_version") != "3.1"
            and record.get("source_path")
        ):
            # 旧记录无需再次付费 OCR：从已保存原票重算图像质量和二维码证据。
            try:
                source_path = Path(record["source_path"]).resolve()
                allowed_dir = (config.DB_PATH.parent / "uploads").resolve()
                if source_path.parent != allowed_dir or not source_path.exists():
                    continue
                cache_key = str(source_path)
                if cache_key not in source_cache:
                    source_cache[cache_key] = source_path.read_bytes()
                source_bytes = source_cache[cache_key]
                enriched = ocr.enrich_local_evidence(
                    source_bytes,
                    record.get("source_filename") or source_path.name,
                    int(record.get("source_page") or 1),
                    record["data"],
                )
                control = validate_invoice(enriched)
                review = assess_review(
                    enriched,
                    control,
                    source_sha256=record.get("source_sha256") or "",
                    source_page=record.get("source_page"),
                )
                if record["review_status"] in {"approved", "rejected"}:
                    review = {
                        **review,
                        "review_status": record["review_status"],
                        "message": record["review"].get("message")
                        or (
                            "人工复核通过"
                            if record["review_status"] == "approved"
                            else "人工复核驳回"
                        ),
                    }
                storage.refresh_machine_metadata(
                    record["id"],
                    current_data=enriched,
                    validation=control,
                    review=review,
                )
            except Exception:
                logger.exception("历史记录 #%s 本地证据升级失败", record["id"])
    _register_routes(app)

    @app.context_processor
    def inject_csrf():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["_csrf_token"]}

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'self'",
        )
        return response

    return app


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def _current_operator() -> str:
    return session.get("username") or config.ADMIN_USERNAME


def _api_admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"items": [], "error": "请先登录后再操作"}), 401
        return f(*args, **kwargs)
    return wrapper


_ocr_calls = defaultdict(deque)


def _rate_limit_ok(client_key: str, units: int = 1) -> bool:
    now = time.time()
    calls = _ocr_calls[client_key]
    while calls and calls[0] < now - 60:
        calls.popleft()
    units = max(1, int(units))
    if len(calls) + units > config.OCR_RATE_LIMIT_PER_MINUTE:
        return False
    calls.extend([now] * units)
    return True


def _verify_csrf():
    submitted = (
        request.form.get("_csrf_token", "")
        or request.headers.get("X-CSRF-Token", "")
    )
    expected = session.get("_csrf_token", "")
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        abort(400, description="CSRF token 校验失败")


def _looks_like_allowed_file(content: bytes, ext: str) -> bool:
    signatures = {
        ".pdf": (b"%PDF",),
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".bmp": (b"BM",),
    }
    return any(content.startswith(sig) for sig in signatures.get(ext, ()))


def _safe_export_cell(value: str) -> str:
    """阻止 CSV/Excel 打开时把 OCR 文本当作公式执行。"""
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _rows_to_excel(rows: list[list[str]], sheet: str, cn_filename: str) -> Response:
    safe_rows = [[_safe_export_cell(cell) for cell in row] for row in rows]
    df = pd.DataFrame(safe_rows, columns=config.EXPORT_FIELD_NAMES)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=export.xlsx; filename*=UTF-8''{quote(cn_filename)}"},
    )


def _exportable_records(record_ids: list) -> list[dict]:
    allowed = {"approved"}
    if config.ALLOW_AUTO_PASS_EXPORT:
        allowed.add("auto_pass")
    return [
        record for record in storage.get_by_ids(record_ids)
        if record["review_status"] in allowed
    ]


def _record_for_batch(record: dict) -> dict:
    """把数据库记录还原成首页识别卡片使用的结构。"""
    item = dict(record["data"])
    item.update({
        "_control": record.get("validation") or {},
        "_review": record.get("review") or {},
        "_record_id": record["id"],
        "_fileName": record.get("source_filename") or "",
        "_isDuplicate": record.get("validation_status") == "duplicate",
    })
    return item


def _current_batch_records() -> list[dict]:
    """读取当前会话的识别批次，并清理已不存在的记录 ID。"""
    requested_ids = session.get("current_batch_ids", [])
    if (
        "current_batch_ids" not in session
        or (
            not requested_ids
            and "current_batch_cleared" not in session
        )
    ):
        # 兼容升级前已经完成识别、但尚未写入会话批次 ID 的页面：
        # 按最近一次上传文件的 SHA-256 恢复其全部页，不再次调用 OCR。
        recovered = storage.get_latest_source_batch()
        session["current_batch_ids"] = [record["id"] for record in recovered]
        session["current_batch_cleared"] = False
        return recovered

    records = storage.get_by_ids(requested_ids)
    valid_ids = [record["id"] for record in records]
    if valid_ids != requested_ids:
        session["current_batch_ids"] = valid_ids
    return records


def _register_routes(app: Flask):

    @app.route("/")
    @_admin_required
    def index():
        current_batch_results = [
            _record_for_batch(record) for record in _current_batch_records()
        ]
        return render_template(
            "index.html",
            ocr_configured=bool(
                config.BAIDU_OCR_API_KEY and config.BAIDU_OCR_SECRET_KEY
            ),
            current_batch_results=current_batch_results,
        )

    @app.route("/admin", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            _verify_csrf()
            submitted_user = request.form.get("username", "")
            submitted_password = request.form.get("password", "")
            accounts = (
                (config.ADMIN_USERNAME, config.ADMIN_PASSWORD, "admin"),
                (config.REVIEWER_USERNAME, config.REVIEWER_PASSWORD, "reviewer"),
            )
            for username, password, role in accounts:
                valid_user = hmac.compare_digest(submitted_user, username)
                valid_password = bool(password) and hmac.compare_digest(
                    submitted_password, password
                )
                if valid_user and valid_password:
                    session["is_admin"] = True
                    session["username"] = username
                    session["role"] = role
                    return redirect(url_for("admin_dashboard"))
            return render_template("admin_login.html", error="用户名或密码错误")
        return render_template("admin_login.html", error=None)

    @app.route("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/admin/dashboard")
    @_admin_required
    def admin_dashboard():
        status_filter = request.args.get("status", "").strip()
        records = storage.get_all(newest_first=True)
        if status_filter in {"pending", "auto_pass", "approved", "rejected"}:
            records = [r for r in records if r["review_status"] == status_filter]
        today = datetime.now().strftime("%Y-%m-%d")
        return render_template(
            "admin_dashboard.html",
            records=records,
            total_count=storage.count(),
            today_count=storage.count_on(today),
            pending_count=storage.count_by_review_status("pending"),
            auto_pass_count=storage.count_by_review_status("auto_pass"),
            approved_count=storage.count_by_review_status("approved"),
            rejected_count=storage.count_by_review_status("rejected"),
            status_filter=status_filter,
            destructive_clear_enabled=config.ALLOW_DESTRUCTIVE_CLEAR,
        )

    @app.route("/admin/clear", methods=["POST"])
    @_admin_required
    def admin_clear():
        _verify_csrf()
        if session.get("role", "admin") != "admin":
            abort(403, description="只有管理员可以执行台账管理操作")
        if not config.ALLOW_DESTRUCTIVE_CLEAR:
            abort(
                403,
                description=(
                    "安全策略已禁用清空台账；本地演示环境需要显式设置 "
                    "ALLOW_DESTRUCTIVE_CLEAR=1"
                ),
            )
        storage.clear()
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/review/<int:record_id>", methods=["GET", "POST"])
    @_admin_required
    def admin_review(record_id: int):
        record = storage.get_by_id(record_id)
        if not record:
            abort(404)
        return_to = request.args.get("return_to", "")
        if return_to not in {"/", "/admin/dashboard"}:
            return_to = "/admin/dashboard"

        error = None
        if request.method == "POST":
            _verify_csrf()
            action = request.form.get("action", "save")
            corrected = {
                key: value for key, value in record["data"].items()
                if not str(key).startswith("_")
            }
            for key, _label in config.REVIEW_FIELDS:
                corrected[key] = request.form.get(key, "").strip()
            for evidence_key in ("_quality", "_qr"):
                if evidence_key in record["data"]:
                    corrected[evidence_key] = record["data"][evidence_key]

            control = validate_invoice(corrected)
            review = assess_review(
                corrected,
                control,
                source_sha256=record.get("source_sha256") or "",
                source_page=record.get("source_page"),
            )
            review_note = request.form.get("review_note", "").strip()
            blockers = manual_approval_blockers(corrected)
            if action in {"approve", "approve_next"} and blockers:
                error = "关键字段仍不可入账：" + "；".join(blockers)
            elif (
                action in {"approve", "approve_next"}
                and (
                    control["status"] != "pass"
                    or review.get("risk_level") == "high"
                )
                and not review_note
            ):
                error = "覆盖机器硬性异常时必须填写复核依据"
            elif (
                action in {"approve", "approve_next"}
                and config.ENFORCE_MAKER_CHECKER
                and record.get("user") == _current_operator()
            ):
                error = "已启用经办/复核分离，识别经办人不能批准自己的记录"
            else:
                target = {
                    "save": "pending",
                    "approve": "approved",
                    "approve_next": "approved",
                    "reject": "rejected",
                    "reject_next": "rejected",
                }.get(action)
                if target is None:
                    abort(400, description="不支持的复核动作")
                if target == "approved":
                    review = {
                        **review,
                        "review_status": "approved",
                        "message": "人工复核通过",
                    }
                elif target == "rejected":
                    review = {
                        **review,
                        "review_status": "rejected",
                        "message": "人工复核驳回",
                    }
                storage.update_review(
                    record_id,
                    corrected_data=corrected,
                    review_status=target,
                    reviewer=_current_operator(),
                    note=review_note,
                    validation=control,
                    review=review,
                )
                if action in {"approve_next", "reject_next"}:
                    next_id = storage.get_next_pending(record_id)
                    if next_id:
                        return redirect(url_for(
                            "admin_review",
                            record_id=next_id,
                            return_to=return_to,
                        ))
                return redirect(url_for(
                    "admin_review",
                    record_id=record_id,
                    return_to=return_to,
                ))

            record = {
                **record,
                "data": corrected,
                "validation": control,
                "review": review,
            }

        return render_template(
            "review_detail.html",
            record=record,
            review_fields=config.REVIEW_FIELDS,
            events=storage.get_review_events(record_id),
            error=error,
            back_url=return_to,
            back_label=(
                "返回本批识别结果"
                if return_to == "/"
                else "返回复核队列"
            ),
        )

    @app.route("/admin/source/<int:record_id>")
    @_admin_required
    def admin_source(record_id: int):
        record = storage.get_by_id(record_id)
        if not record or not record.get("source_path"):
            abort(404)
        source_path = Path(record["source_path"]).resolve()
        allowed_dir = (config.DB_PATH.parent / "uploads").resolve()
        if source_path.parent != allowed_dir or not source_path.exists():
            abort(403)
        return send_file(
            source_path,
            download_name=record.get("source_filename") or "invoice",
            as_attachment=request.args.get("download") == "1",
        )

    @app.route("/admin/source/<int:record_id>/preview")
    @_admin_required
    def admin_source_preview(record_id: int):
        """复核页只展示当前记录对应的 PDF 单页；图片则展示原图。"""
        record = storage.get_by_id(record_id)
        if not record or not record.get("source_path"):
            abort(404)
        source_path = Path(record["source_path"]).resolve()
        allowed_dir = (config.DB_PATH.parent / "uploads").resolve()
        if source_path.parent != allowed_dir or not source_path.exists():
            abort(403)
        if source_path.suffix.lower() != ".pdf":
            return send_file(source_path)

        import fitz
        with fitz.open(source_path) as document:
            page_number = int(record.get("source_page") or 1)
            if page_number < 1 or page_number > len(document):
                abort(404, description="来源页码超出 PDF 范围")
            zoom = max(config.PDF_ZOOM, 1.5)
            pixmap = document[page_number - 1].get_pixmap(
                matrix=fitz.Matrix(zoom, zoom),
                alpha=False,
            )
            image_bytes = pixmap.tobytes("png")
        return Response(
            image_bytes,
            mimetype="image/png",
            headers={"Cache-Control": "private, max-age=300"},
        )

    @app.route("/admin/export/history")
    @_admin_required
    def admin_export_history():
        rows = [[__ef(r["data"], k) for k in config.EXPORT_FIELDS]
                for r in storage.get_all(newest_first=False)]
        return _rows_to_excel(rows, "发票历史记录", "发票历史记录.xlsx")

    @app.route("/api/recognize", methods=["POST"])
    @_api_admin_required
    def recognize():
        _verify_csrf()
        if request.form.get("batch_action") == "reset":
            session["current_batch_ids"] = []
            session["current_batch_cleared"] = True

        client_key = request.remote_addr or "unknown"
        if "file" not in request.files:
            return jsonify({"items": [], "error": "未收到上传文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"items": [], "error": "文件名为空"}), 400

        import os
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            return jsonify({"items": [], "error": f"不支持的文件类型：{ext}"}), 400

        content = file.read()
        if not _looks_like_allowed_file(content, ext):
            return jsonify({"items": [], "error": "文件内容与扩展名不匹配"}), 400

        try:
            units = ocr.estimate_units(content, file.filename)
            if units > config.MAX_PDF_PAGES:
                return jsonify({
                    "items": [],
                    "error": f"PDF 共 {units} 页，超过单次上限 {config.MAX_PDF_PAGES} 页",
                }), 400
            source_sha256 = hashlib.sha256(content).hexdigest()
            cached_records = (
                []
                if request.form.get("force") == "1"
                else storage.get_by_source_sha256(source_sha256)
            )
            if not cached_records and not _rate_limit_ok(client_key, units=units):
                return jsonify({
                    "items": [],
                    "error": (
                        f"本文件需调用 OCR {units} 次，已超过每分钟 "
                        f"{config.OCR_RATE_LIMIT_PER_MINUTE} 页的限额，请稍后再试"
                    ),
                }), 429

            if cached_records:
                results = []
                for position, cached_record in enumerate(cached_records, start=1):
                    cached_page = int(
                        cached_record.get("source_page") or position
                    )
                    cached_data = ocr.enrich_local_evidence(
                        content,
                        file.filename,
                        cached_page,
                        cached_record["original_data"],
                    )
                    cached_data["_source_page"] = cached_page
                    cached_data["_cache_hit"] = True
                    results.append(cached_data)
            else:
                results = ocr.recognize_bytes(content, file.filename)
            source_path = (
                storage.save_source(content, ext, source_sha256) if results else ""
            )
            # 判重范围严格限定为当前点击“开始识别”建立的批次。
            # 历史台账只用于留档，不参与本次重复判断。
            current_batch = storage.get_by_ids(
                session.get("current_batch_ids", [])
            )
            seen_invoice_nums = {
                field(record["data"], "InvoiceNum").strip()
                for record in current_batch
                if field(record["data"], "InvoiceNum").strip()
            }
            for result_position, invoice_data in enumerate(results, start=1):
                cache_hit = bool(invoice_data.pop("_cache_hit", False))
                source_page = int(
                    invoice_data.pop("_source_page", result_position)
                )
                control = validate_invoice(invoice_data)
                invoice_num = field(invoice_data, "InvoiceNum").strip()
                duplicate = bool(
                    invoice_num and invoice_num in seen_invoice_nums
                )
                if duplicate:
                    control = {
                        **control,
                        "status": "duplicate",
                        "message": "重复发票：本次识别批次存在相同发票号码",
                    }
                review = assess_review(
                    invoice_data,
                    control,
                    duplicate=duplicate,
                    source_sha256=source_sha256,
                    source_page=source_page,
                )
                record_id = storage.add_record(
                    invoice_data,
                    user=_current_operator(),
                    source_sha256=source_sha256,
                    validation=control,
                    review=review,
                    source_filename=file.filename,
                    source_path=source_path,
                    source_page=source_page,
                )
                if invoice_num:
                    seen_invoice_nums.add(invoice_num)
                # 以下字段只用于本次前端响应，不写入不可变的原始 OCR JSON。
                invoice_data["_control"] = control
                invoice_data["_review"] = review
                invoice_data["_record_id"] = record_id
                invoice_data["_cache_hit"] = cache_hit
            new_record_ids = [
                item["_record_id"] for item in results if item.get("_record_id")
            ]
            if new_record_ids:
                current_ids = session.get("current_batch_ids", [])
                session["current_batch_ids"] = list(dict.fromkeys(
                    [*current_ids, *new_record_ids]
                ))[-200:]
                session["current_batch_cleared"] = False
            # 识别成功但未提取到发票时，明确告知而非静默返回空
            error = None if results else "未识别到发票内容，请确认上传的是清晰的增值税发票"
            return jsonify({"items": results, "error": error})
        except ocr.OcrError as e:
            logger.warning("识别失败：%s", e)
            return jsonify({"items": [], "error": str(e)}), 502
        except Exception as e:  # 兜底：仍返回明确错误而非空数组
            logger.exception("识别异常")
            return jsonify({"items": [], "error": f"识别异常：{e}"}), 500

    @app.route("/api/records/<int:record_id>/rerun", methods=["POST"])
    @_api_admin_required
    def rerun_record(record_id: int):
        """仅重识别当前记录对应的一页，保留首轮 OCR 和每次尝试轨迹。"""
        _verify_csrf()
        record = storage.get_by_id(record_id)
        if not record or not record.get("source_path"):
            return jsonify({"error": "找不到该记录的原始凭证"}), 404
        source_path = Path(record["source_path"]).resolve()
        allowed_dir = (config.DB_PATH.parent / "uploads").resolve()
        if source_path.parent != allowed_dir or not source_path.exists():
            return jsonify({"error": "原始凭证路径不在授权目录"}), 403
        if not _rate_limit_ok(request.remote_addr or "unknown", units=1):
            return jsonify({"error": "已达到每分钟 OCR 限额，请稍后重试"}), 429

        try:
            page_number = int(record.get("source_page") or 1)
            invoice_data = ocr.recognize_page(
                source_path.read_bytes(),
                record.get("source_filename") or source_path.name,
                page_number,
            )
            invoice_data.pop("_source_page", None)
            control = validate_invoice(invoice_data)
            current_batch = [
                item for item in storage.get_by_ids(
                    session.get("current_batch_ids", [])
                )
                if item["id"] != record_id
            ]
            seen_invoice_nums = {
                field(item["data"], "InvoiceNum").strip()
                for item in current_batch
                if field(item["data"], "InvoiceNum").strip()
            }
            invoice_num = field(invoice_data, "InvoiceNum").strip()
            duplicate = bool(invoice_num and invoice_num in seen_invoice_nums)
            if duplicate:
                control = {
                    **control,
                    "status": "duplicate",
                    "message": "重复发票：本次识别批次存在相同发票号码",
                }
            review = assess_review(
                invoice_data,
                control,
                duplicate=duplicate,
                source_sha256=record.get("source_sha256") or "",
                source_page=page_number,
            )
            invoice_data["_source_page"] = page_number
            storage.update_recognition_attempt(
                record_id,
                invoice_data=invoice_data,
                actor=_current_operator(),
                validation=control,
                review=review,
            )
            updated = storage.get_by_id(record_id)
            item = _record_for_batch(updated)
            item["_isDuplicate"] = duplicate
            return jsonify({"item": item, "error": None})
        except ocr.OcrError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            logger.exception("单页重识别异常")
            return jsonify({"error": f"单页重识别异常：{exc}"}), 500

    @app.route("/api/current-batch/remove", methods=["POST"])
    @_api_admin_required
    def remove_from_current_batch():
        _verify_csrf()
        payload = request.get_json(silent=True) or {}
        try:
            record_id = int(payload.get("record_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "无效的记录 ID"}), 400
        session["current_batch_ids"] = [
            rid for rid in session.get("current_batch_ids", [])
            if rid != record_id
        ]
        return jsonify({"ok": True})

    @app.route("/api/current-batch/clear", methods=["POST"])
    @_api_admin_required
    def clear_current_batch():
        _verify_csrf()
        # 保留显式空列表，避免下一次加载又触发旧版本兼容恢复。
        session["current_batch_ids"] = []
        session["current_batch_cleared"] = True
        return jsonify({"ok": True})

    @app.route("/api/export/excel", methods=["POST"])
    @_api_admin_required
    def export_excel():
        _verify_csrf()
        payload = request.get_json(silent=True) or {}
        records = _exportable_records(payload.get("record_ids", []))
        if not records:
            return jsonify({
                "error": "没有可导出的记录；待复核或已驳回发票须先完成复核"
            }), 400
        rows = [
            [__ef(record["data"], k) for k in config.EXPORT_FIELDS]
            for record in records
        ]
        return _rows_to_excel(rows, "发票汇总", "发票汇总.xlsx")

    @app.route("/api/export/csv", methods=["POST"])
    @_api_admin_required
    def export_csv():
        _verify_csrf()
        payload = request.get_json(silent=True) or {}
        records = _exportable_records(payload.get("record_ids", []))
        if not records:
            return jsonify({
                "error": "没有可导出的记录；待复核或已驳回发票须先完成复核"
            }), 400
        lines = [",".join(config.EXPORT_FIELD_NAMES)]
        for record in records:
            lines.append(",".join(
                _safe_export_cell(__ef(record["data"], k)).replace(",", "，")
                for k in config.EXPORT_FIELDS
            ))
        return Response(
            "\n".join(lines),
            mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=export.csv; filename*=UTF-8''{quote('发票汇总.csv')}"},
        )

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"items": [], "error": "文件过大，超出上传上限"}), 413


def __ef(data: dict, key: str) -> str:
    """导出取字段（复用抽取逻辑）。"""
    from invoice.extract import field
    return field(data, key)


app = create_app()

if __name__ == "__main__":
    print("=" * 50)
    print("发票识别系统启动中...")
    print(f"访问地址:   http://127.0.0.1:{config.PORT}")
    print(f"管理员入口: http://127.0.0.1:{config.PORT}/admin  (账号 {config.ADMIN_USERNAME})")
    if not config.BAIDU_OCR_API_KEY or not config.BAIDU_OCR_SECRET_KEY:
        print("⚠️  未配置百度 OCR 凭证：识别功能不可用，但可浏览演示台账")
    if not config.ADMIN_PASSWORD:
        print("⚠️  未配置 ADMIN_PASSWORD：系统保持锁定，请先在 .env 设置强密码")
    print("=" * 50)
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
