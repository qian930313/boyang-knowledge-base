"""
数据库层：建表 + 常用读写辅助函数（SQLite，零额外依赖）
"""
import os
import sqlite3
import time
import json
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wecom_userid  TEXT UNIQUE,
    name          TEXT NOT NULL,
    dept          TEXT,
    avatar        TEXT,
    created_at    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT UNIQUE,
    name  TEXT NOT NULL,
    icon  TEXT,
    sort  INTEGER DEFAULT 0,
    intro TEXT                -- 分类简介（一句话说明）
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER,
    title       TEXT NOT NULL,
    desc        TEXT,
    cover       TEXT,
    lecturer    TEXT,
    record_time TEXT,
    required    INTEGER DEFAULT 0,
    sort        INTEGER DEFAULT 0,
    created_at  REAL DEFAULT 0,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS materials (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER,
    type      TEXT,           -- video | doc | link
    title     TEXT NOT NULL,
    file_path TEXT,           -- 原始文件相对 uploads 的路径
    preview_path TEXT,        -- 转换后的 PDF 预览路径（可选）
    url       TEXT,           -- type=link 时外链
    duration  INTEGER DEFAULT 0,
    sort      INTEGER DEFAULT 0,
    attach_id INTEGER DEFAULT 0,  -- 所属视频 id（课件/外链 → 视频），0 表示独立素材
    unit_id   INTEGER DEFAULT 0,  -- 上传批次（学习单元）：同一批上传的视频+课件共享
    course_title TEXT,            -- 本素材上传时填写的课程名称（每个视频可不同，空则回退 courses.title）
    lecturer     TEXT,            -- 本素材上传时填写的主讲人（空则回退 courses.lecturer）
    record_time  TEXT,            -- 本素材上传时填写的录制时间（空则回退 courses.record_time）
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

CREATE TABLE IF NOT EXISTS progress (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    material_id INTEGER,
    status     TEXT DEFAULT 'not_started',  -- not_started | in_progress | done
    pct        INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0,
    UNIQUE(user_id, material_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (material_id) REFERENCES materials(id)
);

CREATE TABLE IF NOT EXISTS quizzes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER,
    title      TEXT,
    pass_score INTEGER DEFAULT 60,
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

CREATE TABLE IF NOT EXISTS questions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id  INTEGER,
    type     TEXT,            -- single | multiple | judge
    stem     TEXT,
    options  TEXT,            -- JSON 数组
    answer   TEXT,            -- JSON 数组（正确选项下标）
    score    INTEGER DEFAULT 0,
    sort     INTEGER DEFAULT 0,
    FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    course_id  INTEGER,
    quiz_id    INTEGER,
    score      INTEGER DEFAULT 0,
    passed     INTEGER DEFAULT 0,
    answers    TEXT,          -- JSON
    created_at REAL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

CREATE TABLE IF NOT EXISTS certificates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    no         TEXT UNIQUE,
    user_id    INTEGER,
    course_id  INTEGER,
    issued_at  REAL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (course_id) REFERENCES courses(id)
);
"""


def get_conn():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # 兼容旧库：补充新增字段（已在 SCHEMA 中的重复执行不会报错）
    for col in ("lecturer", "record_time"):
        try:
            conn.execute("ALTER TABLE courses ADD COLUMN %s TEXT" % col)
        except sqlite3.OperationalError:
            pass  # 列已存在
    try:
        conn.execute("ALTER TABLE categories ADD COLUMN intro TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    try:
        conn.execute("ALTER TABLE materials ADD COLUMN attach_id INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    try:
        conn.execute("ALTER TABLE materials ADD COLUMN unit_id INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 每个素材各自的上传信息（同一课程下多个视频可各不相同）
    for col in ("course_title", "lecturer", "record_time"):
        try:
            conn.execute("ALTER TABLE materials ADD COLUMN %s TEXT" % col)
        except sqlite3.OperationalError:
            pass  # 列已存在
    # 旧库兜底：把没有批次号的素材按 id 各自成一组
    conn.execute("UPDATE materials SET unit_id=id WHERE unit_id IS NULL OR unit_id=0")
    # 旧库兼容：attach_id 早期语义是「视频 → 课件」，现改为「课件 → 视频」（一个视频可挂多个课件）
    conn.execute(
        "UPDATE materials SET attach_id = ("
        "  SELECT v.id FROM materials v WHERE v.attach_id = materials.id AND v.type = 'video')"
        " WHERE type != 'video' AND (attach_id IS NULL OR attach_id = 0)"
        "   AND EXISTS (SELECT 1 FROM materials v"
        "               WHERE v.attach_id = materials.id AND v.type = 'video')"
    )
    conn.execute("UPDATE materials SET attach_id = 0 WHERE type = 'video'")
    # 同一批次（unit_id）内只有一个视频时，该批次的课件自动作为它的关联附件。
    # 后台「课程管理」与前台课程页/学习页都据此把课件展示在视频下方，不再单独成条。
    conn.execute(
        "UPDATE materials SET attach_id = ("
        "  SELECT v.id FROM materials v"
        "  WHERE v.unit_id = materials.unit_id AND v.course_id = materials.course_id"
        "    AND v.type = 'video'"
        "  LIMIT 1)"
        " WHERE type != 'video' AND (attach_id IS NULL OR attach_id = 0)"
        "   AND (SELECT COUNT(*) FROM materials v"
        "        WHERE v.unit_id = materials.unit_id"
        "          AND v.course_id = materials.course_id AND v.type = 'video') = 1"
    )
    conn.commit()
    conn.close()


def now():
    return time.time()


# ---------- 通用 ----------
def fetch_one(sql, args=()):
    conn = get_conn()
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def fetch_all(sql, args=()):
    conn = get_conn()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def execute(sql, args=()):
    conn = get_conn()
    cur = conn.execute(sql, args)
    conn.commit()
    last = cur.lastrowid
    conn.close()
    return last


def executescript(sql):
    conn = get_conn()
    conn.executescript(sql)
    conn.commit()
    conn.close()


# ---------- 用户 ----------
def get_or_create_user(wecom_userid, name, dept=None, avatar=None):
    u = fetch_one("SELECT * FROM users WHERE wecom_userid=?", (wecom_userid,))
    if u:
        return u
    uid = execute(
        "INSERT INTO users(wecom_userid,name,dept,avatar,created_at) VALUES(?,?,?,?,?)",
        (wecom_userid, name, dept, avatar, now()),
    )
    return fetch_one("SELECT * FROM users WHERE id=?", (uid,))


def create_dev_user(name, dept=None):
    """开发登录：用名字作为 userid 落库，方便本地预览"""
    wecom_userid = "dev_" + name.strip()
    return get_or_create_user(wecom_userid, name.strip(), dept)


def get_user(uid):
    return fetch_one("SELECT * FROM users WHERE id=?", (uid,))


# ---------- 分类 / 课程 / 素材 ----------
def list_categories():
    return fetch_all("SELECT * FROM categories ORDER BY sort, id")


def list_courses_by_category(cat_id):
    return fetch_all(
        "SELECT c.*, (SELECT COUNT(*) FROM materials m WHERE m.course_id=c.id) AS mat_count "
        "FROM courses c WHERE c.category_id=? ORDER BY c.sort, c.id", (cat_id,)
    )


def get_course(cid):
    return fetch_one("SELECT * FROM courses WHERE id=?", (cid,))


def list_materials(course_id):
    return fetch_all("SELECT * FROM materials WHERE course_id=? ORDER BY sort, id", (course_id,))


def list_materials_by_category(cat_id):
    """分类下全部素材（含所属课程信息），用于分类页直接列视频/课件。
    视频的关联课件由调用方按 attach_id 组装（一个视频可挂多个课件）。
    课程级信息由调用方另行查询（避免与素材级 course_title/lecturer/record_time 列同名冲突）。"""
    return fetch_all(
        "SELECT m.* FROM materials m JOIN courses c ON m.course_id = c.id "
        "WHERE c.category_id=? ORDER BY c.sort, c.id, m.sort, m.id", (cat_id,)
    )


def get_material(mid):
    return fetch_one("SELECT * FROM materials WHERE id=?", (mid,))


# ---------- 进度 ----------
def get_progress(user_id, material_id):
    return fetch_one(
        "SELECT * FROM progress WHERE user_id=? AND material_id=?", (user_id, material_id)
    )


def set_progress(user_id, material_id, status, pct=0):
    existing = get_progress(user_id, material_id)
    t = now()
    if existing:
        execute(
            "UPDATE progress SET status=?, pct=?, updated_at=? WHERE user_id=? AND material_id=?",
            (status, pct, t, user_id, material_id),
        )
    else:
        execute(
            "INSERT INTO progress(user_id,material_id,status,pct,updated_at) VALUES(?,?,?,?,?)",
            (user_id, material_id, status, pct, t),
        )


def course_progress(user_id, course_id):
    """返回 (已完成素材数, 总素材数, 是否已通过测验, 是否整课完成)"""
    mats = list_materials(course_id)
    total = len(mats)
    done = 0
    for m in mats:
        p = get_progress(user_id, m["id"])
        if p and p["status"] == "done":
            done += 1
    quiz = fetch_one("SELECT * FROM quizzes WHERE course_id=?", (course_id,))
    passed = False
    if quiz:
        att = fetch_one(
            "SELECT * FROM attempts WHERE user_id=? AND quiz_id=? AND passed=1 ORDER BY id DESC LIMIT 1",
            (user_id, quiz["id"]),
        )
        passed = bool(att)
    completed = (total > 0 and done == total) and (not quiz or passed)
    return done, total, passed, completed


# ---------- 测验 ----------
def get_quiz(course_id):
    return fetch_one("SELECT * FROM quizzes WHERE course_id=?", (course_id,))


def list_questions(quiz_id):
    rows = fetch_all("SELECT * FROM questions WHERE quiz_id=? ORDER BY sort, id", (quiz_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(r["options"] or "[]")
        d["answer"] = json.loads(r["answer"] or "[]")
        out.append(d)
    return out


def save_attempt(user_id, course_id, quiz_id, score, passed, answers):
    return execute(
        "INSERT INTO attempts(user_id,course_id,quiz_id,score,passed,answers,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, course_id, quiz_id, score, 1 if passed else 0, json.dumps(answers), now()),
    )


def best_attempt(user_id, quiz_id):
    return fetch_one(
        "SELECT * FROM attempts WHERE user_id=? AND quiz_id=? ORDER BY score DESC, id DESC LIMIT 1",
        (user_id, quiz_id),
    )


# ---------- 证书 ----------
def issue_certificate(user_id, course_id):
    existing = fetch_one(
        "SELECT * FROM certificates WHERE user_id=? AND course_id=?", (user_id, course_id)
    )
    if existing:
        return existing
    no = "BY-%s-%s" % (course_id, int(now()))
    cid = execute(
        "INSERT INTO certificates(no,user_id,course_id,issued_at) VALUES(?,?,?,?)",
        (no, user_id, course_id, now()),
    )
    return fetch_one("SELECT * FROM certificates WHERE id=?", (cid,))


def get_certificate(user_id, course_id):
    return fetch_one(
        "SELECT * FROM certificates WHERE user_id=? AND course_id=?", (user_id, course_id)
    )


def list_my_certificates(user_id):
    return fetch_all(
        "SELECT c.*, co.title AS course_title FROM certificates c "
        "JOIN courses co ON co.id=c.course_id WHERE c.user_id=? ORDER BY c.issued_at DESC",
        (user_id,),
    )
