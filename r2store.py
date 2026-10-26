"""
Cloudflare R2 对象存储封装（S3 兼容）。

设计原则：
- 仅在配置了完整 R2_* 环境变量时启用（USE_R2=True）；未配置则全部走本地文件，行为与原版完全一致。
- 数据库 knowledge.db 与上传文件（视频/课件/PDF 预览）都同步到 R2，使 Render 免费实例（临时文件系统）
  重启/重部署后数据不丢失——实现「零成本持久化」。
- 公开桶可设 R2_PUBLIC_URL 用直链（更简单、支持 Range 播放）；私有桶自动用预签名 URL。
"""
import os
import localenv  # 确保本机运行时 .env 中的 R2 凭证已注入（在下方读取 os.environ 之前）
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
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=8 * 1024 * 1024,
        max_concurrency=10,
    )
    _get_client().upload_file(local_path, R2_BUCKET, key, Config=cfg)


_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".ogg": "video/ogg",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def guess_content_type(ext):
    """根据扩展名推断 Content-Type（R2 存储的视频需要正确的 video/* 类型才能正常播放）。"""
    return _CONTENT_TYPES.get((ext or "").lower(), "application/octet-stream")


def put_stream(key, stream, content_type="", chunk_size=8 * 1024 * 1024):
    """把文件流直接流式上传到 R2，全程不落本地磁盘。

    逐块读取（默认 8MB）并分片上传，内存/磁盘占用恒定，因此即使上传 600MB+
    的大视频也不会撑爆 Render 免费实例的临时磁盘。适用于网页后台直传场景。
    stream 需支持 read()；读取结束后由调用方负责关闭。
    """
    if not USE_R2:
        return
    client = _get_client()
    ct = content_type or "application/octet-stream"
    mpu = client.create_multipart_upload(Bucket=R2_BUCKET, Key=key, ContentType=ct)
    upload_id = mpu["UploadId"]
    parts = []
    part_no = 1
    try:
        while True:
            data = stream.read(chunk_size)
            if not data:
                break
            if isinstance(data, str):
                data = data.encode("utf-8")
            resp = client.upload_part(
                Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
                PartNumber=part_no, Body=data,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_no})
            part_no += 1
        if not parts:  # 空文件，直接普通上传
            client.put_object(Bucket=R2_BUCKET, Key=key, Body=b"", ContentType=ct)
            return
        client.complete_multipart_upload(
            Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            client.abort_multipart_upload(Bucket=R2_BUCKET, Key=key, UploadId=upload_id)
        except Exception:
            pass
        raise


def make_presigned_put(key, content_type="", expires=900):
    """生成用于「浏览器直传 R2」的预签名 PUT URL。

    返回可 PUT 上传的临时 URL（默认 15 分钟有效）。配合前端直接 fetch/PUT 把文件发到 R2，
    服务器全程不接收大文件本体，从而绕过 Render 免费实例的磁盘/内存限制。

    注意：签名只绑定 Bucket/Key，不绑定 Content-Type —— 否则浏览器以 File 作请求体时，
    只要实际 Content-Type 与签名值有丝毫差异（文件类型探测为空、多出 charset 等），
    R2 就会返回 403 导致直传失败。Content-Type 通过普通请求头传递，由调用方在 PUT 时显式设置。
    """
    if not USE_R2:
        return None
    params = {"Bucket": R2_BUCKET, "Key": key}
    try:
        return _get_client().generate_presigned_url(
            "put_object", Params=params, ExpiresIn=expires
        )
    except Exception as e:
        print("[r2] 生成预签名 PUT 失败 %s: %s" % (key, e))
        return None


# ---------------------------------------------------------------------------
# 分片上传（浏览器直传用）：大视频切成小块逐片上传，单块失败可重试，
# 天然适应不稳定网络；服务器只签名与合并，不经手文件本体。
# ---------------------------------------------------------------------------
def create_mpu(key, content_type=""):
    """创建分片上传任务，返回 upload_id。"""
    if not USE_R2:
        return None
    ct = content_type or "application/octet-stream"
    return _get_client().create_multipart_upload(
        Bucket=R2_BUCKET, Key=key, ContentType=ct
    )["UploadId"]


def presign_part(key, upload_id, part_number, expires=3600):
    """签发单个分片的预签名 PUT URL（每片单独签名，便于失败后重签重试）。"""
    if not USE_R2:
        return None
    try:
        return _get_client().generate_presigned_url(
            "upload_part",
            Params={"Bucket": R2_BUCKET, "Key": key,
                    "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expires,
        )
    except Exception as e:
        print("[r2] 生成预签名分片 URL 失败 %s#%d: %s" % (key, part_number, e))
        return None


def upload_part_bytes(key, upload_id, part_number, data):
    """由服务器把一片数据写进分片任务，返回该片 ETag。

    这是「经服务器中转」方案的核心：浏览器把 16MB 小片 POST 给服务器，
    服务器在这里直接调 R2 的 upload_part。服务器到 R2 是机房之间的链路，
    远比家庭宽带到 R2 稳定且快（实测 3.65MB/s vs 0.75MB/s）。"""
    if not USE_R2:
        return None
    resp = _get_client().upload_part(
        Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
        PartNumber=part_number, Body=data,
    )
    return resp["ETag"].strip('"')


def list_parts(key, upload_id):
    """列出该分片任务里已成功上传的分片，返回 [{"ETag","PartNumber","Size"}]。

    断点续传的依据：以 R2 上的真实状态为准，前端/localStorage 说的都不算数。"""
    if not USE_R2:
        return []
    out, marker = [], 0
    while True:
        resp = _get_client().list_parts(
            Bucket=R2_BUCKET, Key=key, UploadId=upload_id, PartNumberMarker=marker,
        )
        for p in resp.get("Parts", []):
            out.append({"ETag": p["ETag"].strip('"'),
                        "PartNumber": int(p["PartNumber"]),
                        "Size": int(p["Size"])})
        if not resp.get("IsTruncated"):
            break
        marker = int(resp.get("NextPartNumberMarker") or 0)
    return out


def find_mpu_by_key(key):
    """找出该 key 上「进行中」的分片任务 upload_id（没有则 None）。

    用于断点续传：同一个文件再次上传时，先看 R2 里有没有没做完的任务，
    有就接着传，只补缺失的分片。

    同一个 key 允许存在多个并发任务（多次失败重传会留下多个），
    这里挑「已传分片最多」的那个，保证用户续回的是最接近完成的进度。"""
    if not USE_R2:
        return None
    best, best_parts = None, -1
    for m in list_mpus():
        if m["key"] != key:
            continue
        try:
            n = len(list_parts(key, m["upload_id"]))
        except Exception:
            n = 0
        if n > best_parts:
            best, best_parts = m["upload_id"], n
    return best


def complete_mpu(key, upload_id, parts):
    """合并已上传的分片。parts: [{"ETag": ..., "PartNumber": n}, ...]（按 PartNumber 升序）。"""
    if not USE_R2:
        return
    _get_client().complete_multipart_upload(
        Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_mpu(key, upload_id):
    """放弃分片上传任务（已传分片一并清除）。"""
    if not USE_R2:
        return
    try:
        _get_client().abort_multipart_upload(Bucket=R2_BUCKET, Key=key, UploadId=upload_id)
    except Exception:
        pass


def list_mpus():
    """列出桶内进行中的分片上传任务，返回 [{key, upload_id, initiated}]。"""
    if not USE_R2:
        return []
    out, ct = [], None
    while True:
        resp = (_get_client().list_multipart_uploads(Bucket=R2_BUCKET, ContinuationToken=ct)
                if ct else _get_client().list_multipart_uploads(Bucket=R2_BUCKET))
        for u in resp.get("Uploads", []):
            out.append({"key": u["Key"], "upload_id": u["UploadId"],
                        "initiated": u["Initiated"]})
        if not resp.get("IsTruncated"):
            break
        ct = resp.get("NextContinuationToken")
    return out


def abort_stale_mpu(max_age_hours=24):
    """清理超时未完成的分片任务（浏览器中途关闭会留下孤儿分片，占存储额度）。"""
    if not USE_R2:
        return
    import datetime
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=max_age_hours)
    for m in list_mpus():
        if m["initiated"] < cutoff:
            print("[r2] 清理超时分片任务: %s (%s)" % (m["key"], m["initiated"]))
            abort_mpu(m["key"], m["upload_id"])


def object_exists(key):
    """R2 中是否存在该对象。"""
    if not USE_R2:
        return False
    try:
        _get_client().head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


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


# ---------------------------------------------------------------------------
# 容量上限（仅当启用 R2 时生效）
# ---------------------------------------------------------------------------
# 默认 10 GB，与 Cloudflare R2 免费额度一致；可用环境变量 R2_STORAGE_LIMIT_GB
# 覆盖（设为 0 或留空表示不限）。建议留默认值，超出即按 $0.015/GB/月 计费。
_R2_LIMIT_GB = (os.environ.get("R2_STORAGE_LIMIT_GB") or "").strip()
R2_STORAGE_LIMIT = (int(_R2_LIMIT_GB) * 1073741824) if _R2_LIMIT_GB not in ("", "0") else (10 * 1073741824)
# 剩余不足该值时给出告警（"快用完了"）
_R2_WARN_BYTES = 1073741824  # 1 GB


def get_usage():
    """返回桶当前已用总字节数；未启用 R2 时返回 0。"""
    if not USE_R2:
        return 0
    total = 0
    ct = None
    while True:
        resp = (_get_client().list_objects_v2(Bucket=R2_BUCKET, ContinuationToken=ct)
                if ct else _get_client().list_objects_v2(Bucket=R2_BUCKET))
        for o in resp.get("Contents", []):
            total += o["Size"]
        if not resp.get("IsTruncated"):
            break
        ct = resp.get("NextContinuationToken")
    return total


def check_capacity(extra_bytes):
    """检查新增 extra_bytes 是否会超出容量上限。

    返回 (ok, used_bytes, limit_bytes, message)：
      - ok=False  → 已超限，message 为拒绝原因（应停止上传）；
      - ok=True   → 可上传；若接近上限 message 为告警（可空）。
    未启用 R2 时永远放行（本地文件系统无此限制）。
    """
    if not USE_R2:
        return True, 0, R2_STORAGE_LIMIT, ""
    used = get_usage()
    limit = R2_STORAGE_LIMIT
    new_used = used + extra_bytes
    if new_used > limit:
        return False, used, limit, (
            "⚠ R2 免费存储额度已用尽（已用 %.2f GB / 上限 %.0f GB），"
            "本次文件（%.1f MB）未上传。请先删除旧视频或提高额度后再试。"
            % (used / 1073741824, limit / 1073741824, extra_bytes / 1048576)
        )
    if limit - new_used < _R2_WARN_BYTES:  # 剩余不足 1GB，给告警
        return True, used, limit, (
            "⚠ 注意：R2 剩余空间不足 1 GB（当前已用 %.2f / %.0f GB），"
            "再上传可能超出免费额度产生费用。" % (new_used / 1073741824, limit / 1073741824)
        )
    return True, used, limit, ""
