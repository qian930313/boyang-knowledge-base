"""
博阳影响培训平台 —— Flask 主程序
企业微信 H5 学习平台：分类浏览 / 视频与课件学习 / 进度记录 / 测验 / 证书 / 管理后台
"""
import os
import re
import io
import json
import mimetypes
import shutil
import subprocess
from flask import (
    Flask, request, session, g, render_template, redirect,
    url_for, send_from_directory, send_file, flash, abort, Response,
)
from werkzeug.utils import secure_filename
import config
import db
import wecom
import r2store

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

os.makedirs(config.UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
# 启用 R2 时，先拉取云端最新数据库（本地是临时文件系统），再初始化、回写
if r2store.USE_R2:
    r2store.pull_db_from_r2()
db.init_db()
if r2store.USE_R2:
    r2store.sync_db_to_r2()


# ---------------- 全局 ----------------
@app.before_request
def load_context():
    g.user = db.get_user(session.get("uid")) if session.get("uid") else None
    g.admin = session.get("admin") is True
    g.wecom_enabled = config.WECOM_ENABLED


@app.context_processor
def inject():
    return dict(user=g.user, admin=g.admin, wecom_enabled=config.WECOM_ENABLED,
                public_base=config.PUBLIC_BASE_URL, max_upload_mb=config.MAX_UPLOAD_MB)


@app.errorhandler(413)
def too_large(e):
    """上传文件超过 MAX_CONTENT_LENGTH：返回可读提示，避免后台面板只显示笼统的「保存失败」。"""
    msg = "上传失败：文件超过 %d MB 上限，请压缩后再上传。" % config.MAX_UPLOAD_MB
    flash(msg)
    m = re.search(r"/admin/course/(\d+)/edit", request.referrer or "")
    if m and db.get_course(int(m.group(1))):
        return _render_course_edit(int(m.group(1))), 413
    return Response(msg, status=413, mimetype="text/plain; charset=utf-8")


@app.template_filter("cert_date")
def cert_date(ts):
    import datetime
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y年%m月%d日")


def ensure_user():
    if g.user:
        return g.user
    if config.WECOM_ENABLED:
        redir = config.PUBLIC_BASE_URL or request.host_url.rstrip("/")
        return redirect(wecom.authorize_url(redir + url_for("wecom_callback")))
    return redirect(url_for("login"))


def ensure_admin():
    if not g.admin:
        return redirect(url_for("admin_login"))


# ---------------- 身份 ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if config.WECOM_ENABLED:
        redir = config.PUBLIC_BASE_URL or request.host_url.rstrip("/")
        return redirect(wecom.authorize_url(redir + url_for("wecom_callback")))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        dept = (request.form.get("dept") or "").strip()
        if not name:
            flash("请输入姓名")
            return render_template("dev_login.html")
        u = db.create_dev_user(name, dept)
        session["uid"] = u["id"]
        return redirect(url_for("home"))
    # 快速切换已有开发用户（仅开发模式）
    quick = request.args.get("uid")
    if quick and not config.WECOM_ENABLED:
        u = db.get_user(int(quick))
        if u:
            session["uid"] = u["id"]
            return redirect(url_for("home"))
    # 列出已有开发用户，便于演示多角色
    users = db.fetch_all("SELECT * FROM users ORDER BY id")
    return render_template("dev_login.html", users=users)


@app.route("/wecom/authorize")
def wecom_authorize():
    if not config.WECOM_ENABLED:
        return redirect(url_for("login"))
    redir = config.PUBLIC_BASE_URL or request.host_url.rstrip("/")
    return redirect(wecom.authorize_url(redir + url_for("wecom_callback")))


@app.route("/wecom/callback")
def wecom_callback():
    code = request.args.get("code")
    if not code:
        flash("企业微信授权失败：缺少 code")
        return redirect(url_for("login"))
    try:
        u = wecom.resolve_user(code)
    except Exception as e:
        flash("企业微信身份解析失败：%s" % e)
        return redirect(url_for("login"))
    session["uid"] = u["id"]
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- 学员端 ----------------
@app.route("/")
def home():
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    cards = []
    for c in db.list_categories():
        # 前台把「每一条上传的视频」当成一门课程卡片展示（视频自带课程名称/主讲人/录制时间），
        # 所以「已完成 X/Y 门」要按实际上传的视频数统计；若按 courses 表的行数算，
        # 永远只会显示种子数据那 1 门示例课程（即 0/1），与后台上传的内容对不上。
        videos = [m for m in db.list_materials_by_category(c["id"]) if m["type"] == "video"]
        done = 0
        for v in videos:
            p = db.get_progress(u["id"], v["id"])
            if p and p["status"] == "done":
                done += 1
        cards.append(dict(cat=c, total=len(videos), done=done))
    total_certs = len(db.list_my_certificates(u["id"]))
    return render_template("home.html", cards=cards, total_certs=total_certs)


@app.route("/category/<key>")
def category(key):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    cat = db.fetch_one("SELECT * FROM categories WHERE key=?", (key,))
    if not cat:
        abort(404)
    courses = [dict(c) for c in db.list_courses_by_category(cat["id"])]
    all_mats = [dict(m) for m in db.list_materials_by_category(cat["id"])]
    atts = _atts_by_video(all_mats)
    by_course = {}
    for m in all_mats:
        # 已作为某视频关联附件的课件，不在列表里单独显示（只在视频项下方以附件形式展示）
        if m["type"] != "video" and m["attach_id"] in atts:
            continue
        if m["type"] == "video":
            m["atts"] = atts.get(m["id"], [])
        p = db.get_progress(u["id"], m["id"])
        m["status"] = p["status"] if p else "not_started"
        by_course.setdefault(m["course_id"], []).append(m)
    groups = [{"course": c, "mats": by_course.get(c["id"], [])} for c in courses]
    return render_template("category.html", cat=cat, groups=groups)


@app.route("/course/<int:cid>")
def course(cid):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    co = db.get_course(cid)
    if not co:
        abort(404)
    cat = db.fetch_one("SELECT * FROM categories WHERE id=?", (co["category_id"],))
    mats = [dict(m) for m in db.list_materials(cid)]
    atts = _atts_by_video(mats)
    for m in mats:
        p = db.get_progress(u["id"], m["id"])
        m["status"] = p["status"] if p else "not_started"
    for m in mats:
        m["atts"] = atts.get(m["id"], []) if m["type"] == "video" else []
    # 作为某视频附件的课件不单独成条，只挂在视频下方（与后台一致）
    mats_visible = [m for m in mats if not (m["type"] != "video" and m["attach_id"] in atts)]
    return render_template("course.html", co=co, cat=cat, mats=mats_visible)


@app.route("/material/<int:mid>")
def material(mid):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    m = db.get_material(mid)
    if not m:
        abort(404)
    co = db.get_course(m["course_id"])
    # 进入即记为进行中
    db.set_progress(u["id"], mid, "in_progress", 50)
    # 学习页只展示本节附件（「本节目录」已取消）
    attach = []
    if m["type"] == "video":
        attach = [dict(x) for x in db.list_materials(m["course_id"])
                  if x["type"] != "video" and x["attach_id"] == m["id"]]
    return render_template("learn.html", m=m, co=co, attach=attach)


@app.route("/api/progress", methods=["POST"])
def api_progress():
    u = ensure_user()
    if hasattr(u, "status_code"):
        return {"ok": False}, 403
    mid = int(request.form.get("mid"))
    status = request.form.get("status", "done")
    pct = int(request.form.get("pct", 100))
    db.set_progress(u["id"], mid, status, pct)
    return {"ok": True}


@app.route("/quiz/<int:cid>", methods=["GET", "POST"])
def quiz(cid):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    co = db.get_course(cid)
    qz = db.get_quiz(cid)
    if not qz:
        flash("该课程暂未配置测验")
        return redirect(url_for("course", cid=cid))
    if request.method == "POST":
        questions = db.list_questions(qz["id"])
        total = sum(q["score"] for q in questions) or len(questions) * 10
        got = 0
        answers = {}
        for q in questions:
            sel = request.form.getlist("q_%s" % q["id"])
            sel = [int(x) for x in sel]
            answers[str(q["id"])] = sel
            if sorted(sel) == sorted(q["answer"]):
                got += q["score"]
        passed = got >= qz["pass_score"]
        att_id = db.save_attempt(u["id"], cid, qz["id"], got, passed, answers)
        # 全部素材完成且测验通过 -> 发证书
        done, total_m, qpassed, completed = db.course_progress(u["id"], cid)
        if completed:
            db.issue_certificate(u["id"], cid)
        return redirect(url_for("result", attempt_id=att_id))
    questions = db.list_questions(qz["id"])
    return render_template("quiz.html", co=co, qz=qz, questions=questions)


@app.route("/result/<int:attempt_id>")
def result(attempt_id):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    att = db.fetch_one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    if not att or att["user_id"] != u["id"]:
        abort(404)
    co = db.get_course(att["course_id"])
    qz = db.get_quiz(att["course_id"])
    questions = db.list_questions(qz["id"]) if qz else []
    answers = json.loads(att["answers"] or "{}")
    cert = db.get_certificate(u["id"], att["course_id"]) if att["passed"] else None
    return render_template("result.html", att=att, co=co, qz=qz,
                           questions=questions, answers=answers, cert=cert)


@app.route("/certificate/<int:cid>")
def certificate(cid):
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    cert = db.get_certificate(u["id"], cid)
    if not cert:
        flash("尚未获得该课程证书，请先完成学习并通过测验")
        return redirect(url_for("course", cid=cid))
    co = db.get_course(cid)
    return render_template("certificate.html", cert=cert, co=co, user=u)


@app.route("/my")
def my():
    u = ensure_user()
    if hasattr(u, "status_code"):
        return u
    certs = db.list_my_certificates(u["id"])
    # 进行中的课程
    learning = []
    for c in db.list_categories():
        for co in db.list_courses_by_category(c["id"]):
            done, total, passed, completed = db.course_progress(u["id"], co["id"])
            if not completed and total > 0 and done > 0:
                learning.append((co, done, total))
    return render_template("my.html", certs=certs, learning=learning)


# ---------------- 管理后台 ----------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == config.ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_index"))
        flash("密码错误")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session["admin"] = False
    return redirect(url_for("admin_login"))


def _admin_stats():
    """后台各模块的汇总数据。"""
    return dict(
        total_cats=db.fetch_one("SELECT COUNT(*) AS n FROM categories")["n"],
        total_courses=db.fetch_one("SELECT COUNT(*) AS n FROM courses")["n"],
        total_users=db.fetch_one("SELECT COUNT(*) AS n FROM users")["n"],
        total_certs=db.fetch_one("SELECT COUNT(*) AS n FROM certificates")["n"],
    )


@app.route("/admin/")
def admin_index():
    r = ensure_admin()
    if r is not None:
        return r
    return render_template("admin/index.html", **_admin_stats())


@app.route("/admin/categories")
def admin_categories():
    r = ensure_admin()
    if r is not None:
        return r
    return render_template("admin/categories.html", cats=db.list_categories(), **_admin_stats())


@app.route("/admin/courses")
def admin_courses():
    r = ensure_admin()
    if r is not None:
        return r
    cats = []
    for c in db.list_categories():
        c = dict(c)
        c["courses"] = db.list_courses_by_category(c["id"])
        cats.append(c)
    return render_template("admin/courses.html", cats=cats, **_admin_stats())


# 分类图标按名称关键词自动适配
ICON_KEYWORDS = [
    ("营销", "📣"), ("市场", "📣"), ("销售", "📣"), ("客户", "📣"),
    ("财务", "💰"), ("会计", "💰"), ("税务", "💰"), ("资金", "💰"), ("预算", "💰"),
    ("人事", "👥"), ("人力", "👥"), ("招聘", "👥"), ("薪酬", "👥"),
    ("培训", "📚"), ("学习", "📚"), ("教育", "📚"),
    ("安全", "🛡️"), ("合规", "🛡️"), ("法务", "⚖️"), ("法律", "⚖️"),
    ("技术", "⚙️"), ("研发", "💡"), ("工程", "🔧"), ("生产", "🏭"),
    ("运营", "📈"), ("行政", "📋"), ("质量", "✅"),
]
DEFAULT_ICONS = ["📁", "📘", "📗", "📙", "📕", "📒", "🔖", "🏷️"]


def auto_category_icon(name):
    for kw, ic in ICON_KEYWORDS:
        if kw in name:
            return ic
    return DEFAULT_ICONS[hash(name) % len(DEFAULT_ICONS)]


def auto_category_key(name):
    exist = set(r["key"] for r in db.fetch_all("SELECT key FROM categories"))
    base, n = "c", 1
    while f"{base}{n}" in exist:
        n += 1
    return f"{base}{n}"


@app.route("/admin/category", methods=["POST"])
def admin_category_new():
    r = ensure_admin()
    if r is not None:
        return r
    name = (request.form.get("name") or "").strip()
    if name:
        icon = auto_category_icon(name)
        key = auto_category_key(name)
        db.execute("INSERT INTO categories(key,name,icon,sort) VALUES(?,?,?,?)",
                   (key, name, icon, int(request.form.get("sort", 0) or 0)))
        flash("分类「%s」已添加" % name)
    else:
        flash("类型名称不能为空")
    return redirect(url_for("admin_categories"))


@app.route("/admin/category/<int:cid>/edit", methods=["GET", "POST"])
def admin_category_edit(cid):
    r = ensure_admin()
    if r is not None:
        return r
    c = db.fetch_one("SELECT * FROM categories WHERE id=?", (cid,))
    if not c:
        abort(404)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        intro = (request.form.get("intro") or "").strip()
        if name:
            icon = auto_category_icon(name)
            db.execute(
                "UPDATE categories SET name=?,icon=?,sort=?,intro=? WHERE id=?",
                (name, icon, int(request.form.get("sort", 0) or 0), intro or None, cid),
            )
            flash("分类已更新")
        else:
            flash("类型名称不能为空")
        return redirect(url_for("admin_categories"))
    return render_template("admin/category_edit.html", c=c, **_admin_stats())


@app.route("/admin/category/<int:cid>/delete", methods=["POST"])
def admin_category_delete(cid):
    r = ensure_admin()
    if r is not None:
        return r
    n_courses = db.fetch_one(
        "SELECT COUNT(*) AS n FROM courses WHERE category_id=?", (cid,)
    )["n"]
    if n_courses > 0:
        flash("该分类下还有 %d 门课程，请先移除后再删除" % n_courses)
    else:
        db.execute("DELETE FROM categories WHERE id=?", (cid,))
        flash("分类已删除")
    return redirect(url_for("admin_categories"))


@app.route("/admin/course/new", methods=["GET", "POST"])
def admin_course_new():
    r = ensure_admin()
    if r is not None:
        return r
    if request.method == "POST":
        cname = (request.form.get("category_name") or "").strip()
        first = db.fetch_one("SELECT * FROM categories ORDER BY sort, id")
        if not cname and not first:
            flash("请先填写分类名称")
            return _render_course_form(None, "")
        cat_id = _resolve_category(cname, first["id"] if first else None)
        cid = db.execute(
            "INSERT INTO courses(category_id,title,desc,lecturer,record_time,required,sort,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (cat_id,
             request.form.get("title") or "未命名课程",
             request.form.get("desc"),
             request.form.get("lecturer") or None, request.form.get("record_time") or None,
             1 if request.form.get("required") else 0,
             int(request.form.get("sort", 0) or 0), db.now()),
        )
        flash("课程已创建，请在下方补充课程名称、主讲人与录制时间")
        return redirect(url_for("admin_course_edit", cid=cid))
    return _render_course_form(None, "")


