import os, re, json, time, uuid, datetime, platform, shutil, glob
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# 1) 上传时的临时目录（原始文件暂存）
UPLOAD_TMP_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)

# 2) 教案 JSON 的持久目录：/upload （你要求）
LESSONS_DIR = os.path.join(BASE_DIR, "upload")
os.makedirs(LESSONS_DIR, exist_ok=True)

# 默认教案（静态）
DEFAULT_LESSON_ID = "lesson_bio"  # 在索引与 UI 中的 id
DEFAULT_LESSON_PATH = os.path.join(STATIC_DIR, "lesson1.json")

# Windows: auto-detect poppler / tesseract（可按需硬编码）
POPPLER_PATH = os.environ.get("POPPLER_PATH", None)
if platform.system().lower().startswith("win") and not POPPLER_PATH:
    for guess in [
        r"C:\Program Files\poppler-24.02.0\Library\bin",
        r"C:\Program Files\poppler-23.11.0\Library\bin",
        r"C:\poppler\Library\bin",
    ]:
        if os.path.exists(guess):
            POPPLER_PATH = guess
            os.environ["POPPLER_PATH"] = POPPLER_PATH
            break
try:
    import pytesseract
    if platform.system().lower().startswith("win"):
        if not shutil.which("tesseract"):
            for exe in [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]:
                if os.path.exists(exe):
                    pytesseract.pytesseract.tesseract_cmd = exe
                    break
except Exception:
    pass

# ---- Session（demo 内存实现）----
SESSION = {
    "lesson_id": DEFAULT_LESSON_ID,
    "teacher_starts": {},
    "student_starts": {},
    "tolerance_sec": 30
}

# ---- 课程索引（用于发布/排序/班级可见等元数据）----
INDEX_PATH = os.path.join(STATIC_DIR, "lessons_index.json")

def load_index():
    if not os.path.exists(INDEX_PATH):
        return {"lessons": []}
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_index(idx):
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)

def ensure_default_index():
    """确保默认生物教案出现在索引里（published=true）"""
    idx = load_index()
    lessons = idx.get("lessons", [])
    found = next((x for x in lessons if x.get("id") == DEFAULT_LESSON_ID), None)
    if not found:
        # 读取默认生物教案 title
        title = "Sample Lesson · Cell Structure & Function"
        try:
            with open(DEFAULT_LESSON_PATH, "r", encoding="utf-8") as f:
                title = json.load(f).get("title", title)
        except Exception:
            pass
        lessons.append({
            "id": DEFAULT_LESSON_ID,
            "title": title,
            "published": True,
            "classes": [],
            "order": 1,
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z"
        })
        idx["lessons"] = lessons
        save_index(idx)

ensure_default_index()

# ---- lesson JSON 读写 ----
def lesson_path(lesson_id: str):
    """优先在 /upload 中找；找不到再落回默认静态生物教案"""
    candidate = os.path.join(LESSONS_DIR, f"{lesson_id}.json")
    if os.path.exists(candidate):
        return candidate
    if lesson_id == DEFAULT_LESSON_ID:
        return DEFAULT_LESSON_PATH
    # 不存在则抛错
    return candidate

