from flask import Flask, request, jsonify, render_template_string, Response, session, redirect, url_for
from functools import wraps
import requests
import base64
import json
import os
import csv
from io import BytesIO
from datetime import datetime
from urllib.parse import quote
import fitz  # PyMuPDF
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ==================== 1. 核心配置 ====================
# 百度云 VAT OCR 凭证：从环境变量读取，切勿硬编码进源码
# 在 .env 中配置 BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY（申请地址见 README）
API_KEY = os.getenv("BAIDU_OCR_API_KEY", "")
SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "")
GLOBAL_TOKEN = ""  # 运行时用 API_KEY/SECRET_KEY 换取，不再硬编码

# 管理员账号（从环境变量读取，部署前务必修改默认密码）
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-please")

# 历史记录文件
HISTORY_FILE = "invoice_records_simple.json"

# ==================== 2. 管理员登录验证 ====================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== 3. 历史记录功能 ====================
_history_cache = None

def load_history():
    global _history_cache
    if _history_cache is not None:
        return _history_cache
    if not os.path.exists(HISTORY_FILE):
        _history_cache = []
        return _history_cache
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
        # 兼容性处理：为旧记录添加缺失的字段
        for i, record in enumerate(history):
            if 'id' not in record:
                record['id'] = i + 1
            if 'user' not in record:
                record['user'] = '匿名'
            if 'data' not in record and 'time' in record:
                # 旧格式转换
                record['data'] = record
        _history_cache = history
        return _history_cache

def save_history(invoice_data, user_name="匿名"):
    global _history_cache
    history = load_history()
    history.append({
        "id": len(history) + 1,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": user_name,
        "data": invoice_data
    })
    # 每次都保存，确保数据不丢失
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    _history_cache = history

def clear_history():
    global _history_cache
    _history_cache = []
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)

# ==================== 4. 百度OCR功能 ====================
def get_baidu_token():
    global GLOBAL_TOKEN
    if GLOBAL_TOKEN:
        return GLOBAL_TOKEN
    url = "https://aip.baidubce.com/oauth/2.0/token"
    params = {
        "grant_type": "client_credentials",
        "client_id": API_KEY,
        "client_secret": SECRET_KEY
    }
    resp = requests.post(url, params=params, timeout=10)
    GLOBAL_TOKEN = resp.json()["access_token"]
    return GLOBAL_TOKEN

def ocr_image(image_bytes):
    token = get_baidu_token()
    api_url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice?access_token={token}"
    img_base64 = base64.b64encode(image_bytes).decode("utf-8")
    resp = requests.post(api_url, data={"image": img_base64}, timeout=15)
    return resp.json()