@app.route("/admin/course/<int:cid>/edit", methods=["GET", "POST"])
def admin_course_edit(cid):
    r = ensure_admin()
    if r is not None:
        return r
    co = db.get_course(cid)
    if not co:
        abort(404)
    cats = db.list_categories()
    if request.method == "POST":
        fields = {}
        uploaded = 0
        if request.form.get("_section") == "meta":
            # 课程内容区表单：上传前填一套「课程名称 / 主讲人 / 录制时间」，本批文件共用。
            # 历史遗留的 fkey + ftitle/flecturer/ftime（逐文件）仍兼容：若存在则覆盖本批默认值。
            batch_default = (
                (request.form.get("course_title") or "").strip() or None,
                (request.form.get("lecturer") or "").strip() or None,
                (request.form.get("record_time") or "").strip() or None,
            )
            keys = request.form.getlist("fkey")
            if keys:
                titles = request.form.getlist("ftitle")
                lects = request.form.getlist("flecturer")
                times = request.form.getlist("ftime")

                def pick(lst, i):
                    return (lst[i].strip() if i < len(lst) and lst[i] else "") or None

                meta_map = {k: (pick(titles, i), pick(lects, i), pick(times, i))
                            for i, k in enumerate(keys) if k}
            else:
                meta_map = {}
            # 注意：这里不再改写 courses.title/lecturer/record_time——
            # 旧逻辑每上传一批就覆盖课程级字段，正是「前几批信息消失、全部显示成一样」的原因。
            uploaded = _store_uploads(cid,
                                      request.files.getlist("video") + request.files.getlist("doc"),
                                      int(request.form.get("sort", 0) or 0),
                                      meta_map, batch_default)
        else:
            # 顶部信息表单：分类名称（直接输入，不存在则自动新建）/ 课程名称 / 简介 / 必修 / 排序
            fields["category_id"] = _resolve_category(request.form.get("category_name"),
                                                      co["category_id"])
            fields["title"] = (request.form.get("title") or "").strip() or co["title"]
            fields["desc"] = request.form.get("desc")
            fields["required"] = 1 if request.form.get("required") else 0
            fields["sort"] = int(request.form.get("sort", 0) or 0)
        if fields:
            sql = "UPDATE courses SET %s WHERE id=?" % ", ".join("%s=?" % k for k in fields)
            db.execute(sql, list(fields.values()) + [cid])
        if request.form.get("_section") == "meta":
            if uploaded > 0:
                flash("已上传 %d 个文件" % uploaded)
            elif uploaded == 0:
                flash("没有选择文件，未做任何改动")
            # uploaded < 0：被容量拦截，_store_uploads 已 flash 拒绝消息
        else:
            flash("课程信息已保存")
        return redirect(url_for("admin_course_edit", cid=cid))
    return _render_course_edit(cid)


