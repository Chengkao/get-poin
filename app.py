"""C 作業評分網站(只在你自己的電腦上執行)。

啟動:  python app.py   然後瀏覽器開 http://127.0.0.1:5000
"""
import csv
import io
import json
import os
import re
import shutil
import threading
import uuid
import webbrowser
import zipfile
from collections import Counter

from flask import (Flask, Response, abort, redirect, render_template_string,
                   request, url_for)

import grader

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(BASE, "jobs")
os.makedirs(JOBS, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


# ---------- 工具 ----------
def job_dir(job):
    if not re.fullmatch(r"[0-9a-f]{12}", job):
        abort(404)
    d = os.path.join(JOBS, job)
    if not os.path.isdir(d):
        abort(404)
    return d


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    return re.sub(r'[:*?"<>|\x00-\x1f]', "_", name).strip() or "unnamed.c"


def unique_path(folder, name):
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    return path


def zip_member_name(info):
    """舊式壓縮檔檔名常是 Big5,Python 會誤當 cp437,這裡修正回來。"""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("cp950")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def extract_student_filename(rel_path):
    """將包含資料夾的路徑(如 'B10901001/main.c' 或 '作業1/B10901001/code.c')
    轉換成 'B10901001_code.c'，把資料夾名稱作為學號識別。
    """
    clean_path = (rel_path or "").replace("\\", "/").strip("/")
    parts = [p for p in clean_path.split("/") if p and p != "."]
    
    if len(parts) >= 2:
        # 倒數第二層通常是學生的子資料夾，最後一層是檔名
        folder_name = safe_name(parts[-2])
        file_name = safe_name(parts[-1])
        return f"{folder_name}_{file_name}"
    elif len(parts) == 1:
        return safe_name(parts[0])
    return "unnamed.c"


def save_students(file_list, folder):
    saved, skipped = 0, []
    for fs in file_list:
        if not fs or not fs.filename:
            continue
        
        # 解析包含子資料夾的相對路徑
        name = extract_student_filename(fs.filename)
        low = name.lower()
        
        if low.endswith(".c"):
            fs.save(unique_path(folder, name))
            saved += 1
        elif low.endswith(".zip"):
            try:
                with zipfile.ZipFile(fs.stream) as zf:
                    for info in zf.infolist():
                        raw_mname = zip_member_name(info)
                        if info.is_dir() or not raw_mname.lower().endswith(".c") \
                                or info.file_size > 2 * 1024 * 1024:
                            continue
                        
                        # 壓縮檔內的子資料夾同樣自動前綴化
                        mname = extract_student_filename(raw_mname)
                        with zf.open(info) as src, open(unique_path(folder, mname), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        saved += 1
            except zipfile.BadZipFile:
                skipped.append(fs.filename + "(壓縮檔損壞)")
        else:
            skipped.append(fs.filename)
            
    return saved, skipped


def parse_opts(form):
    try:
        timeout = max(1, min(30, int(form.get("timeout", 5))))
    except ValueError:
        timeout = 5
    mode = form.get("mode", "standard")
    return {
        "input": (form.get("input") or "").replace("\r\n", "\n"),
        "mode": mode if mode in ("standard", "loose") else "standard",
        "timeout": timeout,
    }


def evaluate(job, form):
    d = job_dir(job)
    opts = parse_opts(form)
    keywords_text = form.get("keywords", "")
    roster_text = form.get("roster", "")
    roster = [x for x in re.split(r"[\s,;,、]+", roster_text) if x]
    ref, rows = grader.grade_folder(
        os.path.join(d, "reference.c"), os.path.join(d, "students"),
        opts, keywords_text, roster)
    meta_path = os.path.join(d, "meta.json")
    skipped = []
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            skipped = json.load(f).get("skipped", [])
    old_path = os.path.join(d, "result.json")
    overrides = {}
    if os.path.exists(old_path):
        with open(old_path, encoding="utf-8") as f:
            overrides = json.load(f).get("overrides", {})
    result = {"opts": opts, "roster": roster_text, "keywords": ref["keywords"],
              "expected": ref["output"], "rows": rows, "skipped": skipped,
              "overrides": overrides}
    save_result(d, result)


def save_result(d, data):
    with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_result(d):
    with open(os.path.join(d, "result.json"), encoding="utf-8") as f:
        data = json.load(f)
    ov = data.get("overrides", {})
    for r in data["rows"]:
        r["key"] = f"{r['student']}|{r['file']}"
        r["final"] = ov.get(r["key"], r["score"])
        r["overridden"] = r["final"] != r["score"]
    return data


def error_page(msg, status=400):
    return render_template_string(ERROR_HTML, msg=msg), status


# ---------- 路由 ----------
@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.post("/grade")
def grade():
    ref = request.files.get("reference")
    if not ref or not ref.filename:
        return error_page("請上傳範例檔(.c)。")
    job = uuid.uuid4().hex[:12]
    d = os.path.join(JOBS, job)
    os.makedirs(os.path.join(d, "students"))
    ref.save(os.path.join(d, "reference.c"))
    saved, skipped = save_students(request.files.getlist("students"), os.path.join(d, "students"))
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"skipped": skipped}, f, ensure_ascii=False)
    if saved == 0:
        shutil.rmtree(d, ignore_errors=True)
        return error_page("沒有收到任何學生的 .c 檔(也可以上傳含 .c 的 .zip)。")
    try:
        evaluate(job, request.form)
    except RuntimeError as e:
        shutil.rmtree(d, ignore_errors=True)
        return error_page(str(e))
    return redirect(url_for("result", job=job))


