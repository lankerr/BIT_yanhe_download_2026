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

LOGIN_URL = "https://www.yanhekt.cn/login?type=bit"
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.txt")


def check_auth():
    """检查 auth.txt 里的 token 是否还有效(调一次轻量接口)"""
    try:
        import utils
        if not os.path.exists(AUTH_FILE):
            print("auth.txt 不存在")
            return False
        utils.read_auth()
        r = utils.api_get(
            "https://cbiz.yanhekt.cn/v1/course/initiate/sign/list",
            headers=utils.headers, timeout=(10, 30))
        j = r.json()
        ok = j.get("code") == 0
        print(f"token {'有效 ✓' if ok else '无效(需重新登录)'}")
        return ok
    except Exception as e:
        print(f"检查失败: {e}")
        return False


def harvest():
    """打开WebView2窗口, 轮询localStorage直到出现登录token"""
    import webview  # pywebview

    result = {"token": None}

    def on_loaded(window):
        # 页面每次加载后都探一次 localStorage.auth
        try:
            val = window.evaluate_js("localStorage.getItem('auth')")
        except Exception:
            return
        if not val:
            return
        try:
            auth = json.loads(val)
        except Exception:
            return
        if auth.get("login") and auth.get("token") and not result["token"]:
            result["token"] = auth["token"]
            exp = auth.get("expired_at")
            if exp:
                exp_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000))
            else:
                exp_str = "未知"
            window.evaluate_js(
                f"document.body.innerHTML='<h2 style=\"padding:40px;font-family:sans-serif\">"
                f"✅ 登录成功, token已捕获 (有效至 {exp_str})<br>窗口即将自动关闭…</h2>'")
            time.sleep(1.5)
            window.destroy()

    webview.create_window(
        "延河课堂登录 - token获取器 (登录成功自动关闭)",
        LOGIN_URL, width=1100, height=800,
        on_loaded=on_loaded,
    )
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
