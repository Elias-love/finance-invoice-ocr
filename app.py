"""增值税发票识别与台账系统 · 应用入口。

路由保持与前端约定的接口契约不变：
  POST /api/recognize      -> {"items": [...], "error": null|str}
  POST /api/export/excel   <- {"data": [...]}
  POST /api/export/csv     <- {"data": [...]}
"""

import logging
from datetime import datetime
from functools import wraps
from io import BytesIO
from urllib.parse import quote

import pandas as pd
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

import config
from invoice import ocr, storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("invoice_app")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.FLASK_SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    storage.init_db()
    _register_routes(app)
    return app


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def _rows_to_excel(rows: list[list[str]], sheet: str, cn_filename: str) -> Response:
    df = pd.DataFrame(rows, columns=config.EXPORT_FIELD_NAMES)
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


def _register_routes(app: Flask):

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/admin", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if (request.form.get("username") == config.ADMIN_USERNAME
                    and request.form.get("password") == config.ADMIN_PASSWORD):
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
        records = storage.get_all(newest_first=True)
        today = datetime.now().strftime("%Y-%m-%d")
        # 模板用 Jinja 自动转义渲染，杜绝 OCR 字段中的脚本注入（XSS）
        return render_template(
            "admin_dashboard.html",
            records=records,
            total_count=storage.count(),
            today_count=storage.count_on(today),
        )

    @app.route("/admin/clear", methods=["POST"])
    @_admin_required
    def admin_clear():
        storage.clear()
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/export/history")
    @_admin_required
    def admin_export_history():
        rows = [[__ef(r["data"], k) for k in config.EXPORT_FIELDS]
                for r in storage.get_all(newest_first=False)]
        return _rows_to_excel(rows, "发票历史记录", "发票历史记录.xlsx")

    @app.route("/api/recognize", methods=["POST"])
    def recognize():
        if "file" not in request.files:
            return jsonify({"items": [], "error": "未收到上传文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"items": [], "error": "文件名为空"}), 400

        import os
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            return jsonify({"items": [], "error": f"不支持的文件类型：{ext}"}), 400

        try:
            results = ocr.recognize_bytes(file.read(), file.filename)
            for invoice_data in results:
                storage.add_record(invoice_data, user="系统")
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
    def export_excel():
        data = (request.get_json(silent=True) or {}).get("data", [])
        rows = [[__ef(item, k) for k in config.EXPORT_FIELDS] for item in data]
        return _rows_to_excel(rows, "发票汇总", "发票汇总.xlsx")

    @app.route("/api/export/csv", methods=["POST"])
    def export_csv():
        data = (request.get_json(silent=True) or {}).get("data", [])
        lines = [",".join(config.EXPORT_FIELD_NAMES)]
        for item in data:
            lines.append(",".join(__ef(item, k).replace(",", "，")
                                  for k in config.EXPORT_FIELDS))
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
    print("=" * 50)
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
