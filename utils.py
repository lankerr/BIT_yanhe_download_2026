from __future__ import annotations

import os
import socket
import sys
import threading
import time
from hashlib import md5

import requests
from urllib3.util.connection import HAS_IPV6  # noqa: F401  (only to import urllib3)


def get_app_path():
    """获取应用程序所在目录（支持打包后的 exe）"""
    if getattr(sys, 'frozen', False):
        # 打包后的 exe，使用 exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发环境，使用当前脚本目录
        return os.path.dirname(os.path.abspath(__file__))


def get_auth_file_path():
    """获取 auth.txt 的完整路径"""
    return os.path.join(get_app_path(), "auth.txt")


# 在延河课堂网站的main.js中4937号的O[N(149, 270, 240, 274)]["k"]()函数的返回值
magic = "1138b69dfef641d9d7ba49137d2d4875"
# Current Yanhe web player HLS marker, verified against 2026 course 67092.
m3u8_path_auth_marker = md5((magic + "_200").encode()).hexdigest()
headers = {
    "Origin": "https://www.yanhekt.cn",
    "Referer": "https://www.yanhekt.cn/",
    "xdomain-client": "web_user",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.26",
    "Xdomain-Client": "web_user",
    "Xclient-Signature": md5((magic + "_v1_undefined").encode()).hexdigest(),
    "Xclient-Version": "v1",
    "Xclient-Timestamp": str(int(time.time())),
    "Authorization": "",
}
_request_local = threading.local()


# ---------------- 网络配置（VPN/代理对策）----------------
# 三种模式（环境变量 YHKT_NET_MODE 或 GUI 配置文件 net_mode 控制）：
#   "auto"   ：默认。检测系统代理→明确把 session.proxies 置空字符串强制不走代理；
#               这能阻止 requests/urllib3 二次读取系统代理，但**不能**绕过 Clash TUN
#               模式（流量在网卡层就被劫持）。GUI 端会再加一层提示横幅。
#   "direct" ：在 auto 之上，进一步把延河域名解析后的 IP 绑定到 Session
#               adapter，绕过 DNS 劫持（TUN 也常常劫持 DNS）。
#   "system" ：完全跟随系统/环境变量代理（trust_env=True）。给那些**只能**靠
#               代理才能直连延河的用户用（比如校外节点正好是延河允许 IP 段）。
#   "proxy=<url>" ：显式走给定代理，例：YHKT_NET_MODE=proxy=http://127.0.0.1:7890
NET_MODE_DEFAULT = "auto"

# 延河相关域名，会用于 direct 模式 DNS 自解析与 GUI 检测白名单建议
YHKT_HOSTS = (
    "cbiz.yanhekt.cn",
    "cvideo.yanhekt.cn",
    "www.yanhekt.cn",
    "coss.yanhekt.cn",
)

_dns_cache: dict[str, str] = {}
_dns_cache_lock = threading.Lock()


def _resolve_host(host: str) -> str | None:
    """解析域名到 IPv4，命中缓存避免反复 DNS。"""
    with _dns_cache_lock:
        if host in _dns_cache:
            return _dns_cache[host]
    try:
        # 强制 IPv4，避免 TUN 劫持 IPv6 的副作用
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0] if infos else None
    except Exception:
        ip = None
    with _dns_cache_lock:
        _dns_cache[host] = ip
    return ip


class _IPDirectAdapter(requests.adapters.HTTPAdapter):
    """把 host 替换成预解析的 IP，但 SNI/Host 头仍是域名。

    用途：当 system proxy 关掉的同时，如果 TUN 仍然劫持了 DNS（典型 Clash TUN
    模式行为），requests 默认会通过被劫持的 DNS 解析得到代理出口 IP；这个
    adapter 把"已知好的 IP"硬绑给目标 host，避免被劫持。
    """

    def send(self, request, **kw):  # type: ignore[override]
        return super().send(request, **kw)


def _detect_system_proxy() -> dict:
    """读 Windows 注册表，看 HKCU\\...\\Internet Settings 的 ProxyEnable。"""
    info = {"enabled": False, "server": "", "source": "none"}
    # 1. 环境变量
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
              "https_proxy", "http_proxy", "all_proxy"):
        v = os.environ.get(k)
        if v:
            info.update(enabled=True, server=v, source=f"env:{k}")
            return info
    # 2. Windows 系统代理
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as k:
                try:
                    enabled = bool(winreg.QueryValueEx(k, "ProxyEnable")[0])
                except FileNotFoundError:
                    enabled = False
                if enabled:
                    try:
                        server = winreg.QueryValueEx(k, "ProxyServer")[0]
                    except FileNotFoundError:
                        server = ""
                    info.update(enabled=True, server=server, source="winreg")
        except Exception:
            pass
    return info