def _store_uploads(cid, files, sort, meta_map=None, batch_default=None):
    """保存上传的视频/课件，返回成功入库的文件数；被容量拦截时返回 -1。

    同一批上传的文件共享一个 unit_id（学习单元），使「一次上传的视频+课件」自动成组。
    meta_map: {原始文件名: (课程名称, 主讲人, 录制时间)}，逐个文件写入 materials 的同名列，
    因此同一课程下每个视频都可以有自己的一套课程名称/主讲人/录制时间。
    batch_default: (课程名称, 主讲人, 录制时间)，本批统一的默认值——文件没有自己的信息时用它。
    课件未单独填写时，自动继承本批视频的信息（课件是视频的附件）。"""
    files = [f for f in files if f and f.filename]
    if not files:
        return 0
    # --- R2 容量拦截：上传前先计算本批总大小，超限则整体拒绝并提示 ---
    if r2store.USE_R2:
        batch_total = 0
        for f in files:
            try:  # 探测文件大小（werkzeug FileStorage 支持 seek/tell）
                f.seek(0, os.SEEK_END)
                batch_total += f.tell()
                f.seek(0)
            except Exception:
                pass
        ok, used, limit, msg = r2store.check_capacity(batch_total)
        if not ok:
            flash(msg)
            return -1  # 被容量拦截，调用方不应再提示「上传成功/未选文件」
        if msg:
            flash(msg)
    meta_map = meta_map or {}
    course_dir = os.path.join(config.UPLOAD_DIR, str(cid))
    os.makedirs(course_dir, exist_ok=True)
    # 本批次统一 unit_id：取课程内当前最大 unit_id + 1
    row = db.fetch_one("SELECT COALESCE(MAX(unit_id),0) AS u FROM materials WHERE course_id=?", (cid,))
    unit_id = (row["u"] if row else 0) + 1
    saved = 0
    inserted = []  # [(material_id, type)]
    for f in files:
        raw = f.filename
        # 扩展名必须取「原始文件名」——中文文件名经 secure_filename 会只剩 "mp4"（点被吃掉），
        # 导致扩展名判空而被误判为不支持的类型直接丢弃。
        ext = os.path.splitext(raw)[1].lower()
        if ext not in config.ALLOWED_EXT:
            flash("不支持的文件类型：%s（%s）" % (ext or "无扩展名", raw))
            continue
        # 存储文件名安全化（中文会被过滤），保留原扩展名；重名自动加序号
        stem = secure_filename(os.path.splitext(raw)[0]).strip("._-") or "file"
        fname = stem + ext
        dest = os.path.join(course_dir, fname)
        i = 1
        while os.path.exists(dest):
            fname = "%s_%d%s" % (stem, i, ext)
            dest = os.path.join(course_dir, fname)
            i += 1
        f.save(dest)
        rel = os.path.relpath(dest, config.UPLOAD_DIR).replace("\\", "/")
        if r2store.USE_R2:
            r2store.put_file(rel, dest)
        mtype = "video" if ext in config.ALLOWED_VIDEO_EXT else "doc"
        # 文档尝试转 PDF 以便浏览器内预览
        preview = None
        if mtype == "doc":
            pdf = convert_to_pdf(dest, course_dir)
            if pdf:
                preview = os.path.relpath(pdf, config.UPLOAD_DIR).replace("\\", "/")
                if r2store.USE_R2:
                    r2store.put_file(preview, pdf)
        # 标题用原始文件名（保留中文），便于前台展示
        title = os.path.splitext(raw)[0].strip() or fname
        # 该文件自己的一套信息：优先逐文件填写，其次回退到本批统一信息
        c_title, c_lect, c_time = meta_map.get(raw, (None, None, None))
        bt = batch_default or (None, None, None)
        c_title = c_title or bt[0]
        c_lect = c_lect or bt[1]
        c_time = c_time or bt[2]
        mid = db.execute(
            "INSERT INTO materials(course_id,type,title,file_path,preview_path,sort,unit_id,"
            "course_title,lecturer,record_time) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, mtype, title, rel, preview, sort, unit_id,
             c_title, c_lect, c_time),
        )
        inserted.append((mid, mtype))
        saved += 1
    # 同批次只有一个视频时，本批次的课件自动作为该视频的关联附件（前台/后台均按此展示），
    # 并且信息未单独填写的课件跟随该视频，避免附件卡显示成本课程的统一信息。
    videos = [mid for mid, t in inserted if t == "video"]
    if len(videos) == 1:
        v = db.fetch_one("SELECT course_title,lecturer,record_time FROM materials WHERE id=?",
                         (videos[0],))
        for mid, t in inserted:
            if t == "video":
                continue
            db.execute("UPDATE materials SET attach_id=? WHERE id=?", (videos[0], mid))
            db.execute(
                "UPDATE materials SET "
                "course_title=COALESCE(NULLIF(course_title,''),?),"
                "lecturer=COALESCE(NULLIF(lecturer,''),?),"
                "record_time=COALESCE(NULLIF(record_time,''),?) WHERE id=?",
                (v["course_title"], v["lecturer"], v["record_time"], mid),
            )
    return saved


