"""备份 data/ 与 uploads/ 到 backups/<时间戳>/（上线前或定期快照）

用法:
    python backup_data.py
"""
import os
import shutil
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
dst = os.path.join(BASE, "backups", ts)
src_data = os.path.join(BASE, "data")
src_up = os.path.join(BASE, "uploads")

if os.path.isdir(src_data):
    shutil.copytree(src_data, os.path.join(dst, "data"))
if os.path.isdir(src_up):
    shutil.copytree(src_up, os.path.join(dst, "uploads"))

print("已备份到:", dst)
