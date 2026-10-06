"""C 作業自動評分核心。

評分規則:
  0   空白(沒有任何程式碼)或未繳交
  50  有交但不能跑(編譯失敗、逾時、當機)
  60  有交能跑,但輸出和範例不同(建議人工確認)
  100 有交、能跑、答案正確
(60 分及格。關鍵字只用來提醒「沒用到範例的語法」,不影響分數。)
"""
import difflib
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

COMPILE_TIMEOUT = 30
CONTROL_WORDS = {"for", "while", "if", "switch"}
IGNORE_NAMES = {"main", "sizeof", "return"}

# 在每份程式前先載入:把 system(...) 變成什麼都不做。
# 這樣 system("pause") 不會卡住,Windows / Linux 輸出一致,學生程式也不能藉 system 執行指令。
PREFIX = '#include <stdlib.h>\n#undef system\n#define system(x) ((void)(x), 0)\n'

FALLBACK_FLAGS = ["-w", "-std=gnu99"]
GCC_FLAGS = [
    "-w", "-std=gnu99",
    "-Wno-error=implicit-function-declaration",
    "-Wno-error=implicit-int",
    "-Wno-error=int-conversion",
    "-Wno-error=incompatible-pointer-types",
]


# ---------- 讀檔與編碼 ----------
def decode_bytes(data):
    """依序嘗試 UTF-8、Big5(cp950),都失敗才用取代字元。"""
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def read_source(path):
    with open(path, "rb") as f:
        return decode_bytes(f.read())


# ---------- 程式碼分析 ----------
_LITERAL_OR_COMMENT = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S
)


def strip_code(src):
    """移除註解與字串,只留程式本體,用來判斷是否空白與找關鍵字。"""
    code = _LITERAL_OR_COMMENT.sub(" ", src)
    return re.sub(r"^\s*#.*$", " ", code, flags=re.M)