def _atts_by_video(mats):
    """按 attach_id 把课件归到所属视频下，返回 {视频id: [课件,...]}（保持素材顺序）。"""
    videos = set(m["id"] for m in mats if m["type"] == "video")
    out = {}
    for m in mats:
        if m["type"] == "video" or m["attach_id"] not in videos:
            continue
        out.setdefault(m["attach_id"], []).append(m)
    return out


def _flat_mats(mats):
    """课程素材平铺列表（不按学习单元分组）：视频带 atts 关联附件，
    已挂到视频下的课件收进该视频的 atts 里展示，不再单独成卡。"""
    atts = _atts_by_video(mats)
    out = []
    for m in mats:
        if m["type"] != "video" and m["attach_id"] in atts:
            continue
        v = dict(m)
        v["atts"] = atts.get(m["id"], []) if m["type"] == "video" else []
        out.append(v)
    return out


def _resolve_category(name, fallback_id):
    """按分类名称取 id；名称不存在则自动新建分类。名称为空时返回 fallback_id。"""
    name = (name or "").strip()
    if not name:
        return fallback_id
    want = db.fetch_one("SELECT * FROM categories WHERE name=?", (name,))
    if want:
        return want["id"]
    new_id = db.execute("INSERT INTO categories(key,name,icon,sort) VALUES(?,?,?,?)",
                        (auto_category_key(name), name, auto_category_icon(name), 0))
    flash("已新建分类「%s」" % name)
    return new_id