def load_lesson(lesson_id):
    path = lesson_path(lesson_id)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_lesson(lesson_id, title, content_html):
    """所有新/改教案保存到 /upload 目录"""
    path = os.path.join(LESSONS_DIR, f"{lesson_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"title": title, "content_html": content_html}, f, ensure_ascii=False, indent=2)

# ---- HTML & 标注工具 ----
def _escape_html(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

def _plain_text_to_html_blocks(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    blocks, buf = [], []
    def flush():
        if buf:
            blocks.append("<p>" + _escape_html(" ".join(buf)) + "</p>")
            buf.clear()
    for ln in lines:
        if not ln:
            flush(); continue
        if ln.endswith(":") or re.match(r'^\s*(I+\.|[A-Z]\.|[0-9]+\.)\s', ln):
            flush()
            blocks.append(f"<h3>{_escape_html(ln.rstrip(':'))}</h3>")
        else:
            buf.append(ln)
    flush()
    return "\n".join(blocks).strip() or ("<p>"+_escape_html(text)+"</p>")

def render_targets_from_html(raw_html):
    targets = []
    idx = 0
    pat = re.compile(r"<H>(.*?)</H>", re.DOTALL)
    def repl(m):
        nonlocal idx
        idx += 1
        inner = m.group(1)
        targets.append({"id": f"t{idx}", "text": inner})
        return f'<span class="target need-highlight" data-target-id="t{idx}">{inner}</span>'
    html = pat.sub(repl, raw_html)
    return html, targets

# ---- 文档解析（文本层优先 / 失败走 OCR）----
def extract_text_from_pdf_textlayer(pdf_path: str) -> str:
    try:
        from pdfminer.high_level import extract_text
        txt = extract_text(pdf_path) or ""
        if txt.strip(): return txt
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        out = []
        for p in reader.pages:
            t = p.extract_text() or ""
            if t: out.append(t)
        return "\n".join(out).strip()
    except Exception:
        return ""

def extract_text_from_docx(path: str) -> str:
    try:
        from docx import Document
        doc = Document(path)
        paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
        return "\n\n".join(paras)
    except Exception:
        return ""

def extract_text_from_pdf_ocr(pdf_path: str) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract
        pages = convert_from_path(pdf_path, dpi=300, poppler_path=POPPLER_PATH)
        texts = []
        for img in pages:
            txt = pytesseract.image_to_string(img, lang="eng")  # 中文可 "eng+chi_sim"
            if txt: texts.append(txt)
        return "\n".join(texts).strip()
    except Exception:
        return ""

def extract_text_smart(path: str, ext: str) -> str:
    ext = ext.lower()
    if ext == ".pdf":
        txt = extract_text_from_pdf_textlayer(path)
        if len((txt or "").strip()) < 40:
            ocr_txt = extract_text_from_pdf_ocr(path)
            if len(ocr_txt.strip()) > len(txt.strip()):
                return ocr_txt
        return txt
    if ext == ".docx": return extract_text_from_docx(path)
    if ext in [".txt", ".md"]:
        with open(path, "r", encoding="utf-8", errors="ignore") as rf:
            return rf.read()
    return ""

# ---- 聚合所有教案（默认静态 + /upload/*.json）----
def list_all_lessons():
    """
    返回 [{'id','title','source'}...]
    source: 'static' | 'upload'
    """
    items = []
    # 默认生物
    try:
        with open(DEFAULT_LESSON_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        items.append({"id": DEFAULT_LESSON_ID, "title": d.get("title", "Biology Sample"), "source": "static"})
    except Exception:
        pass
    # /upload 中的所有 *.json
    for fp in glob.glob(os.path.join(LESSONS_DIR, "*.json")):
        lid = os.path.splitext(os.path.basename(fp))[0]
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            items.append({"id": lid, "title": d.get("title", lid), "source": "upload"})
        except Exception:
            items.append({"id": lid, "title": lid, "source": "upload"})
    return items

def merge_with_index(items):
    """
    把索引元数据合并进列表（published/classes/order），并排序
    """
    idx = load_index()
    meta = { x["id"]: x for x in idx.get("lessons", []) }
    out = []
    for it in items:
        m = meta.get(it["id"], {})
        out.append({
            **it,
            "published": m.get("published", True if it["id"]==DEFAULT_LESSON_ID else False),
            "classes": m.get("classes", []),
            "order": m.get("order", 9999 if it["source"]=="upload" else 1),
            "updated_at": m.get("updated_at")
        })
    out.sort(key=lambda x: (x.get("order", 9999), x.get("title","")))
    return out

# -------------------- Pages --------------------
@app.route("/")
def index(): return render_template("index.html")

@app.route("/favicon.ico")
def favicon():
    path = os.path.join(STATIC_DIR, "favicon.ico")
    if os.path.exists(path):
        return send_from_directory(STATIC_DIR, "favicon.ico")
    return ("", 204)

@app.route("/teacher")
def teacher_end():
    override = request.args.get("lesson_id")
    if override: SESSION["lesson_id"] = override
    lesson = load_lesson(SESSION["lesson_id"])
    html, targets = render_targets_from_html(lesson["content_html"])
    # 侧边栏数据（直接在模板里拉 API 也行，这里直接传可减少首屏请求）
    lesson_cards = merge_with_index(list_all_lessons())
    return render_template("teacher_end.html",
        lesson_id=SESSION["lesson_id"],
        content_html=html,
        targets_json=json.dumps(targets, ensure_ascii=False),
        tolerance=SESSION["tolerance_sec"],
        sidebar_lessons=lesson_cards)

@app.route("/student")
def student_end():
    override = request.args.get("lesson_id")
    if override: SESSION["lesson_id"] = override
    lesson = load_lesson(SESSION["lesson_id"])
    html, targets = render_targets_from_html(lesson["content_html"])
    return render_template("student_end.html",
        lesson_id=SESSION["lesson_id"],
        content_html=html,
        targets_json=json.dumps(targets, ensure_ascii=False),
        tolerance=SESSION["tolerance_sec"])

@app.get("/teacher/upload")
def teacher_upload_page():
    return render_template("teacher_upload.html", current_lesson=SESSION["lesson_id"])

@app.get("/teacher/edit")
def teacher_edit_page():
    lesson = load_lesson(SESSION["lesson_id"])
    return render_template("teacher_edit.html",
        lesson_id=SESSION["lesson_id"],
        title=lesson.get("title",""),
        content_html=lesson["content_html"])

# -------------------- APIs --------------------
# 上传 → 生成/覆盖 /upload/{lesson_id}.json（成功后注册到索引）
@app.post("/api/upload_lesson")
def api_upload_lesson():
    try:
        f = request.files.get("file")
        if not f or not (f.filename or "").strip():
            return jsonify({"ok": False, "error": "No file uploaded"}), 400
        filename = f.filename
        ext = os.path.splitext(filename)[1].lower()
        if ext not in [".pdf", ".docx", ".txt", ".md"]:
            return jsonify({"ok": False, "error": "Unsupported file type. Use .pdf/.docx/.txt/.md"}), 400

        tmp_path = os.path.join(UPLOAD_TMP_DIR, f"{uuid.uuid4().hex}{ext}")
        f.save(tmp_path)

        text = extract_text_smart(tmp_path, ext)
        if not text or len(text.strip()) < 5:
            try: os.remove(tmp_path)
            except: pass
            return jsonify({"ok": False, "error": "Failed to extract text. Ensure Tesseract & Poppler for scanned PDFs."}), 400

        content_html = _plain_text_to_html_blocks(text)
        lesson_id = request.form.get("lesson_id") or f"u_{uuid.uuid4().hex[:8]}"
        title = request.form.get("title") or f"Imported Lesson ({filename})"
        save_lesson(lesson_id, title, content_html)  # 👉 保存到 /upload

        # 自动注册到索引
        idx = load_index()
        lessons = idx.get("lessons", [])
        entry = next((x for x in lessons if x.get("id")==lesson_id), None)
        if not entry:
            entry = {"id": lesson_id, "title": title, "published": False, "classes": [], "order": 9999,
                     "updated_at": datetime.datetime.utcnow().isoformat()+"Z"}
            lessons.append(entry)
            idx["lessons"] = lessons
            save_index(idx)

        try: os.remove(tmp_path)
        except: pass
        return jsonify({"ok": True, "lesson_id": lesson_id, "title": title})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}", "trace": traceback.format_exc()}), 500

# 逐词编辑器保存（把选中 token 用 <H>..</H> 包起来落盘）
@app.post("/api/update_lesson")
def api_update_lesson():
    data = request.get_json(force=True)
    lesson_id = data.get("lesson_id") or SESSION["lesson_id"]
    title = data.get("title") or "Lesson"
    tokens = data.get("tokens", [])
    structure = data.get("structure", [])
    def render_span(s, e):
        parts = []
        for i in range(s, e):
            t = tokens[i]
            text = t.get("text",""); sep = t.get("sep","")
            if t.get("selected"):
                parts.append(f"<H>{_escape_html(text)}</H>{_escape_html(sep)}")
            else:
                parts.append(f"{_escape_html(text)}{_escape_html(sep)}")
        return "".join(parts)
    blocks = []
    for blk in structure:
        if blk.get("type") == "h3":
            blocks.append(f"<h3>{_escape_html(blk.get('text',''))}</h3>")
        else:
            s, e = blk.get("token_range", [0,0])
            blocks.append(f"<p>{render_span(s,e)}</p>")
    new_html = "\n".join(blocks).strip()
    save_lesson(lesson_id, title, new_html)  # 👉 保存到 /upload
    return jsonify({"ok": True, "lesson_id": lesson_id})

# 会话与评分
@app.post("/api/reset_session")
def reset_session():
    SESSION["teacher_starts"].clear()
    SESSION["student_starts"].clear()
    return jsonify({"ok": True})

@app.get("/api/plan/<lesson_id>")
def api_plan(lesson_id):
    lesson = load_lesson(lesson_id)
    html, targets = render_targets_from_html(lesson["content_html"])
    return jsonify({"lesson_id": lesson_id, "targets": targets})

@app.post("/api/teacher/start")
def api_teacher_start():
    data = request.get_json(force=True)
    tid = data.get("target_id"); now = time.time()
    if tid and tid not in SESSION["teacher_starts"]:
        SESSION["teacher_starts"][tid] = now
    return jsonify({"ok": True, "ts": SESSION["teacher_starts"].get(tid)})

@app.post("/api/student/start")
def api_student_start():
    data = request.get_json(force=True)
    tid = data.get("target_id"); now = time.time()
    if tid and tid not in SESSION["student_starts"]:
        SESSION["student_starts"][tid] = now
    return jsonify({"ok": True, "ts": SESSION["student_starts"].get(tid)})

@app.get("/api/score")
def api_score():
    lesson = load_lesson(SESSION["lesson_id"])
    _, targets = render_targets_from_html(lesson["content_html"])
    total = len(targets); tol = SESSION["tolerance_sec"]
    focused = 0; diffs = {}
    for t in targets:
        tid = t["id"]
        t_ts = SESSION["teacher_starts"].get(tid)
        s_ts = SESSION["student_starts"].get(tid)
        if t_ts is None or s_ts is None: continue
        diff = abs(s_ts - t_ts); diffs[tid] = diff
        if diff <= tol: focused += 1
    percent = (focused/total*100) if total else 0.0
    return jsonify({
        "total_boxes": total,
        "within_tolerance": focused,
        "tolerance_sec": tol,
        "focus_percent": round(percent, 1),
        "diffs_sec": {k: round(v,3) for k,v in diffs.items()}
    })

# 课件列表 API（合并默认+上传+索引元数据）
@app.get("/api/lessons")
def api_lessons():
    role = request.args.get("role", "student")
    klass = request.args.get("class")
    include_unpub = request.args.get("include_unpublished") == "true"

    items = merge_with_index(list_all_lessons())

    def visible(l):
        if role == "teacher":
            return True if include_unpub else l.get("published", False)
        if not l.get("published", False):
            return False
        classes = l.get("classes") or []
        if not classes: return True
        return klass in classes

    out = [l for l in items if visible(l)]
    return jsonify({"ok": True, "lessons": out})

# 发布设置（维持不变）
@app.post("/api/lessons/<lesson_id>/publish")
def api_publish_lesson(lesson_id):
    data = request.get_json(force=True)
    idx = load_index(); lessons = idx.get("lessons", [])
    entry = next((x for x in lessons if x.get("id")==lesson_id), None)
    if not entry:
        # 若索引没记录，则补一条（title 从文件读取）
        try:
            lesson = load_lesson(lesson_id)
            title = lesson.get("title", lesson_id)
        except Exception:
            title = lesson_id
        entry = {"id": lesson_id, "title": title}
        lessons.append(entry)
    if "published" in data: entry["published"] = bool(data["published"])
    if "classes" in data: entry["classes"] = list(data["classes"])
    if "order" in data: entry["order"] = int(data["order"])
    entry["updated_at"] = datetime.datetime.utcnow().isoformat()+"Z"
    idx["lessons"] = lessons; save_index(idx)
    return jsonify({"ok": True, "lesson": entry})

# 注册（保留）
@app.post("/api/lessons/register")
def api_register_lesson():
    data = request.get_json(force=True)
    lid = data.get("id"); title = data.get("title") or lid
    if not lid: return jsonify({"ok": False, "error": "missing id"}), 400
    idx = load_index(); lessons = idx.get("lessons", [])
    entry = next((x for x in lessons if x.get("id")==lid), None)
    if not entry:
        entry = {"id": lid, "title": title, "published": False, "classes": [], "order": 9999}
        lessons.append(entry)
    else:
        entry["title"] = title or entry.get("title", lid)
        entry["updated_at"] = datetime.datetime.utcnow().isoformat()+"Z"
    idx["lessons"] = lessons; save_index(idx)
    return jsonify({"ok": True, "lesson": entry})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