def detect_keywords(ref_src):
    """從範例程式自動找出『要教的重點』:呼叫過的函式與流程控制。"""
    code = strip_code(ref_src)
    names = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code))
    defined = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^()]*\)\s*\{", code)) - CONTROL_WORDS
    names -= defined | IGNORE_NAMES
    if re.search(r"\bdo\b", code):
        names.add("do")
    return sorted(names)


def uses_keyword(code, kw):
    if kw == "do":
        return re.search(r"\bdo\b", code) is not None
    return re.search(r"\b" + re.escape(kw) + r"\s*\(", code) is not None


def parse_keywords(text):
    return [k for k in re.split(r"[\s,;,、]+", text or "") if k]


# ---------- 編譯與執行 ----------
def normalize(text, mode):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "loose":  # 寬鬆:忽略所有空白
        return re.sub(r"\s+", "", text)
    lines = [ln.rstrip() for ln in text.split("\n")]  # 標準:忽略行尾空白與結尾空行
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def compile_and_run(src_text, stdin_text="", timeout=5):
    """回傳 dict: stage(compile/run/ok)、output、error。"""
    work = tempfile.mkdtemp(prefix="cgrade_")
    try:
        c_path = os.path.join(work, "prog.c")
        pre_path = os.path.join(work, "prefix.h")
        exe = os.path.join(work, "prog.exe" if os.name == "nt" else "prog")
        with open(c_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(src_text)
        with open(pre_path, "w", encoding="utf-8") as f:
            f.write(PREFIX)
        def run_gcc(flags):
            return subprocess.run(
                ["gcc", *flags, "-include", pre_path, c_path, "-o", exe, "-lm"],
                capture_output=True, timeout=COMPILE_TIMEOUT, cwd=work,
            )
        try:
            cp = run_gcc(GCC_FLAGS)
            # 舊版 gcc(例如 Dev-C++ 內建的)不認得某些新的警告參數,改用最基本的參數重試
            if cp.returncode != 0 and re.search(
                    rb"no option|unrecognized command[- ]line option|unknown warning", cp.stderr):
                cp = run_gcc(FALLBACK_FLAGS)
        except FileNotFoundError:
            raise RuntimeError("找不到 gcc,請先安裝(Windows 可裝 MinGW-w64 或 MSYS2)")
        except subprocess.TimeoutExpired:
            return {"stage": "compile", "output": "", "error": "編譯逾時"}
        if cp.returncode != 0:
            return {"stage": "compile", "output": "", "error": decode_bytes(cp.stderr)[:2000]}
        try:
            rp = subprocess.run(
                [exe], input=stdin_text.encode("utf-8"), capture_output=True,
                timeout=timeout, cwd=work,
            )
        except subprocess.TimeoutExpired as e:
            return {"stage": "run", "output": decode_bytes(e.stdout or b"")[:2000],
                    "error": f"執行超過 {timeout} 秒(可能是無窮迴圈或在等輸入)"}
        rc = rp.returncode
        if rc < 0 or rc >= 0xC0000000:
            return {"stage": "run", "output": decode_bytes(rp.stdout)[:2000],
                    "error": f"程式當機(結束代碼 {rc})"}
        return {"stage": "ok", "output": decode_bytes(rp.stdout), "error": ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- 評分 ----------
def grade_one(name, src, ref, opts):
    """ref: {'src','output','keywords'};opts: {'input','mode','timeout'}"""
    row = {"file": name, "score": 0, "label": "", "note": "", "output": "", "error": ""}
    code = strip_code(src)
    if not code.strip():
        row.update(score=0, label="空白", note="檔案沒有任何程式碼")
        return row

    res = compile_and_run(src, opts["input"], opts["timeout"])
    row["output"], row["error"] = res["output"], res["error"]
    if res["stage"] == "compile":
        row.update(score=50, label="不能跑", note="編譯失敗")
        return row
    if res["stage"] == "run":
        row.update(score=50, label="不能跑", note=res["error"])
        return row

    exp = normalize(ref["output"], opts["mode"])
    got = normalize(res["output"], opts["mode"])
    if got != exp:
        sim = round(difflib.SequenceMatcher(None, exp, got).ratio() * 100)
        diff = list(difflib.unified_diff(exp.split("\n"), got.split("\n"),
                                         "範例輸出", "學生輸出", lineterm="", n=0))
        row.update(score=60, label="答案錯誤", review=True, similarity=sim,
                   note=f"輸出和範例不同(相似度 {sim}%),建議人工確認",
                   diff="\n".join(diff[:40]))
        return row

    missing = [k for k in ref["keywords"] if not uses_keyword(code, k)]
    row.update(score=100, label="正確")
    if missing:  # 只提醒,不扣分
        row["note"] = "提醒:沒用到 " + "、".join(missing)
    if " ".join(src.split()) == " ".join(ref["src"].split()):
        row["note"] = (row["note"] + " ").strip() + "(內容與範例檔完全相同)"
    return row


def prepare_reference(ref_path, opts, keywords_text=""):
    src = read_source(ref_path)
    res = compile_and_run(src, opts["input"], opts["timeout"])
    if res["stage"] != "ok":
        raise RuntimeError("範例檔本身無法編譯或執行:" + (res["error"] or "未知錯誤"))
    kws = parse_keywords(keywords_text) or detect_keywords(src)
    return {"src": src, "output": res["output"], "keywords": kws}


def grade_folder(ref_path, student_dir, opts, keywords_text="", roster=None):
    """批改資料夾內所有 .c。roster 是學號清單,名單上沒有對應檔案的記 0 分。"""
    ref = prepare_reference(ref_path, opts, keywords_text)
    files = sorted(f for f in os.listdir(student_dir) if f.lower().endswith(".c"))

    def work(fn):
        return grade_one(fn, read_source(os.path.join(student_dir, fn)), ref, opts)

    with ThreadPoolExecutor(max_workers=4) as ex:
        rows = list(ex.map(work, files))

    for r in rows:
        stem = os.path.splitext(r["file"])[0]
        r["student"] = next((i for i in (roster or []) if i in r["file"]), stem)

    for sid in roster or []:
        if not any(sid in r["file"] for r in rows):
            rows.append({"file": "(未繳交)", "student": sid, "score": 0,
                         "label": "未繳交", "note": "名單上有、但找不到對應檔案",
                         "output": "", "error": ""})
    rows.sort(key=lambda r: (r["student"], r["file"]))
    return ref, rows