def _render_course_form(co, cat_name, mats=None, qz=None):
    r2_used = r2store.get_usage()
    r2_limit = r2store.R2_STORAGE_LIMIT
    r2_pct = (r2_used / r2_limit * 100) if r2_limit else 0
    r2_warn = bool(r2_limit) and (r2_limit - r2_used < r2store._R2_WARN_BYTES)
    return render_template("admin/course_form.html", co=co, cat_name=cat_name,
                           mats=mats or [], qz=qz,
                           r2_used=r2_used, r2_limit=r2_limit,
                           r2_pct=r2_pct, r2_warn=r2_warn)


def _render_course_edit(cid):
    """渲染课程编辑页（素材平铺，不再按上传批次分「单元」）。
    「课程内容」表单始终不预填课程已有信息（该表单用于上传新一批内容）。"""
    co = db.get_course(cid)
    cat = db.fetch_one("SELECT * FROM categories WHERE id=?", (co["category_id"],)) if co else None
    mats = [dict(m) for m in db.list_materials(cid)]
    return _render_course_form(co, cat["name"] if cat else "",
                               mats=_flat_mats(mats), qz=db.get_quiz(cid))


@app.route("/admin/course/<int:cid>/upload", methods=["POST"])
def admin_upload(cid):
    r = ensure_admin()
    if r is not None:
        return r
    co = db.get_course(cid)
    if not co:
        abort(404)
    n = _store_uploads(cid, request.files.getlist("file"),
                      int(request.form.get("sort", 0) or 0))
    if n > 0:
        flash("上传成功")
    elif n == 0:
        flash("没有选择文件，未做任何改动")
    # n < 0：被容量拦截，_store_uploads 已 flash 拒绝消息
    return redirect(url_for("admin_course_edit", cid=cid))