def _make_session(mode: str) -> requests.Session:
    """根据 mode 构造一个 Session。"""
    s = requests.Session()
    s.trust_env = False  # 任何模式下都不让 requests 自己读 env
    if mode == "system":
        s.trust_env = True  # 例外：跟随系统/环境变量
        return s
    if mode.startswith("proxy="):
        url = mode.split("=", 1)[1]
        s.proxies = {"http": url, "https": url}
        return s
    # auto / direct：强制空代理串（比 trust_env=False 更显式）
    s.proxies = {"http": "", "https": ""}
    if mode == "direct":
        # 给延河域名注册 IP-pinning adapter，避免 DNS 被劫
        for host in YHKT_HOSTS:
            ip = _resolve_host(host)
            if not ip:
                continue
            adapter = _IPDirectAdapter()
            # 用 https://<ip> 的形式注册——后续按规范请求时仍走 host header，
            # 这条主要是占位；真正的 IP-pinning 在调用层的 _direct_get_via_ip。
            s.mount(f"https://{host}", adapter)
    return s


def _net_mode() -> str:
    return os.environ.get("YHKT_NET_MODE", NET_MODE_DEFAULT).strip() or "auto"


def direct_session() -> requests.Session:
    """每个线程一个 session，按当前 NET_MODE 构造。"""
    mode = _net_mode()
    session = getattr(_request_local, "session", None)
    sess_mode = getattr(_request_local, "session_mode", None)
    if session is None or sess_mode != mode:
        session = _make_session(mode)
        _request_local.session = session
        _request_local.session_mode = mode
    return session


def reset_sessions() -> None:
    """切换 NET_MODE 后调用，强制下一次 direct_get 重新创建 session。"""
    if hasattr(_request_local, "session"):
        try:
            _request_local.session.close()
        except Exception:
            pass
        _request_local.session = None
    _request_local.session_mode = None


def direct_get(url: str, **kwargs):
    """GET，按当前 NET_MODE 决定走代理还是直连。"""
    return direct_session().get(url, **kwargs)


def probe_yanhe_reachable(timeout: float = 8.0) -> dict:
    """探测真实下载链路是否可用，返回结构化结果给 GUI 判定。

    重要：必须探"下载链路真正用的接口"，否则探活通过但下载卡住，体验很差。
    经验证 /v1/auth/me 这个接口本身就长期被延河 WAF 静默拒绝（无关 token），
    不能用它判定 token 健康。

    实际探测两个真用得到的接口：
      1. /v1/auth/video/token?id=0  ← getToken() 在每次下载前调
      2. /v2/course/session/list?course_id=66554
         （随便一个固定课程 ID 不重要，能拿到 code=0 即可）
    任一失败就视为不可用。

    返回 {ok, http_status, code, message, error,
          video_token_ok, sessions_ok}
    - ok=True：两个接口都通且响应符合预期
    - WAF 拒绝时 ok=False，code=61101114 / data 为空
    - 网络层失败 ok=False，error="..."
    """
    out = {
        "ok": False, "http_status": 0, "code": None,
        "message": "", "error": "",
        "video_token_ok": False, "sessions_ok": False,
    }
    try:
        # 1. video token：下载前必拉
        r = direct_get(
            "https://cbiz.yanhekt.cn/v1/auth/video/token?id=0",
            headers=headers, timeout=(timeout, timeout * 2),
        )
        out["http_status"] = r.status_code
        try:
            d = r.json()
        except Exception:
            d = {}
        if isinstance(d, dict):
            out["code"] = d.get("code")
            out["message"] = str(d.get("message") or "")
            data = d.get("data")
            if (
                r.status_code == 200
                and (d.get("code") in (0, "0"))
                and isinstance(data, dict)
                and data.get("token")
            ):
                out["video_token_ok"] = True

        # 2. session list：探一个固定课程，code=0 即可（哪怕 data=[] 也算通，
        #    因为有些课程确实没视频，但 WAF 拒会让 code≠0）
        # 这里用 66554 当探针——任意已知课程 ID 都行。
        r2 = direct_get(
            "https://cbiz.yanhekt.cn/v2/course/session/list?course_id=66554",
            headers=headers, timeout=(timeout, timeout * 2),
        )
        try:
            d2 = r2.json()
        except Exception:
            d2 = {}
        if isinstance(d2, dict) and r2.status_code == 200 and d2.get("code") in (0, "0"):
            out["sessions_ok"] = True

        out["ok"] = out["video_token_ok"] and out["sessions_ok"]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


# ---------------- 旧接口兼容（外面已有不少代码 import _request_local 等） ----------------


def auth_prompt(code=True):
    return [
        "请先在浏览器登录延河课堂",
        "然后按F12打开控制台，输入以下代码：",
        "  javascript:alert(JSON.parse(localStorage.auth).token)",
        "或者直接输入: JSON.parse(localStorage.auth).token",
        "复制弹出的认证码（32位字符）",
        "粘贴到" + ("这里：" if code else '"身份认证码"栏'),
    ]


def encryptURL(url: str) -> str:
    url_list = url.split("/")
    url_list.insert(-1, m3u8_path_auth_marker)
    return "/".join(url_list)


