"""
Cloudflare R2 对象存储封装（S3 兼容）。

设计原则：
- 仅在配置了完整 R2_* 环境变量时启用（USE_R2=True）；未配置则全部走本地文件，行为与原版完全一致。
- 数据库 knowledge.db 与上传文件（视频/课件/PDF 预览）都同步到 R2，使 Render 免费实例（临时文件系统）
  重启/重部署后数据不丢失——实现「零成本持久化」。
- 公开桶可设 R2_PUBLIC_URL 用直链（更简单、支持 Range 播放）；私有桶自动用预签名 URL。
"""
import os
import config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_PUBLIC_URL = (os.environ.get("R2_PUBLIC_URL", "") or "").rstrip("/")

USE_R2 = bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)

# 数据库在桶内的固定 key
DB_KEY = "db/knowledge.db"

_endpoint = "https://%s.r2.cloudflarestorage.com" % R2_ACCOUNT_ID if R2_ACCOUNT_ID else ""
_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client(
            "s3",
            endpoint_url=_endpoint,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
    return _client


def put_file(key, local_path):
    """上传本地文件到 R2（key 含路径，如 uploads/3/abc.mp4）。"""
    if not USE_R2:
        return
    _get_client().upload_file(local_path, R2_BUCKET, key)


def get_url(key, expires=3600):
    """返回可访问的下载 URL：公开桶用直链，私有桶用预签名 URL（支持 Range）。"""
    if not USE_R2:
        return ""
    if R2_PUBLIC_URL:
        return "%s/%s" % (R2_PUBLIC_URL, key)
    return _get_client().generate_presigned_url(
        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=expires
    )


def download_file(key, local_path):
    """从 R2 下载到本地；不存在/失败返回 False。"""
    if not USE_R2:
        return False
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        _get_client().download_file(R2_BUCKET, key, local_path)
        return True
    except Exception as e:
        print("[r2] 下载失败 %s: %s" % (key, e))
        return False


def delete(key):
    """删除 R2 对象（忽略错误）。"""
    if not USE_R2:
        return
    try:
        _get_client().delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception:
        pass


def sync_db_to_r2():
    """把本地 knowledge.db 同步到 R2。"""
    if USE_R2 and os.path.isfile(config.DB_PATH):
        put_file(DB_KEY, config.DB_PATH)


def pull_db_from_r2(force=True):
    """启动前从 R2 拉取最新数据库到本地。返回是否成功拉取。

    force=True（默认）：只要 R2 上存在数据库，就以 R2 为准覆盖本地——
    避免容器重启/重新部署后，用镜像里的旧库覆盖云端最新数据。
    """
    if not USE_R2:
        return False
    if not force and os.path.isfile(config.DB_PATH):
        return False
    return download_file(DB_KEY, config.DB_PATH)
