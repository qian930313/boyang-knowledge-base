#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地视频/课件直传 R2 —— 绕过 Render 免费实例磁盘限制。

用法：
  python upload_local.py <课程cid> <本地文件1> [本地文件2 ...]

说明：
  - 不依赖 Flask/网页，直接在本地把文件复制到 uploads/<cid>/，
    登记到本地数据库，并同步到 R2。
  - R2 凭证从根目录 .env 读取（见 localenv.py），无需手动设环境变量。
  - 同步完成后，若配置了 Render API（.env 中的 RENDER_API_KEY / RENDER_SERVICE_ID），
    会自动重启线上实例让它重新拉取最新数据库 —— 你只需等约 30 秒后刷新页面即可看到，
    全程无需找人手动重启。
  - 文档类会自动尝试转 PDF 以便网页内预览（依赖 libreoffice，缺失则跳过）。
"""
import os
import sys
import json
import shutil
import urllib.request
import localenv  # 确保 .env 中的 R2_*/RENDER_* 已注入 os.environ
import config
import db
import r2store
from werkzeug.utils import secure_filename
import app as appmod


def trigger_render_restart():
    """同步完成后自动重启线上 Render 实例，使其重新拉取最新数据库。

    配置：根目录 .env 需含 RENDER_API_KEY 与 RENDER_SERVICE_ID。
    未配置则跳过（仅提示用户手动重启/等待保活实例重启）。
    """
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    svc_id = os.environ.get("RENDER_SERVICE_ID", "").strip()
    if not api_key or not svc_id:
        print("[提示] 未配置 RENDER_API_KEY/RENDER_SERVICE_ID，无法自动重启实例。")
        print("        线上实例拉取新库约需等待其下次重启（或你在 Render 后台手动重启）后刷新查看。")
        return
    url = "https://api.render.com/v1/services/%s/restart" % svc_id
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", "Bearer %s" % api_key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.getcode()
        if 200 <= code < 300:
            print("[已触发线上实例重启] 约 30 秒后刷新页面即可看到新视频。")
        else:
            print("[警告] 重启请求返回 HTTP %s，请稍后在 Render 后台手动重启实例。" % code)
    except Exception as e:
        print("[警告] 自动重启失败：%s（可忽略，稍后在 Render 后台手动重启即可）" % e)


def main():
    if len(sys.argv) < 3:
        print("用法: python upload_local.py <课程cid> <本地文件1> [文件2 ...]")
        sys.exit(1)

    cid = int(sys.argv[1])
    paths = sys.argv[2:]

    # 必须先能连上 R2，否则素材只写进本地库、线上永远看不到（这是之前踩过的坑）。
    # 连不上就直接报错退出，绝不静默只写本地。
    if not r2store.USE_R2:
        print("[错误] 未检测到 R2 配置（环境变量或项目根目录 .env 缺失）。")
        print("        素材无法同步到云端，线上将看不到。请确认根目录 .env 含 R2_* 四项后重试。")
        sys.exit(1)

    # 上传前先从 R2 拉取最新数据库，避免用旧的本地库覆盖云端导致数据丢失
    if r2store.USE_R2:
        try:
            import boto3
            c = boto3.client("s3", endpoint_url=r2store._endpoint,
                             aws_access_key_id=r2store.R2_ACCESS_KEY_ID,
                             aws_secret_access_key=r2store.R2_SECRET_ACCESS_KEY,
                             region_name="auto")
            obj = c.get_object(Bucket=r2store.R2_BUCKET, Key=r2store.DB_KEY)
            data = obj["Body"].read()
            with open(config.DB_PATH, "wb") as f:
                f.write(data)
            print("[已拉取 R2 最新数据库到本地]")
        except Exception as e:
            print("[警告] 拉取 R2 数据库失败，将沿用本地库: %s" % e)

    co = db.get_course(cid)
    if not co:
        print("错误：课程 cid=%s 不存在" % cid)
        sys.exit(1)
    print("目标课程 cid=%d（数据库已确认存在）" % cid)

    course_dir = os.path.join(config.UPLOAD_DIR, str(cid))
    os.makedirs(course_dir, exist_ok=True)

    if r2store.USE_R2:
        total = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
        ok, used, limit, msg = r2store.check_capacity(total)
        if not ok:
            print("[容量拦截] %s" % msg)
            sys.exit(1)
        if msg:
            print("[容量提示] %s" % msg)

    row = db.fetch_one(
        "SELECT COALESCE(MAX(unit_id),0) AS u FROM materials WHERE course_id=?", (cid,)
    )
    unit_id = (row["u"] if row else 0) + 1

    for p in paths:
        if not os.path.isfile(p):
            print("跳过（不存在）: %s" % p)
            continue
        raw = os.path.basename(p)
        ext = os.path.splitext(raw)[1].lower()
        if ext not in config.ALLOWED_EXT:
            print("跳过（不支持类型 %s）: %s" % (ext or "无", raw))
            continue

        stem = secure_filename(os.path.splitext(raw)[0]).strip("._-") or "file"
        fname = stem + ext
        dest = os.path.join(course_dir, fname)
        i = 1
        while os.path.exists(dest):
            fname = "%s_%d%s" % (stem, i, ext)
            dest = os.path.join(course_dir, fname)
            i += 1

        shutil.copy2(p, dest)
        rel = os.path.relpath(dest, config.UPLOAD_DIR).replace("\\", "/")
        if r2store.USE_R2:
            r2store.put_file(rel, dest)

        mtype = "video" if ext in config.ALLOWED_VIDEO_EXT else "doc"
        preview = None
        if mtype == "doc":
            try:
                pdf = appmod.convert_to_pdf(dest, course_dir)
                if pdf:
                    preview = os.path.relpath(pdf, config.UPLOAD_DIR).replace("\\", "/")
                    if r2store.USE_R2:
                        r2store.put_file(preview, pdf)
            except Exception as e:
                print("  (转PDF失败，已忽略: %s)" % e)

        title = os.path.splitext(raw)[0].strip() or fname
        db.execute(
            "INSERT INTO materials(course_id,type,title,file_path,preview_path,sort,unit_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (cid, mtype, title, rel, preview, 0, unit_id),
        )
        print("OK  %-40s -> %s" % (raw, rel))

    if r2store.USE_R2:
        r2store.sync_db_to_r2()
        print("[已同步数据库到 R2]")
        trigger_render_restart()

    print("DONE")


if __name__ == "__main__":
    main()
