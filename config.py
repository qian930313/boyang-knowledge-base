"""
博阳知识库 —— 运行配置
所有敏感配置均可通过环境变量覆盖（推荐在服务器/企业微信侧用环境变量注入）。
本地开发时直接使用默认值即可，企业微信相关留空则自动切换为「开发登录」模式。
"""
import os

# ---- 基础 ----
# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录（容器平台可挂载持久卷，例如设 KB_DATA_DIR=/data）
DATA_DIR = os.environ.get("KB_DATA_DIR", os.path.join(BASE_DIR, "data"))
# 上传文件根目录（容器平台可挂载持久卷，例如设 KB_UPLOAD_DIR=/data/uploads）
UPLOAD_DIR = os.environ.get("KB_UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
# 数据库文件
DB_PATH = os.path.join(DATA_DIR, "knowledge.db")
# 密钥（生产环境务必修改为随机长字符串并通过环境变量注入）
SECRET_KEY = os.environ.get("KB_SECRET_KEY", "boyang-kb-dev-secret-change-me")
# 服务监听
HOST = os.environ.get("KB_HOST", "0.0.0.0")
# 云平台（Render/Railway/Fly 等）会注入 $PORT；本地可用 KB_PORT 覆盖
PORT = int(os.environ.get("PORT") or os.environ.get("KB_PORT") or "5000")
# 调试模式：默认关闭；本地开发可设 KB_DEBUG=1 开启自动重载
DEBUG = os.environ.get("KB_DEBUG", "0") == "1"
# 对外可访问的基础地址（企业微信网页授权回调、JS 安全域名用到，例如 https://kb.example.com）
PUBLIC_BASE_URL = os.environ.get("KB_PUBLIC_BASE_URL", "").rstrip("/")

# ---- 管理员 ----
# 管理员后台登录密码（生产务必修改）
ADMIN_PASSWORD = os.environ.get("KB_ADMIN_PASSWORD", "bygp")

# ---- 企业微信（自建应用）----
# 不填则关闭企业微信登录，使用本地「开发登录」
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID", "")
WECOM_APP_SECRET = os.environ.get("WECOM_APP_SECRET", "")
# 网页授权可信域名（用于 JS-SDK，可选）
WECOM_TRUST_DOMAIN = os.environ.get("WECOM_TRUST_DOMAIN", "")

# 是否已启用企业微信身份接入
WECOM_ENABLED = bool(WECOM_CORP_ID and WECOM_APP_SECRET and WECOM_AGENT_ID)

# ---- Cloudflare R2 对象存储（免费持久化，可选）----
# 四个变量都填了才会启用（USE_R2）。不填则全部走本地文件，行为不变。
# 用 R2 后：数据库与上传文件都同步到桶，Render 免费实例重启也不丢数据。
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
# 可选：把桶设为公开读后填此值（形如 https://<域名>/<桶名>），文件用直链访问，支持 Range 播放
R2_PUBLIC_URL = (os.environ.get("R2_PUBLIC_URL", "") or "").rstrip("/")

# 允许的课件/视频文件扩展名
ALLOWED_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".ogg"}
ALLOWED_DOC_EXT = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".key", ".txt", ".md"}
ALLOWED_EXT = ALLOWED_VIDEO_EXT | ALLOWED_DOC_EXT
# 单次上传体积上限（MB）。培训视频常有 1G 以上，默认给足；可用环境变量覆盖。
MAX_UPLOAD_MB = int(os.environ.get("KB_MAX_UPLOAD_MB", "4096"))