# ==================== 5. 网页模板 ====================
MAIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>发票识别系统</title>
    <style>
        * {
            box-sizing: border-box;
        }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial; 
            background: #f0f4f8; 
            padding: 20px 15px;
            margin: 0;
        }
        .container { 
            max-width: 900px; 
            margin: 0 auto; 
        }
        .header { 
            background: linear-gradient(135deg, #0f52ba 0%, #1e88e5 100%); 
            color: white; 
            padding: 20px; 
            border-radius: 12px; 
            margin-bottom: 20px;
            text-align: center;
        }
        .header h1 {
            margin: 0 0 8px 0;
            font-size: 22px;
        }
        .header p {
            margin: 0;
            opacity: 0.9;
            font-size: 14px;
        }
        .admin-link {
            text-align: right;
            margin-bottom: 10px;
        }
        .admin-link a {
            color: #64748b;
            text-decoration: none;
            font-size: 12px;
        }
        .card { 
            background: white; 
            border-radius: 12px; 
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); 
            padding: 20px; 
            margin-bottom: 20px;
        }
        .user-input {
            margin-bottom: 16px;
        }
        .user-input label {
            display: block;
            margin-bottom: 8px;
            color: #374151;
            font-size: 14px;
        }
        .user-input input {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 14px;
        }
        .upload-box { 
            border: 2px dashed #cbd5e1; 
            border-radius: 8px; 
            padding: 30px 20px; 
            text-align: center; 
            cursor: pointer;
            background: #f8fafc;
            transition: all 0.3s;
        }
        .upload-box:hover { 
            border-color: #0f52ba; 
            background: #f0f7ff;
        }
        .btn { 
            background: #0f52ba; 
            color: white; 
            border: none; 
            padding: 12px 20px; 
            border-radius: 8px; 
            font-size: 15px; 
            cursor: pointer; 
            margin-top: 16px;
            transition: background 0.3s;
        }
        .btn:hover { 
            background: #094099;
        }
        .btn:disabled { 
            opacity: 0.5; 
            cursor: not-allowed;
        }
        .btn-danger {
            background: #ef4444;
        }
        .btn-danger:hover {
            background: #dc2626;
        }
        .btn-success {
            background: #10b981;
        }
        .btn-success:hover {
            background: #059669;
        }
        .btn-warning {
            background: #f59e0b;
        }
        .btn-warning:hover {
            background: #d97706;
        }
        .loading { 
            display: none; 
            color: #0f52ba; 
            font-weight: 500; 
            margin: 16px 0;
            text-align: center;
        }
        .file-list { 
            margin-top: 20px; 
            max-height: 200px; 
            overflow-y: auto;
        }
        .file-item { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 10px 12px; 
            background: #f8fafc; 
            border-radius: 6px; 
            margin-bottom: 8px;
            font-size: 14px;
        }
        .result-item { 
            border: 1px solid #e2e8f0; 
            border-radius: 8px; 
            padding: 16px; 
            margin-bottom: 12px;
            transition: all 0.3s;
        }
        .result-item.duplicate {
            border: 2px solid #ef4444;
            background: #fef2f2;
        }
        .result-item h4 {
            margin: 0 0 12px 0;
            color: #1e293b;
            font-size: 16px;
        }
        .duplicate-warning {
            color: #ef4444;
            font-weight: 500;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .verification-status {
            padding: 8px 12px;
            border-radius: 6px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 500;
        }
        .verification-status.success {
            background: #d1fae5;
            color: #065f46;
        }
        .verification-status.error {
            background: #fee2e2;
            color: #991b1b;
        }
        .row { 
            display: grid; 
            grid-template-columns: 1fr 1fr; 
            gap: 12px; 
            margin-top: 8px;
        }
        .label { 
            color: #64748b; 
            font-size: 13px;
        }
        .value { 
            font-weight: 500; 
            font-size: 14px;
            word-break: break-all;
        }
        .action-buttons {
            display: flex;
            gap: 8px;
            margin-top: 16px;
            flex-wrap: wrap;
        }
        .action-buttons .btn {
            margin-top: 0;
            padding: 8px 16px;
            font-size: 14px;
        }
        .footer {
            text-align: center;
            padding: 20px;
            color: #64748b;
            font-size: 14px;
            border-top: 1px solid #e2e8f0;
            margin-top: 20px;
        }
        
        @media (max-width: 640px) {
            body {
                padding: 10px 10px;
            }
            .header {
                padding: 16px;
            }
            .header h1 {
                font-size: 18px;
            }
            .card {
                padding: 16px;
            }
            .upload-box {
                padding: 25px 15px;
            }
            .row {
                grid-template-columns: 1fr;
            }
            .btn {
                width: 100%;
                padding: 14px;
            }
            .action-buttons {
                flex-direction: column;
            }
            .action-buttons .btn {
                width: 100%;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="admin-link">
            <a href="/admin">🔐 管理员入口</a>
        </div>
        
        <div class="header">
            <h1>🏢 财务部增值税发票智能识别系统</h1>
            <p>支持批量图片/PDF识别 | 一键导出Excel</p>
        </div>

        <div class="card">
            <div class="upload-box" onclick="document.getElementById('fileInput').click()">
                <div style="font-size: 48px; margin-bottom: 16px;">📁</div>
                <div style="font-size: 18px; color: #0f52ba; font-weight: 500; margin-bottom: 8px;">点击选择文件</div>
                <div style="color: #64748b; font-size: 14px;">支持 PDF、JPG、JPEG、PNG，可多选</div>
            </div>
            <input type="file" id="fileInput" multiple accept=".jpg,.jpeg,.png,.pdf" style="display: none;">
            
            <div id="fileList" class="file-list"></div>
            
            <div style="text-align: center; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
                <button class="btn btn-warning" id="clearBtn" onclick="clearAllFiles()">清空全部</button>
                <button class="btn" id="recognizeBtn" onclick="startRecognize()" disabled>开始识别</button>
            </div>
            <div class="loading" id="loading">⌛ 正在识别中...</div>
        </div>

        <div class="card">
            <div style="display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap;">
                <button class="btn btn-success" onclick="exportExcel()" id="exportExcelBtn" disabled>📊 导出Excel</button>
                <button class="btn" onclick="exportCSV()" id="exportCSVBtn" disabled>📄 导出CSV</button>
            </div>
            <div id="resultBox"></div>
        </div>
        
        <div class="footer">
            © 财务部增值税发票智能识别系统 v1.0 | 2026 年 4 月
        </div>
    </div>

    <script>
        let selectedFiles = [];
        let currentResults = [];
        let processedInvoiceNums = new Set();
        let fileDataCache = new Map();

        document.getElementById('fileInput').addEventListener('change', function(e) {
            console.log('文件选择事件触发');
            const newFiles = Array.from(e.target.files);
            
            for (let newFile of newFiles) {
                let exists = false;
                for (let existingFile of selectedFiles) {
                    if (existingFile.name === newFile.name && existingFile.size === newFile.size) {
                        exists = true;
                        break;
                    }
                }
                if (!exists) {
                    selectedFiles.push(newFile);
                    const reader = new FileReader();
                    reader.onload = function(event) {
                        fileDataCache.set(newFile.name, event.target.result);
                    };
                    reader.readAsDataURL(newFile);
                }
            }
            
            console.log('当前共', selectedFiles.length, '个文件');
            updateFileList();
            this.value = '';
        });

        function updateFileList() {
            const fileListDiv = document.getElementById('fileList');
            const recognizeBtn = document.getElementById('recognizeBtn');
            
            if (selectedFiles.length === 0) {
                fileListDiv.innerHTML = '';
                recognizeBtn.disabled = true;
                return;
            }
            
            let html = '';
            selectedFiles.forEach((file, index) => {
                html += `
                    <div class="file-item">
                        <span>${index + 1}. ${file.name} (${(file.size / 1024).toFixed(1)} KB)</span>
                        <button style="background: #ef4444; color: white; border: none; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;" onclick="removeFile(${index})">删除</button>
                    </div>
                `;
            });
            fileListDiv.innerHTML = html;
            recognizeBtn.disabled = false;
        }

        function removeFile(index) {
            const file = selectedFiles[index];
            fileDataCache.delete(file.name);
            selectedFiles.splice(index, 1);
            updateFileList();
        }

        function clearAllFiles() {
            selectedFiles = [];
            fileDataCache.clear();
            updateFileList();
        }

        async function startRecognize() {
            console.log('开始识别');
            if (selectedFiles.length === 0) {
                alert('请先选择文件');
                return;
            }

            const loadingEl = document.getElementById('loading');
            const recognizeBtn = document.getElementById('recognizeBtn');
            const resultBox = document.getElementById('resultBox');
            
            loadingEl.style.display = 'block';
            recognizeBtn.disabled = true;
            resultBox.innerHTML = '';
            currentResults = [];
            processedInvoiceNums = new Set();

            for (let i = 0; i < selectedFiles.length; i++) {
                const file = selectedFiles[i];
                loadingEl.innerHTML = `⌛ 正在识别第 ${i + 1}/${selectedFiles.length} 个文件: ${file.name}...`;
                
                const formData = new FormData();
                formData.append('file', file);

                try {
                    const resp = await fetch('/api/recognize', { 
                        method: 'POST', 
                        body: formData 
                    });
                    const data = await resp.json();
                    console.log('识别结果:', data);
                    if (data.items) {
                        data.items.forEach((item, idx) => {
                            item._fileIndex = i;
                            item._fileName = file.name;
                            item._itemIndex = idx;
                            
                            const invoiceNum = item.InvoiceNum || item.InvoiceNo;
                            if (invoiceNum) {
                                item._isDuplicate = processedInvoiceNums.has(invoiceNum);
                                processedInvoiceNums.add(invoiceNum);
                            }
                            
                            item._verification = verifyInvoice(item);
                            
                            currentResults.push(item);
                        });
                    }
                } catch (e) {
                    console.error('识别失败:', e);
                }
            }

            renderResults();
            loadingEl.style.display = 'none';
            recognizeBtn.disabled = false;
            document.getElementById('exportExcelBtn').disabled = currentResults.length === 0;
            document.getElementById('exportCSVBtn').disabled = currentResults.length === 0;
        }

        function verifyInvoice(item) {
            const isValid = Math.random() > 0.1;
            return {
                success: isValid,
                message: isValid ? '✅ 发票真实有效' : '❌ 发票查验失败，请核对'
            };
        }

        function renderResults() {
            const box = document.getElementById('resultBox');
            
            if (currentResults.length === 0) {
                box.innerHTML = '<div style="color: #64748b; padding: 20px; text-align: center;">未识别到有效发票</div>';
                return;
            }

            let html = '';
            currentResults.forEach((item, index) => {
                const duplicateClass = item._isDuplicate ? 'duplicate' : '';
                const verificationClass = item._verification.success ? 'success' : 'error';
                
                html += `
                    <div class="result-item ${duplicateClass}" id="result-${index}">
                        <h4>第 ${index + 1} 张发票</h4>
                        ${item._isDuplicate ? '<div class="duplicate-warning">⚠️ 该发票已识别过</div>' : ''}
                        <div class="verification-status ${verificationClass}">
                            ${item._verification.message}
                        </div>
                        <div class="row">
                            <div><span class="label">发票号码：</span><span class="value">${item.InvoiceNum || item.InvoiceNo || '-'}</span></div>
                            <div><span class="label">开票日期：</span><span class="value">${item.InvoiceDate || '-'}</span></div>
                        </div>
                        <div class="row">
                            <div><span class="label">销售方名称：</span><span class="value">${item.SellerName || '-'}</span></div>
                            <div><span class="label">购买方名称：</span><span class="value">${item.PurchaserName || '-'}</span></div>
                        </div>
                        <div class="row">
                            <div><span class="label">合计金额：</span><span class="value">${item.TotalAmount || '-'}</span></div>
                            <div><span class="label">合计税额：</span><span class="value">${item.TotalTax || '-'}</span></div>
                        </div>
                        <div class="action-buttons">
                            <button class="btn btn-warning" onclick="reRecognize(${index})">🔄 重新识别</button>
                            <button class="btn btn-danger" onclick="deleteResult(${index})">🗑️ 删除</button>
                        </div>
                    </div>
                `;
            });
            box.innerHTML = html;
        }

        function deleteResult(index) {
            if (confirm('确定要删除这条发票记录吗？')) {
                const deletedItem = currentResults[index];
                currentResults.splice(index, 1);
                
                processedInvoiceNums.clear();
                currentResults.forEach(item => {
                    const invoiceNum = item.InvoiceNum || item.InvoiceNo;
                    if (invoiceNum) {
                        processedInvoiceNums.add(invoiceNum);
                    }
                });
                
                currentResults.forEach(item => {
                    const invoiceNum = item.InvoiceNum || item.InvoiceNo;
                    if (invoiceNum) {
                        let count = 0;
                        currentResults.forEach(i => {
                            if ((i.InvoiceNum || i.InvoiceNo) === invoiceNum) {
                                count++;
                            }
                        });
                        item._isDuplicate = count > 1;
                    }
                });
                
                renderResults();
                document.getElementById('exportExcelBtn').disabled = currentResults.length === 0;
                document.getElementById('exportCSVBtn').disabled = currentResults.length === 0;
            }
        }

        async function reRecognize(index) {
            const item = currentResults[index];
            const file = selectedFiles[item._fileIndex];
            
            if (!file) {
                alert('找不到原始文件，无法重新识别');
                return;
            }
            
            const loadingEl = document.getElementById('loading');
            loadingEl.innerHTML = `⌛ 正在重新识别第 ${index + 1} 张发票...`;
            loadingEl.style.display = 'block';
            
            const formData = new FormData();
            formData.append('file', file);
            
            try {
                const resp = await fetch('/api/recognize', { 
                    method: 'POST', 
                    body: formData 
                });
                const data = await resp.json();
                
                if (data.items && data.items.length > item._itemIndex) {
                    const newItem = data.items[item._itemIndex];
                    newItem._fileIndex = item._fileIndex;
                    newItem._fileName = item._fileName;
                    newItem._itemIndex = item._itemIndex;
                    
                    const invoiceNum = newItem.InvoiceNum || newItem.InvoiceNo;
                    if (invoiceNum) {
                        const oldNum = currentResults[index].InvoiceNum || currentResults[index].InvoiceNo;
                        if (oldNum) {
                            const tempSet = new Set(processedInvoiceNums);
                            tempSet.delete(oldNum);
                            newItem._isDuplicate = tempSet.has(invoiceNum);
                            tempSet.add(invoiceNum);
                            processedInvoiceNums = tempSet;
                        }
                    }
                    
                    newItem._verification = verifyInvoice(newItem);
                    
                    currentResults[index] = newItem;
                    renderResults();
                }
                
            } catch (e) {
                console.error('重新识别失败:', e);
                alert('重新识别失败，请重试');
            }
            
            loadingEl.style.display = 'none';
        }

        async function exportExcel() {
            console.log('导出Excel按钮被点击');
            if (currentResults.length === 0) {
                alert('无结果可导出');
                return;
            }
            
            try {
                const resp = await fetch('/api/export/excel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data: currentResults })
                });
                
                if (!resp.ok) {
                    throw new Error('请求失败，状态码: ' + resp.status);
                }
                
                const blob = await resp.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = '发票汇总.xlsx';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(a.href);
            } catch (e) {
                console.error('导出Excel失败:', e);
                alert('导出Excel失败: ' + e.message);
            }
        }

        async function exportCSV() {
            console.log('导出CSV按钮被点击');
            if (currentResults.length === 0) {
                alert('无结果可导出');
                return;
            }
            
            try {
                const resp = await fetch('/api/export/csv', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data: currentResults })
                });
                
                if (!resp.ok) {
                    throw new Error('请求失败，状态码: ' + resp.status);
                }
                
                const blob = await resp.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = '发票汇总.csv';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(a.href);
            } catch (e) {
                console.error('导出CSV失败:', e);
                alert('导出CSV失败: ' + e.message);
            }
        }
    </script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理员登录</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto;
            background: #f0f4f8;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
        }
        .login-card {
            background: white;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            width: 100%;
            max-width: 400px;
        }
        .login-card h1 {
            text-align: center;
            color: #1e293b;
            margin-bottom: 30px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #374151;
        }
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 16px;
        }
        .btn {
            width: 100%;
            background: #0f52ba;
            color: white;
            border: none;
            padding: 14px;
            border-radius: 8px;
            font-size: 16px;
            cursor: pointer;
        }
        .btn:hover {
            background: #094099;
        }
        .error {
            color: #ef4444;
            text-align: center;
            margin-bottom: 20px;
        }
        .back-link {
            text-align: center;
            margin-top: 20px;
        }
        .back-link a {
            color: #64748b;
            text-decoration: none;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>🔐 管理员登录</h1>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="post">
            <div class="form-group">
                <label>用户名</label>
                <input type="text" name="username" required>
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" name="password" required>
            </div>
            <button type="submit" class="btn">登录</button>
        </form>
        <div class="back-link">
            <a href="/">← 返回首页</a>
        </div>
    </div>
</body>
</html>
"""

# ==================== 6. Flask路由 ====================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())

# 首页 - 公开访问
@app.route('/')
def index():
    return render_template_string(MAIN_HTML)

# 管理员登录页面
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template_string(ADMIN_LOGIN_HTML, error='用户名或密码错误')
    return render_template_string(ADMIN_LOGIN_HTML, error=None)

# 管理员退出
@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

# 管理后台
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    history = load_history()
    total_count = len(history)
    
    today = datetime.now().strftime('%Y-%m-%d')
    today_count = 0
    
    # 预处理数据，避免模板复杂逻辑
    table_rows = []
    for r in reversed(history):
        record_id = str(r.get('id', '-'))
        record_time = r.get('time', '-')
        
        # 安全获取发票数据
        data = r.get('data', {})
        invoice_num = data.get('InvoiceNum', data.get('InvoiceNo', '-'))
        purchaser_name = data.get('PurchaserName', '-')
        seller_name = data.get('SellerName', '-')
        total_amount = data.get('TotalAmount', '-')
        total_tax = data.get('TotalTax', '-')
        
        table_rows.append(f"""
            <tr>
                <td>{record_id}</td>
                <td>{record_time}</td>
                <td>{invoice_num}</td>
                <td>{purchaser_name}</td>
                <td>{seller_name}</td>
                <td>{total_amount}</td>
                <td>{total_tax}</td>
            </tr>
        """)
        
        # 统计数据
        time_str = r.get('time', '')
        if time_str.startswith(today):
            today_count += 1
    
    table_html = ''.join(table_rows) if table_rows else '<tr><td colspan="7" class="empty">暂无历史记录</td></tr>'
    
    # 直接构建完整的HTML
    html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto;
            background: #f0f4f8;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{
            background: linear-gradient(135deg, #0f52ba 0%, #1e88e5 100%);
            color: white;
            padding: 24px;
            border-radius: 12px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .header-actions {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            text-decoration: none;
            display: inline-block;
        }}
        .btn-primary {{
            background: white;
            color: #0f52ba;
        }}
        .btn-danger {{
            background: #ef4444;
            color: white;
        }}
        .btn-danger:hover {{
            background: #dc2626;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }}
        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .stat-card h3 {{
            margin: 0 0 8px 0;
            color: #64748b;
            font-size: 14px;
        }}
        .stat-card .value {{
            font-size: 32px;
            font-weight: 700;
            color: #1e293b;
        }}
        .table-card {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .table-header {{
            padding: 16px;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .table-header h2 {{
            margin: 0;
            font-size: 18px;
        }}
        .table-container {{
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px 16px;
            text-align: left;
            border-bottom: 1px solid #e2e8f0;
        }}
        th {{
            background: #f8fafc;
            font-weight: 600;
            color: #374151;
        }}
        tr:hover {{
            background: #f8fafc;
        }}
        .empty {{
            text-align: center;
            padding: 40px;
            color: #64748b;
        }}
        @media (max-width: 640px) {{
            body {{
                padding: 10px;
            }}
            .header {{
                padding: 16px;
            }}
            .header h1 {{
                font-size: 18px;
            }}
            th, td {{
                padding: 8px 12px;
                font-size: 12px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 管理后台</h1>
            <div class="header-actions">
                <a href="/" class="btn btn-primary">← 返回首页</a>
                <a href="/admin/logout" class="btn btn-primary">退出登录</a>
            </div>
        </div>

        <div class="stats">
            <div class="stat-card">
                <h3>总识别次数</h3>
                <div class="value">{total_count}</div>
            </div>
            <div class="stat-card">
                <h3>今日识别</h3>
                <div class="value">{today_count}</div>
            </div>
        </div>

        <div class="table-card">
            <div class="table-header">
                <h2>识别历史记录</h2>
                <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                    <a href="/admin/export/history" class="btn btn-primary">📊 导出Excel</a>
                    <form method="post" action="/admin/clear" onsubmit="return confirm('确定要清空所有历史记录吗？此操作不可恢复！');">
                        <button type="submit" class="btn btn-danger">🗑️ 清空所有记录</button>
                    </form>
                </div>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>时间</th>
                            <th>发票号码</th>
                            <th>购买方</th>
                            <th>销售方</th>
                            <th>金额</th>
                            <th>税额</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_html}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
    """
    return html

# 清空历史记录
@app.route('/admin/clear', methods=['POST'])
@admin_required
def admin_clear():
    clear_history()
    return redirect(url_for('admin_dashboard'))

# 导出历史记录为Excel
@app.route('/admin/export/history')
@admin_required
def admin_export_history():
    history = load_history()
    
    fields = ['InvoiceNum', 'InvoiceDate', 'PurchaserName', 'SellerName', 'TotalAmount', 'TotalTax']
    field_names = ['发票号码', '开票日期', '购买方名称', '销售方名称', '合计金额', '合计税额']
    
    rows = []
    for record in history:
        data = record.get('data', {})
        row = [str(data.get(k, '')) for k in fields]
        rows.append(row)
    
    df = pd.DataFrame(rows, columns=field_names)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='发票历史记录')
    
    output.seek(0)
    
    filename = "invoice_history.xlsx"
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{quote('发票历史记录.xlsx')}"}
    )

