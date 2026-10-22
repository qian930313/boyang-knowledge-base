"""从项目根目录的 .env 读取本地环境变量（仅当系统环境未定义时才填充）。

目的：让本机脚本（如 upload_local.py）无需手动 set 环境变量即可直连 R2。
- 仅在变量缺失时填充，绝不会覆盖已存在的系统环境变量（Render 上的真实配置不受影响）。
- .env 已被 .gitignore 忽略，不会随 git 提交泄露。
"""
import os


def load():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    except Exception:
        return
    if not os.path.isfile(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


load()
