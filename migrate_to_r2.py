"""
一次性迁移脚本：把本机 data/knowledge.db 与 uploads/ 下全部文件推送到 Cloudflare R2。
运行前请先设置 R2 环境变量（或在下方直接填写）：
  R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET

用法：
  python migrate_to_r2.py
推送完成后，云端实例（已设同样 R2 变量）启动时会自动拉取数据库、按 key 读取文件。
"""
import os
import sys
import config
import r2store


def main():
    if not r2store.USE_R2:
        print("未检测到 R2 配置（R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET 需全部设置）。")
        print("请先设置环境变量后重试。")
        sys.exit(1)

    # 1) 数据库
    if os.path.isfile(config.DB_PATH):
        print("[1/2] 上传数据库 knowledge.db ...")
        r2store.put_file(r2store.DB_KEY, config.DB_PATH)
    else:
        print("[1/2] 本地无 knowledge.db，跳过。")

    # 2) 上传文件
    print("[2/2] 上传 uploads/ 全部文件 ...")
    count = 0
    for root, _, files in os.walk(config.UPLOAD_DIR):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, config.UPLOAD_DIR).replace("\\", "/")
            r2store.put_file(rel, p)
            count += 1
    print("完成：共上传 %d 个文件到 R2 桶 %s" % (count, r2store.R2_BUCKET))


if __name__ == "__main__":
    main()