@app.post("/regrade/<job>")
def regrade(job):
    job_dir(job)
    try:
        evaluate(job, request.form)
    except RuntimeError as e:
        return error_page(str(e))
    return redirect(url_for("result", job=job))


@app.get("/result/<job>")
def result(job):
    data = load_result(job_dir(job))
    counts = sorted(Counter(r["final"] for r in data["rows"]).items(), reverse=True)
    review_n = sum(1 for r in data["rows"] if r.get("review") and not r["overridden"])
    only = request.args.get("only") == "review"
    rows = [(i, r) for i, r in enumerate(data["rows"]) if not only or r.get("review")]
    return render_template_string(RESULT_HTML, job=job, d=data, counts=counts,
                                  total=len(data["rows"]), rows=rows,
                                  review_n=review_n, only=only)


@app.post("/override/<job>")
def override(job):
    d = job_dir(job)
    data = load_result(d)
    ov = dict(data.get("overrides", {}))
    for i, r in enumerate(data["rows"]):
        field = f"ov_{i}"
        if field not in request.form:  # 篩選時沒顯示的列,維持原本設定
            continue
        v = request.form[field]
        if v.isdigit() and int(v) in (0, 50, 60, 100) and int(v) != r["score"]:
            ov[r["key"]] = int(v)
        else:
            ov.pop(r["key"], None)
    data["overrides"] = ov
    for r in data["rows"]:
        for k in ("key", "final", "overridden"):
            r.pop(k, None)
    save_result(d, data)
    return redirect(url_for("result", job=job, only=request.args.get("only")))


@app.get("/csv/<job>")
def csv_download(job):
    data = load_result(job_dir(job))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["學號/名稱", "檔名", "最終分數", "自動分數", "判斷", "是否人工調整", "說明"])
    for r in data["rows"]:
        w.writerow([r["student"], r["file"], r["final"], r["score"], r["label"],
                    "是" if r["overridden"] else "", r["note"]])
    body = "\ufeff" + buf.getvalue()  # BOM,讓 Excel 正確顯示中文
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=scores_{job}.csv"})


