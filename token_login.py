# -*- coding: utf-8 -*-
"""token_login.py — 迷你登录器：复用本机 Edge(WebView2) 内核，只做一件事
打开延河课堂登录页 → 你手动登录(统一身份认证) → 自动抓取登录token → 写入 auth.txt

用法:
    py token_login.py            # 默认延河课堂
    py token_login.py --site lexue   # 乐学(抓到的会话供浏览器复用, 不写auth.txt)
    py token_login.py --check    # 只检查当前 auth.txt 是否有效, 不开窗口

设计原则: 不下载、不浏览、不做任何其他事。登录成功即自动退出。
依赖: pip install pywebview  (Windows 11 自带 WebView2 运行时, 无需装浏览器)
"""
import sys, os, json, time, argparse

# Windows 后台/GUI启动时控制台默认GBK, ✓等字符会UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOGIN_URL = "https://www.yanhekt.cn/login?type=bit"
# PyInstaller 打包后 __file__ 指向临时解压目录，改用 exe 所在目录
_BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
AUTH_FILE = os.path.join(_BASE, "auth.txt")


def check_auth():
    """检查 auth.txt 里的 token 是否有效(与 utils.test_auth 同款四态语义, 独立实现避免拉入下载器依赖)"""
    import hashlib
    try:
        import requests
    except ImportError:
        print("缺少 requests: pip install requests")
        return False
    if not os.path.exists(AUTH_FILE):
        print("auth.txt 不存在（先登录一次生成）")
        return False
    token = open(AUTH_FILE, encoding="utf-8").read().strip()
    magic = "1138b69dfef641d9d7ba49137d2d4875"
    ts = str(int(time.time()))
    sig = hashlib.md5((magic + "_v1_" + ts).encode()).hexdigest()
    h = {
        "Origin": "https://www.yanhekt.cn",
        "Referer": "https://www.yanhekt.cn/",
        "xdomain-client": "web_user",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "Xclient-Signature": sig, "Xclient-Version": "v1", "Xclient-Timestamp": ts,
        "Authorization": "Bearer " + token,
    }
    try:
        r = requests.get("https://cbiz.yanhekt.cn/v1/course/initiate/sign/list",
                         headers=h, timeout=(10, 30))
        code = r.json().get("code")
        if code == 0:
            print("token 有效 ✓")
            return True
        if code == 61101113:
            print("token 无效（被服务器拒绝，需要重新登录获取）")
            return False
        if code == 61101114:
            print("WAF 限流中，无法判定 token 状态（不是过期！稍后重试或换网络）")
            return True
        print(f"未知业务码 code={code}，无法判定")
        return True
    except Exception as e:
        print(f"网络异常，无法判定 token 状态: {e}")
        return True


def harvest():
    """打开WebView2窗口: 事件钩子+2秒轮询双保险抓 localStorage.auth
    (pywebview 5+/6+: create_window 无 on_loaded 形参, 用 window.events.loaded 实例事件;
     延河是SPA, token可能在首屏加载完成之后才写入, 故加轮询线程)"""
    import threading
    import webview  # pywebview

    result = {"token": None}

    def probe(window):
        try:
            val = window.evaluate_js("localStorage.getItem('auth')")
        except Exception:
            return  # 窗口已关等
        if not val or result["token"]:
            return
        try:
            auth = json.loads(val)
        except Exception:
            return
        if auth.get("login") and auth.get("token"):
            result["token"] = auth["token"].strip()
            exp = auth.get("expired_at")
            exp_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000)) if exp else "未知"
            try:
                window.evaluate_js(
                    f"document.body.innerHTML='<h2 style=\"padding:40px;font-family:sans-serif\">"
                    f"登录成功, token已捕获 (有效至 {exp_str})<br>窗口即将自动关闭…</h2>'")
            except Exception:
                pass
            time.sleep(1.5)
            try:
                window.destroy()
            except Exception:
                pass

    window = webview.create_window(
        "延河课堂登录 - token获取器 (登录成功自动关闭)",
        LOGIN_URL, width=1100, height=800,
    )
    try:
        window.events.loaded += lambda: probe(window)
    except Exception:
        pass  # 老版本pywebview无events, 仅靠轮询

    def poller():
        while not result["token"]:
            time.sleep(2)
            probe(window)
            try:
                window.evaluate_js("1")  # 窗口关了会抛, 退出轮询
            except Exception:
                return

    threading.Thread(target=poller, daemon=True).start()
    webview.start()  # Windows 上默认使用 Edge WebView2
    return result["token"]


def main():
    ap = argparse.ArgumentParser(description="迷你登录器: 只做登录+抓token")
    ap.add_argument("--check", action="store_true", help="只检查现有token")
    args = ap.parse_args()

    if args.check:
        sys.exit(0 if check_auth() else 1)

    print("打开登录窗口…请在窗口中完成统一身份认证登录。")
    token = harvest()
    if not token:
        print("未捕获到token(窗口被关闭或未完成登录)")
        sys.exit(1)

    # 备份旧token再写入
    if os.path.exists(AUTH_FILE):
        os.replace(AUTH_FILE, AUTH_FILE + ".bak")
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    print(f"✅ token已写入 auth.txt (长度{len(token)})")
    print("提示: 现在管线/下载器可以正常拉取课程列表了。")
    check_auth()


if __name__ == "__main__":
    main()