@app.route("/admin/course/<int:cid>/videos-meta", methods=["POST"])
def admin_videos_meta(cid):
    """保存本课程视频的「课程名称 / 主讲人 / 录制时间 / 排序」（素材级）。
    表单里每行有三项信息 + 排序 + 一个「保存」按钮（name=only，值是该视频 id）：
      - 点某行的「保存」 → 只提交该行（浏览器的 FormData(form, submitter) 只带被点的按钮）；
      - 点「保存全部」   → 不带 only，提交几行就存几行。
    没出现在本次表单里的视频（key 缺失）一律不动。"""
    r = ensure_admin()
    if r is not None:
        return r
    if not db.get_course(cid):
        abort(404)
    only = (request.form.get("only") or "").strip()
    n = 0
    saved_title = None
    for m in db.list_materials(cid):
        if m["type"] != "video":
            continue
        if only and only != str(m["id"]):       # 逐个保存：本次只改被点的那一个
            continue
        key = "t_%d" % m["id"]
        if key not in request.form:             # 该视频不在本次提交的表单里
            continue
        title = (request.form.get(key) or "").strip() or None
        try:
            sort = int((request.form.get("s_%d" % m["id"]) or "").strip() or 0)
        except ValueError:
            sort = m["sort"] or 0
        db.execute("UPDATE materials SET course_title=?, lecturer=?, record_time=?, sort=? WHERE id=?",
                   (title,
                    (request.form.get("l_%d" % m["id"]) or "").strip() or None,
                    (request.form.get("r_%d" % m["id"]) or "").strip() or None,
                    sort, m["id"]))
        n += 1
        saved_title = title or saved_title
    if not n:
        flash("没有可更新的视频")
    elif only:
        flash("已保存「%s」" % saved_title if saved_title else "已保存该视频信息")
    else:
        flash("已更新 %d 个视频的信息" % n)
    return redirect(url_for("admin_course_edit", cid=cid))