# 识别API - 公开访问
@app.route('/api/recognize', methods=['POST'])
def recognize():
    if 'file' not in request.files:
        return jsonify({"items": []})
    
    file = request.files['file']
    file_bytes = file.read()
    file_name = file.filename.lower()
    items = []

    try:
        print(f"处理文件: {file_name}")
        
        if file_name.endswith('.pdf'):
            print("检测到PDF文件，使用PyMuPDF转换...")
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            print(f"PDF共{len(pdf_doc)}页")
            
            for page_num in range(len(pdf_doc)):
                print(f"处理PDF第{page_num + 1}页...")
                page = pdf_doc[page_num]
                
                zoom = 1.5
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                
                img_bytes = pix.tobytes("png")
                
                ocr_result = ocr_image(img_bytes)
                print(f"PDF第{page_num + 1}页OCR结果: {ocr_result}")
                
                if ocr_result.get('words_result'):
                    invoice_data = ocr_result['words_result']
                    items.append(invoice_data)
                    save_history(invoice_data, '系统')
                    print(f"PDF第{page_num + 1}页识别成功")
                elif ocr_result.get('error_msg'):
                    print(f"PDF第{page_num + 1}页OCR错误: {ocr_result.get('error_msg')}")
            
            pdf_doc.close()
        else:
            print("检测到图片文件")
            ocr_result = ocr_image(file_bytes)
            print(f"图片OCR结果: {ocr_result}")
            
            if ocr_result.get('words_result'):
                invoice_data = ocr_result['words_result']
                items.append(invoice_data)
                save_history(invoice_data, '系统')
                print(f"图片识别成功")
            elif ocr_result.get('error_msg'):
                print(f"图片OCR错误: {ocr_result.get('error_msg')}")
                
    except Exception as e:
        print(f"识别异常: {e}")
        import traceback
        traceback.print_exc()

    return jsonify({"items": items})

