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
from invoice.validate import validate_invoice

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
    for record in storage.get_all(newest_first=False):
        if not record.get("review"):
            control = record.get("validation") or validate_invoice(record["data"])
            review = assess_review(
                record["data"],
                control,
                source_sha256=record.get("source_sha256") or "",
            )
            storage.initialize_review_metadata(
                record["id"], validation=control, review=review
            )
    _register_routes(app)

    @app.context_processor
    def inject_csrf():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["_csrf_token"]}

    return app


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


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
    submitted = request.form.get("_csrf_token", "")
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


def _register_routes(app: Flask):

    @app.route("/")
    @_admin_required
    def index():
        return render_template(
            "index.html",
            ocr_configured=bool(
                config.BAIDU_OCR_API_KEY and config.BAIDU_OCR_SECRET_KEY
            ),
        )

    @app.route("/admin", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            _verify_csrf()
            valid_user = hmac.compare_digest(
                request.form.get("username", ""), config.ADMIN_USERNAME
            )
            valid_password = bool(config.ADMIN_PASSWORD) and hmac.compare_digest(
                request.form.get("password", ""), config.ADMIN_PASSWORD
            )
            if valid_user and valid_password:
                session["is_admin"] = True
                return redirect(url_for("admin_dashboard"))
            return render_template("admin_login.html", error="用户名或密码错误")
        return render_template("admin_login.html", error=None)

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
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
        )

    @app.route("/admin/clear", methods=["POST"])
    @_admin_required
    def admin_clear():
        _verify_csrf()
        storage.clear()
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/review/<int:record_id>", methods=["GET", "POST"])
    @_admin_required
    def admin_review(record_id: int):
        record = storage.get_by_id(record_id)
        if not record:
            abort(404)

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

            control = validate_invoice(corrected)
            review = assess_review(
                corrected,
                control,
                source_sha256=record.get("source_sha256") or "",
            )
            if action == "approve" and control["status"] != "pass":
                error = "仍有硬性校验错误，请修正后再通过"
            else:
                target = {
                    "save": "pending",
                    "approve": "approved",
                    "reject": "rejected",
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
                    reviewer=config.ADMIN_USERNAME,
                    note=request.form.get("review_note", "").strip(),
                    validation=control,
                    review=review,
                )
                return redirect(url_for("admin_review", record_id=record_id))

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

    @app.route("/admin/export/history")
    @_admin_required
    def admin_export_history():
        rows = [[__ef(r["data"], k) for k in config.EXPORT_FIELDS]
                for r in storage.get_all(newest_first=False)]
        return _rows_to_excel(rows, "发票历史记录", "发票历史记录.xlsx")

    @app.route("/api/recognize", methods=["POST"])
    @_api_admin_required
    def recognize():
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
            if not _rate_limit_ok(client_key, units=units):
                return jsonify({
                    "items": [],
                    "error": (
                        f"本文件需调用 OCR {units} 次，已超过每分钟 "
                        f"{config.OCR_RATE_LIMIT_PER_MINUTE} 页的限额，请稍后再试"
                    ),
                }), 429

            source_sha256 = hashlib.sha256(content).hexdigest()
            results = ocr.recognize_bytes(content, file.filename)
            source_path = (
                storage.save_source(content, ext, source_sha256) if results else ""
            )
            for invoice_data in results:
                control = validate_invoice(invoice_data)
                invoice_num = field(invoice_data, "InvoiceNum").strip()
                duplicate = storage.invoice_exists(invoice_num)
                if duplicate:
                    control = {
                        **control,
                        "status": "duplicate",
                        "message": "重复发票：历史台账已存在相同发票号码",
                    }
                review = assess_review(
                    invoice_data,
                    control,
                    duplicate=duplicate,
                    source_sha256=source_sha256,
                )
                if duplicate:
                    record_id = None
                else:
                    record_id = storage.add_record(
                        invoice_data,
                        user=config.ADMIN_USERNAME,
                        source_sha256=source_sha256,
                        validation=control,
                        review=review,
                        source_filename=file.filename,
                        source_path=source_path,
                    )
                # 以下字段只用于本次前端响应，不写入不可变的原始 OCR JSON。
                invoice_data["_control"] = control
                invoice_data["_review"] = review
                invoice_data["_record_id"] = record_id
            # 识别成功但未提取到发票时，明确告知而非静默返回空
            error = None if results else "未识别到发票内容，请确认上传的是清晰的增值税发票"
            return jsonify({"items": results, "error": error})
        except ocr.OcrError as e:
            logger.warning("识别失败：%s", e)
            return jsonify({"items": [], "error": str(e)}), 502
        except Exception as e:  # 兜底：仍返回明确错误而非空数组
            logger.exception("识别异常")
            return jsonify({"items": [], "error": f"识别异常：{e}"}), 500

    @app.route("/api/export/excel", methods=["POST"])
    @_api_admin_required
    def export_excel():
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