@app.route("/admin/material/<int:mid>/delete", methods=["POST"])
def admin_material_delete(mid):
    r = ensure_admin()
    if r is not None:
        return r
    m = db.get_material(mid)
    if not m:
        abort(404)
    cid = m["course_id"]
    # 删除磁盘文件：尽力而为——文件可能被占用/权限受限（如正在播放的视频被系统锁住），
    # 此时不能阻断删除记录，否则会出现「删除失败」。
    file_warn = False
    for p in (m["file_path"], m["preview_path"]):
        if p:
            fp = os.path.join(config.UPLOAD_DIR, p)
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except OSError as e:
                file_warn = True
                print("[warn] 删除文件失败（已跳过）:", fp, e)
            if r2store.USE_R2:
                r2store.delete(p)
    db.execute("DELETE FROM progress WHERE material_id=?", (mid,))
    # 若有视频把它关联为附件，先解除关联，避免前台出现空附件
    db.execute("UPDATE materials SET attach_id=0 WHERE attach_id=?", (mid,))
    db.execute("DELETE FROM materials WHERE id=?", (mid,))
    flash("素材已删除" + ("（磁盘文件删除失败，已跳过）" if file_warn else ""))
    # 直接渲染编辑页（200），兼容后台面板内的 fetch 局部刷新
    return _render_course_edit(cid)


@app.route("/admin/course/<int:cid>/quiz", methods=["GET", "POST"])
def admin_quiz(cid):
    r = ensure_admin()
    if r is not None:
        return r
    co = db.get_course(cid)
    qz = db.get_quiz(cid)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "meta":
            title = request.form.get("title", "课程测验")
            pass_score = int(request.form.get("pass_score", 60) or 60)
            if not qz:
                db.execute("INSERT INTO quizzes(course_id,title,pass_score) VALUES(?,?,?)",
                           (cid, title, pass_score))
                qz = db.get_quiz(cid)
            else:
                db.execute("UPDATE quizzes SET title=?,pass_score=? WHERE id=?",
                           (title, pass_score, qz["id"]))
            flash("测验设置已保存")
            return redirect(url_for("admin_quiz", cid=cid))
        if action == "add":
            if not qz:
                db.execute("INSERT INTO quizzes(course_id,title,pass_score) VALUES(?,?,?)",
                           (cid, "课程测验", 60))
                qz = db.get_quiz(cid)
            qz_id = qz["id"]
            stem = (request.form.get("stem") or "").strip()
            if not stem:
                flash("题目内容不能为空")
                return redirect(url_for("admin_quiz", cid=cid))
            qtype = request.form.get("qtype", "single")
            score = int(request.form.get("qscore", 10) or 10)
            opts = [o.strip() for o in (request.form.get("options") or "").split("\n") if o.strip()]
            ans_raw = (request.form.get("answer") or "").strip()
            if qtype == "multiple":
                ans = [int(x) - 1 for x in ans_raw.replace("，", ",").split(",") if x.strip()]
            else:
                ans = [int(ans_raw) - 1] if ans_raw else []
            db.execute(
                "INSERT INTO questions(quiz_id,type,stem,options,answer,score,sort) VALUES(?,?,?,?,?,?,?)",
                (qz_id, qtype, stem, json.dumps(opts, ensure_ascii=False), json.dumps(ans), score, 0),
            )
            flash("题目已添加")
            return redirect(url_for("admin_quiz", cid=cid))
        if action == "del":
            qid = int(request.form.get("qid"))
            db.execute("DELETE FROM questions WHERE id=?", (qid,))
            flash("题目已删除")
            return redirect(url_for("admin_quiz", cid=cid))
    questions = db.list_questions(qz["id"]) if qz else []
    return render_template("admin/quiz_form.html", co=co, qz=qz, questions=questions)