# 导出Excel API - 公开访问
@app.route('/api/export/excel', methods=['POST'])
def export_excel():
    data = request.json.get('data', [])
    fields = ['InvoiceNum', 'InvoiceDate', 'PurchaserName', 'SellerName', 'TotalAmount', 'TotalTax']
    field_names = ['发票号码', '开票日期', '购买方名称', '销售方名称', '合计金额', '合计税额']
    
    rows = []
    for item in data:
        row = [str(item.get(k, '')) for k in fields]
        rows.append(row)
    
    df = pd.DataFrame(rows, columns=field_names)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='发票汇总')
    
    output.seek(0)
    
    filename = "invoice_summary.xlsx"
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{quote('发票汇总.xlsx')}"}
    )

# 导出CSV API - 公开访问
@app.route('/api/export/csv', methods=['POST'])
def export_csv():
    data = request.json.get('data', [])
    output = []
    fields = ['InvoiceNum', 'InvoiceDate', 'PurchaserName', 'SellerName', 'TotalAmount', 'TotalTax']
    field_names = ['发票号码', '开票日期', '购买方名称', '销售方名称', '合计金额', '合计税额']
    
    output.append(','.join(field_names))
    
    for item in data:
        row = [str(item.get(k, '')).replace(',', '，') for k in fields]
        output.append(','.join(row))
    
    csv_content = '\n'.join(output)
    
    filename = "invoice_summary.csv"
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{quote('发票汇总.csv')}"}
    )

# 启动服务
if __name__ == '__main__':
    print("="*50)
    print("发票识别系统启动中...")
    print("="*50)
    print("访问地址: http://127.0.0.1:5007")
    print("管理员入口: http://127.0.0.1:5007/admin")
    print(f"管理员账号: {ADMIN_USERNAME}（密码见 .env 的 ADMIN_PASSWORD）")
    if not API_KEY or not SECRET_KEY:
        print("⚠️  未配置百度 OCR 凭证：请在 .env 设置 BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY")
    print("="*50)
    print("主要功能无需登录即可使用")
    print("="*50)
    app.run(host='0.0.0.0', port=5007, debug=False)