# ---------- 頁面 ----------
STYLE = """
<style>
:root{--bg:#fafaf8;--fg:#222;--muted:#666;--card:#fff;--line:#ddd;--accent:#2a6df4}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#eee;--muted:#9a9a9a;--card:#2a2a2d;--line:#444;--accent:#6b9bff}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Microsoft JhengHei",sans-serif}
main{max-width:960px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:24px 0 8px}
.muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=text],input[type=number],textarea,select{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px}
button,.btn{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:9px 18px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
ttable{
    width:100%;
    border-collapse:collapse;
}

th,td{
    text-align:left;
    padding:7px 8px;
    border-bottom:1px solid var(--line);
    vertical-align:top;
}

/* 成績表格 */
.result-table{
    width:100%;
    table-layout:fixed;
}

.result-table th:nth-child(1),
.result-table td:nth-child(1){
    width:160px;
    min-width:160px;
    max-width:160px;
    overflow-wrap:anywhere;
    word-break:break-word;
}

.result-table th:nth-child(2),
.result-table td:nth-child(2){
    width:220px;
    min-width:220px;
    max-width:220px;
    overflow-wrap:anywhere;
    word-break:break-word;
}

/* 讓其他欄位使用剩餘空間 */
.result-table th:nth-child(3),
.result-table td:nth-child(3){
    width:70px;
}

.result-table th:nth-child(4),
.result-table td:nth-child(4){
    width:100px;
}

.result-table th:nth-child(5),
.result-table td:nth-child(5){
    width:auto;
    overflow-wrap:anywhere;
    word-break:break-word;
}

.result-table th:nth-child(6),
.result-table td:nth-child(6){
    width:100px;
}
.badge{display:inline-block;min-width:44px;text-align:center;border-radius:12px;padding:1px 8px;font-weight:700;color:#fff}
.s100{background:#2e9e5b}.s60{background:#d99a1c}.s50{background:#d9661c}.s0{background:#c0392b}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;overflow-x:auto;white-space:pre-wrap;margin:6px 0}
details summary{cursor:pointer;color:var(--accent)}
.chips span{display:inline-block;margin:0 6px 6px 0}
</style>
"""