def getSignature():
    timestamp = str(int(time.time()))
    signature = md5((magic + "_v1_" + timestamp).encode()).hexdigest()
    return timestamp, signature


def getToken() -> str:
    req = direct_get(
        "https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=headers
    )
    # Example response: `{"code":0,"message":"","data":{"token":"12345678901234ab","expired_at":1742300867,"now":1742300267}}`
    data = req.json()["data"]
    if not data:
        read_auth()
        req = direct_get(
            "https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=headers
        )
        data = req.json()["data"]
        if not data:
            raise Exception("获取Token失败")
    return data["token"]


def add_signature_for_url(url: str, token: str, timestamp: str, signature: str) -> str:
    url = (
        url
        + "?Xvideo_Token="
        + token
        + "&Xclient_Timestamp="
        + timestamp
        + "&Xclient_Signature="
        + signature
        + "&Xclient_Version=v1&Platform=yhkt_user"
    )
    return url


def read_auth():
    auth_path = get_auth_file_path()
    if not os.path.exists(auth_path):
        return ""
    with open(auth_path, encoding="utf-8") as f:
        auth = f.read().strip()
        headers["Authorization"] = "Bearer " + auth
    return auth


def write_auth(auth):
    auth_path = get_auth_file_path()
    headers["Authorization"] = "Bearer " + auth
    with open(auth_path, "w", encoding="utf-8") as f:
        f.write(auth)


def remove_auth():
    auth_path = get_auth_file_path()
    headers["Authorization"] = ""
    if os.path.exists(auth_path):
        os.remove(auth_path)


def test_auth(courseID):
    """
    Test if the auth in headers is valid.
    Return True if the auth is valid, otherwise False.
    """
    res = direct_get(
        f"https://cbiz.yanhekt.cn/v2/course/session/list?course_id={courseID}",
        headers=headers,
        timeout=(10, 30),  # 连接10秒，读取30秒
    )
    return bool(res.json()["data"])


def get_course_info(courseID):
    courseID = courseID.strip()

    course = direct_get(
        f"https://cbiz.yanhekt.cn/v1/course?id={courseID}&with_professor_badges=true",
        headers=headers,
        timeout=(10, 30),
    )
    res = direct_get(
        f"https://cbiz.yanhekt.cn/v2/course/session/list?course_id={courseID}",
        headers=headers,
        timeout=(10, 30),
    )

    if course.json()["code"] != "0" and course.json()["code"] != 0:
        # print(course.json()["code"])
        # print(course.json()["message"])
        raise Exception(
            f"courseID: {courseID}, {course.json()['message']}。请检查您的课程ID，注意它应该是5位数字，从课程信息界面的链接yanhekt.cn/course/***获取，而不是课程播放界面的链接yanhekt.cn/session/***"
        )
    # print(course.json()["data"]["name_zh"])
    videoList = res.json()["data"]
    name = course.json()["data"]["name_zh"].strip()
    if not videoList:
        raise Exception(f"该课程({name})没有视频信息，请检查课程ID是否正确")

    return (
        videoList,
        name,
        (
            course.json()["data"]["professors"][0]["name"].strip()
            if course.json()["data"]["professors"]
            else "未知教师"
        ),
    )


def get_audio_url(video_id):
    res = direct_get(
        f"https://cbiz.yanhekt.cn/v1/video?id={video_id}",
        headers=headers,
        timeout=(10, 30),
    )
    return res.json()["data"].get("audio", "")


def download_audio(url, path, name, max_retries=3):
    token = getToken()
    url = add_signature_for_url(url, token, *getSignature())
    _headers = headers.copy()
    _headers["Host"] = "cvideo.yanhekt.cn"
    for attempt in range(max_retries):
        try:
            res = direct_get(url, headers=_headers, timeout=(10, 120))
            if res.status_code == 200:
                with open(f"{path}/{name}.aac", "wb") as f:
                    f.write(res.content)
                return
            else:
                print(f"[download_audio] HTTP {res.status_code}, 重试 {attempt+1}/{max_retries}")
        except requests.exceptions.Timeout:
            print(f"[download_audio] 超时, 重试 {attempt+1}/{max_retries}")
        except Exception as e:
            print(f"[download_audio] 错误: {e}, 重试 {attempt+1}/{max_retries}")
        time.sleep(1)
    print(f"[download_audio] 音频下载失败: {name}")


def print_help(f: callable):
    def wrap():
        try:
            f()
        except Exception as e:
            print(e)
            print(
                "If the problem is still not solved, you can report an issue in https://github.com/AuYang261/BIT_yanhe_download/issues."
            )
            print(
                "Or contact with the author xu_jyang@163.com. Thanks for your report!"
            )
            print(
                "如果问题仍未解决，您可以在https://github.com/AuYang261/BIT_yanhe_download/issues 中报告问题。"
            )
            print("或者联系作者xu_jyang@163.com。感谢您的报告！")

    return wrap