@app.route("/admin/users")
def admin_users():
    r = ensure_admin()
    if r is not None:
        return r
    rows = db.fetch_all("SELECT * FROM users ORDER BY id")
    users = []
    for u in rows:
        u = dict(u)
        u["cert_count"] = len(db.list_my_certificates(u["id"]))
        users.append(u)
    return render_template("admin/users.html", users=users, **_admin_stats())


@app.route("/admin/users/<int:uid>")
def admin_user_detail(uid):
    r = ensure_admin()
    if r is not None:
        return r
    u = db.get_user(uid)
    if not u:
        abort(404)
    cats = {c["id"]: c for c in db.list_categories()}
    courses = db.fetch_all("SELECT * FROM courses ORDER BY category_id, sort, id")
    detail = []
    for co in courses:
        done, total, passed, completed = db.course_progress(uid, co["id"])
        att = None
        qz = db.get_quiz(co["id"])
        if qz:
            att = db.best_attempt(uid, qz["id"])
        cert = db.get_certificate(uid, co["id"])
        detail.append(dict(co=co, done=done, total=total, passed=passed,
                           completed=completed, att=att, cert=cert,
                           cat=cats.get(co["category_id"])))
    certs = db.list_my_certificates(uid)
    return render_template("admin/user_detail.html", u=u, detail=detail,
                           certs=certs, **_admin_stats())


# ---------------- 文件服务 ----------------
@app.route("/uploads/<path:filename>")
def uploads(filename):
    """发送上传文件，支持 HTTP Range（206 分片）——企业微信/iOS 的 <video> 必须依赖 Range 才能播放。"""
    # R2 模式：直接重定向到对象存储的 URL（直链或预签名），由 R2 处理 Range 分片播放
    if r2store.USE_R2:
        url = r2store.get_url(filename)
        if url:
            return redirect(url)
        abort(404)
    filepath = os.path.join(config.UPLOAD_DIR, filename)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file_range(filepath)


_MIME_OVERRIDE = {".m4v": "video/mp4", ".mov": "video/quicktime"}


def send_file_range(filepath, download=False):
    """支持 Range 的静态文件发送，兼容移动端视频播放。"""
    full = os.path.getsize(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    mimetype = _MIME_OVERRIDE.get(ext) or mimetypes.guess_type(filepath)[0]
    range_header = request.headers.get("Range")
    if not range_header:
        return send_file(filepath, mimetype=mimetype, conditional=True, as_attachment=download)
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        return send_file(filepath, mimetype=mimetype, conditional=True, as_attachment=download)
    start_s, end_s = m.group(1), m.group(2)
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else full - 1
    if start < 0 or end >= full or start > end:
        resp = Response(status=416)
        resp.headers["Content-Range"] = "bytes */%d" % full
        return resp
    length = end - start + 1
    with open(filepath, "rb") as f:
        f.seek(start)
        data = f.read(length)
    resp = Response(data, 206, mimetype=mimetype)
    resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, full)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    resp.headers["Cache-Control"] = "no-cache"
    if download:
        resp.headers["Content-Disposition"] = "attachment; filename=\"%s\"" % os.path.basename(filepath)
    return resp


# ---------------- 工具 ----------------
def convert_to_pdf(src_abs, out_dir):
    """若本机有 LibreOffice，将 Office 文档转成同目录 PDF 用于浏览器内预览；失败返回 None"""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, src_abs],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        base = os.path.splitext(os.path.basename(src_abs))[0]
        pdf = os.path.join(out_dir, base + ".pdf")
        return pdf if os.path.exists(pdf) else None
    except Exception:
        return None


if __name__ == "__main__":
    # 生产环境请用 waitress（见 Procfile / Dockerfile / render.yaml），不要直接用 Flask 内置服务器。
    # 此分支仅供本地直接 `python app.py` 调试，调试时设 KB_DEBUG=1 可开启自动重载。
    print("博阳知识库已启动: http://%s:%s" % (config.HOST, config.PORT))
    if not config.WECOM_ENABLED:
        print("提示：未配置企业微信，当前为『开发登录』模式。")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