INDEX_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>C 作業評分</title>""" + STYLE + """</head><body><main>
<h1>C 作業評分</h1>
<p class="muted">每次批改都可以換不同的範例。上傳範例檔和學生檔案,幾秒後就有分數表。</p>
<form method="post" action="/grade" enctype="multipart/form-data">
<div class="card">
<label>① 範例檔(課本的範例,.c)</label>
<input type="file" name="reference" accept=".c" required>
<label>② 學生檔案(可多選 .c,或上傳含 .c 的 .zip)</label>
<input type="file" name="students" accept=".c,.zip" multiple>
<label>或選擇整個資料夾</label>
<input type="file" name="students" webkitdirectory multiple>
</div>
<details class="card"><summary>進階設定(多半不用改)</summary>
<label>測試輸入(範例需要 scanf 輸入時才填,每行一筆)</label>
<textarea name="input" rows="3"></textarea>
<label>提醒用關鍵字(只做提示、不影響分數;例如 printf, for;留空 = 從範例自動偵測)</label>
<input type="text" name="keywords">
<label>輸出比對方式</label>
<select name="mode"><option value="standard">標準:文字一字不差(忽略行尾空白、結尾空行)</option>
<option value="loose">寬鬆:忽略所有空白與換行差異</option></select>
<label>執行時間上限(秒)</label>
<input type="number" name="timeout" value="5" min="1" max="30">
<label>學號名單(選填,每行一個;名單上沒交的會記 0 分)</label>
<textarea name="roster" rows="3"></textarea>
</details>
<button type="submit">開始評分</button>
</form>
<h2>評分規則</h2>
<div class="card" style="overflow-x:auto;">
<table class="result-table">
<tr><td><span class="badge s100">100</span></td><td>有交、能跑、答案正確</td></tr>
<tr><td><span class="badge s60">60</span></td><td>有交、能跑,但答案錯誤(60 分及格,會標示待人工確認)</td></tr>
<tr><td><span class="badge s50">50</span></td><td>有交,但不能跑(編譯失敗、無窮迴圈、當機)</td></tr>
<tr><td><span class="badge s0">0</span></td><td>空白或未繳交</td></tr></table>
<p class="muted">程式只在這台電腦上編譯執行。<code>system(...)</code> 呼叫會被略過(所以 <code>system("pause")</code> 不會卡住),。</p></div>
</main></body></html>"""

RESULT_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>評分結果</title>""" + STYLE + """</head><body><main>
<h1>評分結果</h1>
<p><a href="/">← 批改另一份作業</a></p>
<div class="card">
<div class="chips">共 {{ total }} 份:
{% for score, n in counts %}<span><span class="badge s{{ score }}">{{ score }}</span> × {{ n }}</span>{% endfor %}</div>
{% if review_n %}<p>⚠ 有 <b>{{ review_n }}</b> 份輸出和範例不同,建議人工確認。
{% if only %}<a href="/result/{{ job }}">看全部</a>{% else %}<a href="/result/{{ job }}?only=review">只看待確認的</a>{% endif %}</p>{% endif %}
<p class="muted">提醒用關鍵字(沒用到只會在說明欄提醒,不扣分):{{ d.keywords|join("、") or "(無)" }}</p>
{% if d.skipped %}<p class="muted">略過的檔案(不是 .c):{{ d.skipped|join("、") }}</p>{% endif %}
<a class="btn" href="/csv/{{ job }}">下載成績 CSV</a>
</div>
<details class="card"><summary>範例的標準輸出</summary><pre>{{ d.expected }}</pre></details>
<form method="post" action="/override/{{ job }}{{ '?only=review' if only }}">
<div class="card" style="overflow-x:auto;">
<table class="result-table">
<tr>
    <th>學號/名稱</th>
    <th>檔名</th>
    <th>分數</th>
    <th>判斷</th>
    <th>說明</th>
    <th>人工調整</th>
</tr>
{% for i, r in rows %}
<tr style="{{ 'background:rgba(217,154,28,.12)' if r.review and not r.overridden }}">
<td>{{ r.student }}</td><td>{{ r.file }}</td>
<td><span class="badge s{{ r.final }}">{{ r.final }}</span>{% if r.overridden %}<div class="muted">自動 {{ r.score }}</div>{% endif %}</td>
<td>{{ r.label }}{% if r.review %}<div class="muted">待人工確認</div>{% endif %}</td>
<td>{{ r.note }}
{% if r.error or r.output or r.diff %}<details><summary>查看詳情</summary>
{% if r.diff %}<div class="muted">和範例的差異(- 範例、+ 學生):</div><pre>{{ r.diff }}</pre>{% endif %}
{% if r.error %}<pre>{{ r.error }}</pre>{% endif %}
{% if r.output %}<div class="muted">學生程式輸出:</div><pre>{{ r.output }}</pre>{% endif %}
</details>{% endif %}</td>
<td><input type="hidden" name="key_{{ i }}" value="{{ r.key }}">
<select name="ov_{{ i }}">
<option value="">依自動</option>
{% for sc in (100, 60, 50, 0) %}<option value="{{ sc }}" {{ 'selected' if r.overridden and r.final == sc }}>{{ sc }}</option>{% endfor %}
</select></td></tr>
{% endfor %}</table></div>
<p><button type="submit">儲存人工調整</button></p>
</form>
<details class="card"><summary>調整設定後重新評分(不用重新上傳)</summary>
<form method="post" action="/regrade/{{ job }}">
<label>測試輸入</label><textarea name="input" rows="3">{{ d.opts.input }}</textarea>
<label>提醒用關鍵字(只做提示、不影響分數;留空 = 自動偵測)</label>
<input type="text" name="keywords" value="{{ d.keywords|join(', ') }}">
<label>輸出比對方式</label>
<select name="mode"><option value="standard" {{ 'selected' if d.opts.mode=='standard' }}>標準</option>
<option value="loose" {{ 'selected' if d.opts.mode=='loose' }}>寬鬆(忽略所有空白)</option></select>
<label>執行時間上限(秒)</label><input type="number" name="timeout" value="{{ d.opts.timeout }}" min="1" max="30">
<label>學號名單</label><textarea name="roster" rows="3">{{ d.roster }}</textarea>
<p><button type="submit">重新評分</button></p></form></details>
</main></body></html>"""

ERROR_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>錯誤</title>""" + STYLE + """</head><body><main>
<h1>無法評分</h1><div class="card"><pre>{{ msg }}</pre></div><p><a href="/">← 回上一頁</a></p></main></body></html>"""


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
