"""
企业微信身份接入（自建应用 · 网页授权）
仅在 config.WECOM_ENABLED 为 True 时启用；否则平台自动回退到本地「开发登录」。

流程：
  员工在企业微信内打开主页 -> 未登录 -> 跳转企业微信授权页 ->
  企业微信带回 code -> 用 code 换取 userid -> 拉取成员姓名/部门 -> 落地为平台用户
"""
import time
import json
import urllib.request
import urllib.parse
import config
import db

_token_cache = {"token": None, "expire": 0}


def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "boyang-kb"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token():
    if _token_cache["token"] and _token_cache["expire"] > time.time() + 60:
        return _token_cache["token"]
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s" % (
        config.WECOM_CORP_ID,
        config.WECOM_APP_SECRET,
    )
    data = _http_get_json(url)
    if data.get("errcode") == 0:
        _token_cache["token"] = data["access_token"]
        _token_cache["expire"] = time.time() + data.get("expires_in", 7200)
        return data["access_token"]
    raise RuntimeError("获取企业微信 access_token 失败: %s" % data)


def authorize_url(redirect_uri, state="kb"):
    base = (
        "https://open.weixin.qq.com/connect/oauth2/authorize"
        "?appid=%s&redirect_uri=%s&response_type=code&scope=snsapi_base&state=%s#wechat_redirect"
    )
    return base % (config.WECOM_CORP_ID, urllib.parse.quote(redirect_uri, safe=""), state)


def get_userid_by_code(code):
    token = get_access_token()
    url = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo?access_token=%s&code=%s" % (
        token,
        code,
    )
    data = _http_get_json(url)
    if data.get("errcode") == 0 and data.get("UserId"):
        return data["UserId"]
    raise RuntimeError("换取 userid 失败: %s" % data)


def get_user_detail(userid):
    token = get_access_token()
    url = "https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token=%s&userid=%s" % (
        token,
        userid,
    )
    data = _http_get_json(url)
    if data.get("errcode") == 0:
        dept = ",".join(str(d) for d in (data.get("department") or []))
        return {
            "userid": userid,
            "name": data.get("name", userid),
            "dept": dept,
            "avatar": data.get("avatar"),
        }
    raise RuntimeError("拉取成员信息失败: %s" % data)


def resolve_user(code):
    """用 OAuth code 解析并返回平台用户行"""
    userid = get_userid_by_code(code)
    detail = get_user_detail(userid)
    return db.get_or_create_user(
        detail["userid"], detail["name"], detail.get("dept"), detail.get("avatar")
    )
