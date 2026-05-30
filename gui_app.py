"""
延河课堂下载器 - 现代化GUI界面
基于 AuYang261/BIT_yanhe_download 改进
新增：多线程自适应下载、Watchdog监控、关键帧提取等功能

通过入口脚本注入 YHKT_EDITION=simple|full 切换简易版 / 完整版。
简易版隐藏后处理工具入口，并按需懒加载 PPT/Whisper 模块。
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
import threading
import queue
import os
import sys
import time
import logging
import traceback
from typing import Optional, List, Dict, Callable

from app_paths import (
    get_app_dir as _shared_get_app_dir,
    resource_path as _shared_resource_path,
    is_full_edition,
    edition_label,
)


def get_resource_path(relative_path):
    return _shared_resource_path(relative_path)


def get_app_dir():
    return _shared_get_app_dir()


# 设置工作目录为应用程序目录（确保相对路径正确）
app_dir = get_app_dir()
os.chdir(app_dir)
log_file_path = os.path.join(app_dir, 'gui_debug.log')

# 配置日志 - 同时输出到控制台和文件
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8', mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.info("=" * 50)
logger.info(f"GUI 启动中... 应用目录: {app_dir}")
logger.info(f"日志文件: {log_file_path}")

# 导入项目模块
try:
    import utils
    logger.info("utils 模块加载成功")
    # 启动时加载已保存的 token
    saved_token = utils.read_auth()
    if saved_token:
        logger.info(f"已加载保存的 token: {saved_token[:20]}...")
    else:
        logger.info("没有已保存的 token")
except Exception as e:
    logger.error(f"utils 模块加载失败: {e}")
    raise

try:
    import m3u8dl
    logger.info("m3u8dl 模块加载成功")
except Exception as e:
    logger.error(f"m3u8dl 模块加载失败: {e}")
    raise

try:
    import history as download_history
    logger.info("history 模块加载成功")
except Exception as e:
    logger.error(f"history 模块加载失败: {e}")
    download_history = None  # type: ignore

# 设置外观（GitHub Dark / Grok 风格）
ctk.set_appearance_mode("dark")
# 使用内置主题，并在组件上覆盖关键色彩以呈现极客黑风格
# 参考配色：背景 #0d1117，面板 #161b22，文字 #c9d1d9，强调 #58a6ff，成功 #3fb950，警告 #f2cc60，错误 #f85149
ctk.set_default_color_theme("blue")
logger.info("CustomTkinter 主题设置完成")

# ---------- 主题配色（Antigravity 风格，DaisyUI dark）----------
# 真正的色板定义在 theme.py，这里 import 进来。
from theme import (  # noqa: E402
    THEME,
    G_BG, G_PANEL, G_PANEL_HI, G_BORDER,
    G_TEXT, G_TEXT_DIM,
    G_ACCENT, G_ACCENT_H,
    G_SUCCESS, G_WARN, G_ERROR,
)


class DownloadTask:
    """下载任务类"""
    def __init__(self, session_info: dict, course_name: str, professor: str,
                 download_type: str, download_audio: bool = False, output_dir: str = "output"):
        self.session_info = session_info
        self.course_name = course_name
        self.professor = professor
        self.download_type = download_type  # 'video' or 'screen'
        self.download_audio = download_audio
        self.output_dir = output_dir  # 输出目录
        self.title = session_info['title']
        self.name = f"{course_name}-{professor}-{self.title}"
        
        self.progress = 0
        self.total = 0
        self.status = "等待中"  # 等待中, 下载中, 合并中, 完成, 失败
        self.error_msg = ""
        self.current_threads = 0
        self.max_threads = 0
        self.max_workers = 16  # 默认并发，docs/speed_stage4_fullseg.md 实测饱和点


class LoginFrame(ctk.CTkFrame):
    """登录框架"""
    def __init__(self, master, on_login_success: Callable, on_post_process: Callable = None):
        super().__init__(master)
        self.on_login_success = on_login_success
        self.on_post_process = on_post_process
        self.output_directory = "output"  # 默认输出目录
        
        # 标题
        self.configure(fg_color=G_BG)

        self.title_label = ctk.CTkLabel(
            self, text="延河课堂下载器", 
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=G_TEXT
        )
        self.title_label.pack(pady=(30, 10))
        
        self.subtitle = ctk.CTkLabel(
            self, text="多线程自适应下载 · Watchdog监控 · 关键帧提取",
            font=ctk.CTkFont(size=12),
            text_color=G_TEXT_DIM
        )
        self.subtitle.pack(pady=(0, 30))

        # 网络诊断横幅（VPN/代理/WAF 检测）—— 启动时异步探测，发现问题再显示
        self.net_banner: ctk.CTkFrame | None = None
        self._schedule_network_check()
        
        # 课程ID输入
        self.course_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.course_frame.pack(fill="x", padx=40, pady=10)

        self.course_label = ctk.CTkLabel(
            self.course_frame, text="课程ID:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.course_label.pack(anchor="w", pady=(10, 5))

        # 输入框 + 历史下拉按钮
        course_row = ctk.CTkFrame(self.course_frame, fg_color="transparent")
        course_row.pack(fill="x", pady=(0, 10))

        self.course_entry = ctk.CTkEntry(
            course_row,
            placeholder_text="输入5位课程编号，如 40524",
            height=40,
            font=ctk.CTkFont(size=14)
        )
        self.course_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.history_btn = ctk.CTkButton(
            course_row,
            text="▼ 历史",
            width=80,
            height=40,
            font=ctk.CTkFont(size=13),
            fg_color=G_PANEL_HI,
            hover_color=G_BORDER,
            text_color=G_TEXT,
            command=self.toggle_history_panel,
        )
        self.history_btn.pack(side="right")

        # 历史下拉面板（默认折叠，展开时插入到 course_frame 末尾）
        self.history_panel = None
        self.history_panel_visible = False

        # 输出目录选择
        self.output_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.output_frame.pack(fill="x", padx=40, pady=10)

        self.output_label = ctk.CTkLabel(
            self.output_frame, text="保存目录:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.output_label.pack(anchor="w", pady=(10, 5))

        # 目录选择容器
        dir_container = ctk.CTkFrame(self.output_frame, fg_color="transparent")
        dir_container.pack(fill="x")

        self.output_entry = ctk.CTkEntry(
            dir_container,
            placeholder_text="默认保存到 output/ 目录",
            height=40,
            font=ctk.CTkFont(size=14)
        )
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.browse_btn = ctk.CTkButton(
            dir_container,
            text="浏览...",
            width=80,
            height=40,
            command=self.browse_output_dir
        )
        self.browse_btn.pack(side="right")

        # Token输入
        self.token_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.token_frame.pack(fill="x", padx=40, pady=10)
        
        self.token_label = ctk.CTkLabel(
            self.token_frame, text="身份认证码 (必填):",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.token_label.pack(anchor="w", pady=(10, 5))
        
        self.token_entry = ctk.CTkEntry(
            self.token_frame,
            placeholder_text="请输入Token（按F12获取后复制）",
            height=40,
            font=ctk.CTkFont(size=14)
        )
        self.token_entry.pack(fill="x", pady=(0, 5))

        # Token获取代码和复制按钮
        code_frame = ctk.CTkFrame(self.token_frame, fg_color=THEME["code_bg"])
        code_frame.pack(fill="x", pady=(5, 10))

        # 代码显示
        self.code_label = ctk.CTkLabel(
            code_frame,
            text="javascript:alert(JSON.parse(localStorage.auth).token)",
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color=THEME["code_text"],
            anchor="w"
        )
        self.code_label.pack(side="left", fill="x", expand=True, padx=10, pady=8)

        # 复制按钮
        self.copy_btn = ctk.CTkButton(
            code_frame,
            text="复制代码",
            width=70,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color=G_SUCCESS,
            hover_color=THEME["accent_h"],
            command=self.copy_token_code
        )
        self.copy_btn.pack(side="right", padx=5, pady=5)

        # 获取方法说明
        method_text = (
            "获取Token方法：\n"
            "方法1：在延河课堂网页地址栏输入（浏览器会自动去掉javascript:前缀，需手动补上）\n"
            "方法2：按F12打开控制台，粘贴运行（推荐）"
        )
        self.method_label = ctk.CTkLabel(
            self.token_frame,
            text=method_text,
            font=ctk.CTkFont(size=9),
            text_color=G_TEXT_DIM,
            anchor="w",
            justify="left"
        )
        self.method_label.pack(anchor="w", pady=(0, 10))
        
        # 加载已保存的token
        saved_auth = utils.read_auth()
        if saved_auth:
            self.token_entry.insert(0, saved_auth)
        
        # 获取课程按钮
        self.fetch_btn = ctk.CTkButton(
            self, text="获取课程列表", 
            height=45,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=G_ACCENT,
            hover_color=G_ACCENT_H,
            command=self.fetch_course
        )
        self.fetch_btn.pack(pady=(30, 10), padx=40, fill="x")

        # 后处理工具按钮（仅完整版可用；简易版直接不显示，避免误触发未打包模块）
        if is_full_edition():
            self.postprocess_btn = ctk.CTkButton(
                self, text="🔧 后处理工具 (课件提取 + 音频转录)",
                height=40,
                font=ctk.CTkFont(size=14),
                fg_color=THEME["accent"],
                hover_color=THEME["accent_h"],
                command=self._open_post_process
            )
            self.postprocess_btn.pack(pady=(0, 10), padx=40, fill="x")
        
        # 状态标签
        self.status_label = ctk.CTkLabel(
            self, text="", 
            font=ctk.CTkFont(size=12),
            text_color=G_WARN
        )
        self.status_label.pack(pady=10)
        logger.info("LoginFrame 初始化完成")

        # 截图 / 演示模式：启动后自动展开历史面板，便于生成文档示意图
        if os.environ.get("YHKT_DEMO_HISTORY") == "1":
            self.after(800, self._show_history_panel)

        # 演示模式：模拟 WAF 风控横幅 + 自动重试倒计时
        if os.environ.get("YHKT_DEMO_WAF") == "1":
            self.after(800, lambda: self._render_net_banner(
                {"enabled": True, "server": "127.0.0.1:7890", "source": "winreg"},
                {"ok": False, "http_status": 200, "code": 61101114,
                 "message": "系统繁忙", "error": ""},
            ))
            # 1.5s 后模拟点"自动重试"
            self.after(1500, self._start_waf_retry)
    
    def fetch_course(self):
        course_id = self.course_entry.get().strip()
        token = self.token_entry.get().strip()
        output_dir = self.output_entry.get().strip()

        # 保存输出目录（如果为空则使用默认output）
        self.output_directory = output_dir if output_dir else "output"
        logger.info(f"开始获取课程: course_id={course_id}, token长度={len(token) if token else 0}, 输出目录={self.output_directory}")
        
        if not course_id:
            logger.warning("课程ID为空")
            messagebox.showerror("错误", "请输入课程ID")
            return
        
        if not course_id.isdigit():
            logger.warning(f"课程ID不是数字: {course_id}")
            messagebox.showerror("错误", "课程ID应为数字")
            return
        
        self.status_label.configure(text="正在获取课程信息...", text_color=G_WARN)
        self.fetch_btn.configure(state="disabled")
        logger.info("开始异步获取课程信息...")
        
        def do_fetch():
            logger.info("do_fetch 线程开始执行")
            try:
                # 如果有token，先保存
                if token:
                    logger.info("保存新token")
                    utils.write_auth(token)
                elif not utils.read_auth():
                    logger.warning("没有可用的token")
                    self.after(0, lambda: self.show_error(
                        "需要身份认证码",
                        "请先在浏览器登录延河课堂，然后获取Token：\n\n"
                        "1. 打开 https://www.yanhekt.cn 并登录\n"
                        "2. 按F12打开控制台，粘贴运行：JSON.parse(localStorage.auth).token\n"
                        "3. 复制弹出的认证码（32位字符）粘贴到下方输入框\n\n"
                        "提示：可以点击界面上的「复制」按钮直接复制代码"
                    ))
                    self.after(0, lambda: self.fetch_btn.configure(state="normal"))
                    return
                
                # 测试认证（带超时检测）
                logger.info("开始测试认证...")
                self.after(0, lambda: self.status_label.configure(
                    text="正在验证身份...", text_color=G_WARN
                ))
                
                try:
                    logger.info(f"调用 utils.test_auth({course_id})")
                    auth_result = utils.test_auth(course_id)
                    logger.info(f"认证结果: {auth_result}")
                except Exception as auth_err:
                    logger.error(f"认证异常: {type(auth_err).__name__}: {auth_err}")
                    logger.error(traceback.format_exc())
                    error_msg = str(auth_err)
                    if "ProxyError" in error_msg or "proxy" in error_msg.lower():
                        self.after(0, lambda: self.show_error(
                            "代理/VPN 连接错误",
                            "下载器已默认绕过系统代理直连延河域名，但当前网络仍返回了代理/VPN错误。\n\n"
                            "建议：\n"
                            "1. 将 yanhekt.cn 和 cvideo.yanhekt.cn 加入代理/VPN 直连白名单\n"
                            "2. 或临时关闭代理/VPN 后重试\n"
                            "3. 确保浏览器能正常访问 https://www.yanhekt.cn"
                        ))
                    elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                        self.after(0, lambda: self.show_error(
                            "连接超时",
                            "无法连接到延河课堂服务器。\n\n"
                            "可能原因：\n"
                            "1. 网络连接不稳定\n"
                            "2. 延河课堂服务器暂时不可用\n"
                            "3. VPN/代理规则拦截了直连请求\n\n"
                            "建议：将 yanhekt.cn / cvideo.yanhekt.cn 加入直连白名单后重试"
                        ))
                    elif "SSLError" in error_msg or "certificate" in error_msg.lower():
                        self.after(0, lambda: self.show_error(
                            "SSL证书错误",
                            "HTTPS连接失败，可能是代理软件拦截了请求。\n\n"
                            "解决方案：将延河域名加入代理/VPN 直连白名单，或临时关闭代理/VPN 后重试"
                        ))
                    else:
                        self.after(0, lambda e=error_msg: self.show_error(
                            "网络连接错误",
                            f"无法连接到延河课堂：\n{e[:200]}\n\n"
                            "建议：检查网络连接；如使用 VPN/代理，请将延河域名加入直连白名单"
                        ))
                    self.after(0, lambda: self.fetch_btn.configure(state="normal"))
                    return

                if not auth_result:
                    if token:
                        self.after(0, lambda: self.show_error(
                            "身份认证失败",
                            "Token无效或已过期。\n\n"
                            "请重新获取：\n"
                            "1. 打开 https://www.yanhekt.cn 并登录\n"
                            "2. 按F12打开控制台，粘贴运行：JSON.parse(localStorage.auth).token\n"
                            "3. 复制新的认证码（32位字符）\n\n"
                            "提示：可以点击界面上的「复制」按钮直接复制代码"
                        ))
                    else:
                        self.after(0, lambda: self.show_error(
                            "身份认证失败",
                            "已保存的Token已过期，请重新输入认证码。"
                        ))
                    self.after(0, lambda: self.fetch_btn.configure(state="normal"))
                    return
                
                # 获取课程信息
                logger.info("开始获取课程信息...")
                self.after(0, lambda: self.status_label.configure(
                    text="正在获取课程列表...", text_color=G_WARN
                ))
                
                try:
                    logger.info(f"调用 utils.get_course_info({course_id})")
                    video_list, course_name, professor = utils.get_course_info(course_id)
                    logger.info(f"获取成功: 课程={course_name}, 教师={professor}, 视频数={len(video_list) if video_list else 0}")
                except Exception as course_err:
                    logger.error(f"获取课程信息异常: {type(course_err).__name__}: {course_err}")
                    logger.error(traceback.format_exc())
                    raise
                
                if not video_list:
                    logger.warning("课程没有视频")
                    self.after(0, lambda: self.show_error(
                        "课程无视频",
                        f"课程 {course_name} 没有可下载的视频。\n"
                        "请检查课程ID是否正确。"
                    ))
                    self.after(0, lambda: self.fetch_btn.configure(state="normal"))
                    return
                
                # 成功，切换到选择界面
                logger.info("准备切换到课程选择界面")
                # 写入下载历史（失败不影响主流程）
                if download_history is not None:
                    try:
                        download_history.add_entry(course_id, course_name, professor)
                    except Exception as _hist_err:
                        logger.warning(f"写入下载历史失败: {_hist_err}")
                self.after(0, lambda: self.on_login_success(
                    course_id, video_list, course_name, professor
                ))
                
            except Exception as e:
                logger.error(f"do_fetch 未捕获异常: {type(e).__name__}: {e}")
                logger.error(traceback.format_exc())
                error_msg = str(e)
                if "ProxyError" in error_msg or "proxy" in error_msg.lower():
                    self.after(0, lambda: self.show_error(
                        "VPN/代理错误", 
                        "下载器已默认绕过系统代理直连延河域名。\n"
                        "如果仍失败，请把 yanhekt.cn 和 cvideo.yanhekt.cn 加入代理/VPN 直连白名单，或临时关闭代理/VPN 后重试。"
                    ))
                elif "没有视频信息" in error_msg or "课程ID" in error_msg:
                    self.after(0, lambda e=error_msg: self.show_error(
                        "课程不存在", e
                    ))
                else:
                    self.after(0, lambda e=error_msg: self.show_error(
                        "获取课程失败", 
                        f"{e[:150]}\n\n常见解决方案：\n1. 检查课程ID是否正确\n2. 将延河域名加入代理/VPN直连白名单\n3. 重新获取Token"
                    ))
                self.after(0, lambda: self.fetch_btn.configure(state="normal"))
        
        thread = threading.Thread(target=do_fetch, daemon=True)
        thread.start()
        logger.info(f"do_fetch 线程已启动: {thread.name}")
    
    def browse_output_dir(self):
        """选择输出目录"""
        from tkinter import filedialog
        directory = filedialog.askdirectory(
            title="选择保存目录",
            initialdir=os.getcwd()
        )
        if directory:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, directory)

    def copy_token_code(self):
        """复制Token获取代码到剪贴板"""
        code = "javascript:alert(JSON.parse(localStorage.auth).token)"
        self.clipboard_clear()
        self.clipboard_append(code)
        self.update()  # 保持剪贴板内容
        # 显示复制成功提示
        self.copy_btn.configure(text="已复制!", fg_color=G_SUCCESS, hover_color=G_SUCCESS)
        self.after(2000, lambda: self.copy_btn.configure(
            text="复制代码", fg_color=G_SUCCESS, hover_color=THEME["accent_h"]
        ))

    def show_error(self, title: str, message: str):
        """显示详细错误信息"""
        self.status_label.configure(text=f"❌ {title}", text_color=G_ERROR)
        messagebox.showerror(title, message)

    def _open_post_process(self):
        """打开后处理工具"""
        if self.on_post_process:
            self.on_post_process()

    # ---------- 网络/代理诊断 ----------
    def _schedule_network_check(self):
        """LoginFrame 启动 600ms 后异步做一次网络检测，避免阻塞 UI。"""
        try:
            self.after(600, self._run_network_check_async)
        except Exception:
            pass

    def _run_network_check_async(self):
        def _job():
            sys_proxy = utils._detect_system_proxy()
            probe = utils.probe_yanhe_reachable(timeout=8)
            self.after(0, lambda: self._render_net_banner(sys_proxy, probe))
        threading.Thread(target=_job, daemon=True).start()

    def _render_net_banner(self, sys_proxy: dict, probe: dict):
        """根据探测结果在 token 区上方显示提示横幅。
        - 探活通：绿色一行紧凑显示
        - 探活失败但系统代理开着：黄色，建议加白名单或临时关代理
        - 探活失败且没代理：橙色，token 可能过期或被风控
        """
        # 清掉旧的
        if self.net_banner is not None:
            try: self.net_banner.destroy()
            except Exception: pass
            self.net_banner = None

        proxy_on = sys_proxy.get("enabled")
        code = probe.get("code")
        http_status = probe.get("http_status")
        err = probe.get("error")

        # 三态判定
        if probe.get("ok"):
            state = "ok"
            title = "🟢 Token 正常 · 网络通畅"
            detail = f"模式：{utils._net_mode()}    " + (
                f"系统代理：{sys_proxy.get('server','—')}" if proxy_on else "未走代理"
            )
            color = G_SUCCESS
        elif err:
            state = "neterr"
            title = "🔴 网络无法连通延河课堂"
            detail = (
                f"{err}\n排查：检查网络是否能访问 https://www.yanhekt.cn"
            )
            color = G_ERROR
        elif code == 61101114 or "系统繁忙" in (probe.get("message") or ""):
            state = "waf"
            if proxy_on:
                title = "🟡 检测到 VPN/代理 + 延河 WAF 拒绝"
                detail = (
                    f"代理：{sys_proxy.get('server','')} · 来源：{sys_proxy.get('source','')}\n"
                    "可选操作：\n"
                    "  1) 把 *.yanhekt.cn 加入 Clash/V2Ray 直连规则\n"
                    "  2) 临时关闭系统代理后重试\n"
                    "  3) 在「网络」里改用「跟随系统代理」模式"
                )
                color = G_WARN
            else:
                title = "🟡 Token 已被风控或过期"
                detail = (
                    "WAF 静默拒绝（HTTP 200 + code=61101114）。\n"
                    "原因：Token 短时间被探活过多 / 已超 24 小时。\n"
                    "建议：浏览器重新登录延河 → F12 控制台脚本拿新 32 位 Token，\n"
                    "或点「自动重试」等待限流窗口过期（5-15 分钟）。"
                )
                color = G_WARN
        elif code in (401, 403) or http_status in (401, 403):
            state = "unauth"
            title = "🔴 Token 未授权 / 已失效"
            detail = "请重新拿 Token：浏览器 F12 控制台 → JSON.parse(localStorage.auth).token"
            color = G_ERROR
        else:
            state = "unknown"
            title = f"🟡 网络异常（HTTP {http_status}, code={code}）"
            detail = (probe.get("message") or "")[:200]
            color = G_WARN

        banner = ctk.CTkFrame(self, fg_color=G_PANEL, border_color=color, border_width=1)
        banner.pack(fill="x", padx=40, pady=(0, 10))
        self.net_banner = banner

        # 顶部一行：状态 + 折叠/展开
        top = ctk.CTkFrame(banner, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(
            top, text=title,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=color, anchor="w",
        ).pack(side="left", fill="x", expand=True)

        # 绿色状态紧凑模式：detail 用小字单行；失败状态展开多行 + 操作按钮
        if state == "ok":
            ctk.CTkLabel(
                banner, text=detail,
                font=ctk.CTkFont(size=10),
                text_color=G_TEXT_DIM, anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 6))
            return

        ctk.CTkLabel(
            banner, text=detail,
            font=ctk.CTkFont(size=11),
            text_color=G_TEXT, anchor="w", justify="left",
        ).pack(fill="x", padx=12, pady=(0, 6))

        # 操作按钮区
        btnrow = ctk.CTkFrame(banner, fg_color="transparent")
        btnrow.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(
            btnrow, text="网络设置...", height=28, width=110,
            fg_color=G_PANEL_HI, hover_color=G_BORDER, text_color=G_TEXT,
            command=self._open_network_dialog,
        ).pack(side="left", padx=4)
        if state == "waf" and proxy_on:
            ctk.CTkButton(
                btnrow, text="复制 Clash 直连规则", height=28, width=160,
                fg_color=G_PANEL_HI, hover_color=G_BORDER, text_color=G_TEXT,
                command=self._copy_clash_rule,
            ).pack(side="left", padx=4)
        if state == "waf":
            ctk.CTkButton(
                btnrow, text="自动重试 (5min)", height=28, width=130,
                fg_color=G_PANEL_HI, hover_color=G_SUCCESS, text_color=G_TEXT,
                command=self._start_waf_retry,
            ).pack(side="left", padx=4)
        # 浏览器登录兜底（A 失败时这条最稳）
        if state in ("waf", "unauth"):
            ctk.CTkButton(
                btnrow, text="🌐 浏览器登录", height=28, width=130,
                fg_color=G_ACCENT, hover_color=G_ACCENT_H if 'G_ACCENT_H' in globals() else G_ACCENT,
                text_color="#ffffff",
                command=self._launch_browser_login,
            ).pack(side="left", padx=4)
        ctk.CTkButton(
            btnrow, text="重新检测", height=28, width=90,
            fg_color=G_PANEL_HI, hover_color=G_BORDER, text_color=G_TEXT,
            command=self._run_network_check_async,
        ).pack(side="left", padx=4)

    # ---------- WAF 指数退避自动重试 ----------
    def _start_waf_retry(self):
        """启动一个后台线程，按 30s/60s/2min/5min/10min 重试探活，成功就提示用户。"""
        if getattr(self, "_waf_retry_thread", None) and self._waf_retry_thread.is_alive():
            messagebox.showinfo("提示", "已经在自动重试中")
            return

        delays = [30, 60, 120, 300, 600]   # 5 次重试

        def _job():
            for i, d in enumerate(delays, 1):
                # 倒计时
                remain = d
                while remain > 0:
                    self.after(0, self._update_retry_countdown, i, len(delays), remain)
                    time.sleep(1)
                    remain -= 1
                # 跑一次 probe
                probe = utils.probe_yanhe_reachable(timeout=8)
                if probe.get("ok"):
                    self.after(0, self._on_retry_success, probe)
                    return
            self.after(0, self._on_retry_exhausted)

        self._waf_retry_thread = threading.Thread(target=_job, daemon=True)
        self._waf_retry_thread.start()
        # 立即在 status_label 显示
        self.status_label.configure(
            text=f"⏳ 自动重试中... 第 1/{len(delays)} 次，{delays[0]} 秒后",
            text_color=G_WARN,
        )

    def _update_retry_countdown(self, attempt: int, total: int, remain: int):
        try:
            self.status_label.configure(
                text=f"⏳ 自动重试中... 第 {attempt}/{total} 次，{remain} 秒后",
                text_color=G_WARN,
            )
        except Exception:
            pass

    def _on_retry_success(self, probe: dict):
        self.status_label.configure(
            text="✅ Token 恢复正常，可以继续",
            text_color=G_SUCCESS,
        )
        # 触发横幅重渲染（应该变绿）
        sys_proxy = utils._detect_system_proxy()
        self._render_net_banner(sys_proxy, probe)

    def _on_retry_exhausted(self):
        self.status_label.configure(
            text="❌ 自动重试 5 次仍未恢复，请换新 Token 或检查网络",
            text_color=G_ERROR,
        )

    # ---------- 浏览器登录兜底 ----------
    def _launch_browser_login(self):
        """启动 Chrome 让用户重新登录拿新 token，跑完自动写 auth.txt 并刷新 UI。"""
        try:
            import browser_auth
        except Exception as e:
            messagebox.showerror(
                "浏览器登录不可用",
                f"无法导入 browser_auth：{e}\n\n"
                "请确保已安装 undetected-chromedriver：\n"
                "  pip install undetected-chromedriver",
            )
            return
        ok, msg = browser_auth.is_available()
        if not ok:
            messagebox.showwarning("浏览器登录不可用", msg)
            return

        # 给一个进度对话框
        dlg = ctk.CTkToplevel(self)
        dlg.title("浏览器登录")
        dlg.geometry("520x300")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()

        ctk.CTkLabel(
            dlg, text="🌐 通过浏览器登录延河课堂",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(16, 6), padx=16, anchor="w")

        ctk.CTkLabel(
            dlg, text=(
                "下载器会用 undetected_chromedriver 弹出 Chrome 窗口，\n"
                "请在窗口里完成登录（密码 / 校园卡 / 扫码 / 人脸 任意）。\n\n"
                "登录成功后浏览器会自动关闭，新 Token 会写入 auth.txt。\n"
                "全程最多 4 分钟，超时会自动放弃。"
            ),
            font=ctk.CTkFont(size=12), text_color=G_TEXT_DIM,
            justify="left",
        ).pack(padx=16, anchor="w")

        log_box = ctk.CTkTextbox(
            dlg, height=100, font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=G_BG, text_color=G_TEXT,
        )
        log_box.pack(fill="x", padx=16, pady=10)

        def _log(msg: str):
            try:
                log_box.insert("end", msg + "\n")
                log_box.see("end")
                dlg.update_idletasks()
            except Exception:
                pass

        result_holder: dict = {}

        def _job():
            try:
                from app_paths import get_app_dir
                app_dir = get_app_dir()
            except Exception:
                app_dir = None
            r = browser_auth.login_interactive(
                on_log=lambda m: self.after(0, lambda mm=m: _log(mm)),
                timeout_sec=240,
                app_dir=app_dir,
                headless=False,
            )
            result_holder["r"] = r
            self.after(0, _on_done)

        def _on_done():
            r = result_holder.get("r") or {}
            if r.get("ok"):
                token = r.get("token", "")
                # 把新 token 也填回 entry
                try:
                    self.token_entry.delete(0, "end")
                    self.token_entry.insert(0, token)
                except Exception:
                    pass
                _log(f"✅ 登录成功，token = {token[:8]}...{token[-4:]}")
                _log("正在重新探活...")
                # 强制 utils 用新 token
                try:
                    utils.read_auth()  # 让 utils.headers["Authorization"] 刷新
                    utils.reset_sessions()
                except Exception:
                    pass
                # 重新探活刷横幅
                self.after(200, self._run_network_check_async)
                self.after(800, dlg.destroy)
            else:
                _log("❌ 登录失败：" + (r.get("error") or "未知错误")[:200])
                # 留对话框给用户看错误，不自动关

        threading.Thread(target=_job, daemon=True).start()

        # 关闭按钮
        ctk.CTkButton(
            dlg, text="关闭", height=32, fg_color=G_PANEL_HI,
            hover_color=G_BORDER, text_color=G_TEXT,
            command=dlg.destroy,
        ).pack(padx=16, pady=(0, 12), anchor="w")

    def _copy_clash_rule(self):
        """把延河直连规则复制到剪贴板。Clash/V2Ray/sing-box 通用语法。"""
        rules = (
            "# 延河课堂直连（粘贴到 Clash 配置 rules: 段最前面）\n"
            "  - DOMAIN-SUFFIX,yanhekt.cn,DIRECT\n"
            "  - DOMAIN-SUFFIX,yhkt.cn,DIRECT\n"
            "# 或 V2Ray (routing.rules)：\n"
            '  {"type":"field","outboundTag":"direct","domain":["domain:yanhekt.cn","domain:yhkt.cn"]}\n'
        )
        try:
            self.clipboard_clear()
            self.clipboard_append(rules)
            self.update()
            messagebox.showinfo(
                "已复制",
                "已复制延河直连规则到剪贴板。\n\n"
                "粘贴到 Clash/V2Ray 的规则配置最前面，重启代理后再试。",
            )
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def _open_network_dialog(self):
        """简易的网络模式选择对话框。"""
        win = ctk.CTkToplevel(self)
        win.title("网络设置")
        win.geometry("520x420")
        win.transient(self.winfo_toplevel())
        win.grab_set()

        ctk.CTkLabel(
            win, text="选择网络模式",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(16, 6), padx=16, anchor="w")

        ctk.CTkLabel(
            win,
            text=(
                "下载器与延河 API 之间的连接策略。挂 VPN/代理时请优先尝试 auto/direct，\n"
                "若校外节点反而需要走代理才能访问延河，再切到 system 或自定义代理。"
            ),
            font=ctk.CTkFont(size=11),
            text_color=G_TEXT_DIM, justify="left",
        ).pack(padx=16, anchor="w")

        cur = utils._net_mode()
        var = ctk.StringVar(value=cur if not cur.startswith("proxy=") else "proxy_custom")
        proxy_url_default = (cur.split("=", 1)[1]
                             if cur.startswith("proxy=") else "http://127.0.0.1:7890")

        modes = [
            ("auto", "auto", "自动绕过代理（推荐）：requests 显式置空 proxies"),
            ("direct", "direct", "直连+IP 锁定：再绕过 DNS 劫持"),
            ("system", "system", "跟随系统/环境变量代理（trust_env=True）"),
            ("proxy_custom", "proxy_custom", "自定义代理 URL ↓"),
        ]
        for tag, value, desc in modes:
            r = ctk.CTkRadioButton(win, text=desc, variable=var, value=value)
            r.pack(anchor="w", padx=24, pady=4)

        proxy_entry = ctk.CTkEntry(win, height=32, placeholder_text="http://127.0.0.1:7890")
        proxy_entry.insert(0, proxy_url_default)
        proxy_entry.pack(fill="x", padx=40, pady=(2, 10))

        result_lbl = ctk.CTkLabel(
            win, text="", font=ctk.CTkFont(size=11), text_color=G_TEXT_DIM,
            anchor="w", justify="left", wraplength=470,
        )
        result_lbl.pack(fill="x", padx=16, pady=(0, 8))

        def apply_and_test():
            mode = var.get()
            if mode == "proxy_custom":
                url = proxy_entry.get().strip()
                if not url:
                    messagebox.showerror("错误", "请填写代理 URL")
                    return
                os.environ["YHKT_NET_MODE"] = f"proxy={url}"
            else:
                os.environ["YHKT_NET_MODE"] = mode
            utils.reset_sessions()

            result_lbl.configure(text="正在测试...", text_color=G_WARN)

            def _bg():
                probe = utils.probe_yanhe_reachable(timeout=8)
                msg = (f"模式={os.environ['YHKT_NET_MODE']}  HTTP={probe.get('http_status')}  "
                       f"code={probe.get('code')}  msg={probe.get('message') or probe.get('error')}")
                color = G_SUCCESS if probe.get("ok") else G_ERROR
                self.after(0, lambda: result_lbl.configure(text=msg, text_color=color))
                if probe.get("ok"):
                    # 同时让外面 LoginFrame 横幅刷新
                    self.after(50, self._run_network_check_async)
            threading.Thread(target=_bg, daemon=True).start()

        ctk.CTkButton(win, text="应用并测试", height=32, command=apply_and_test).pack(
            padx=16, pady=(0, 8), anchor="w"
        )
        ctk.CTkButton(win, text="关闭", height=32, fg_color=G_PANEL_HI,
                      hover_color=G_BORDER, text_color=G_TEXT,
                      command=win.destroy).pack(padx=16, pady=(0, 16), anchor="w")

    # ---------- 下载历史下拉面板 ----------
    def toggle_history_panel(self):
        """点击 ▼ 历史 按钮：展开 / 折叠面板。"""
        if self.history_panel_visible:
            self._hide_history_panel()
        else:
            self._show_history_panel()

    def _show_history_panel(self):
        items = download_history.load_history() if download_history else []
        # 重建面板
        if self.history_panel is not None:
            try:
                self.history_panel.destroy()
            except Exception:
                pass
            self.history_panel = None

        panel = ctk.CTkFrame(self.course_frame, fg_color=G_BG,
                             border_width=1, border_color=G_BORDER)
        panel.pack(fill="x", pady=(0, 10))

        if not items:
            empty = ctk.CTkLabel(
                panel,
                text="（暂无历史，成功获取课程列表后会自动记录）",
                font=ctk.CTkFont(size=12),
                text_color=G_TEXT_DIM,
            )
            empty.pack(padx=12, pady=12)
        else:
            # 滚动容器，最多显示 ~6 行
            row_h = 38
            visible_rows = min(len(items), 6)
            scroll = ctk.CTkScrollableFrame(
                panel,
                fg_color=G_BG,
                height=row_h * visible_rows,
            )
            scroll.pack(fill="x", padx=6, pady=6)
            for it in items:
                self._build_history_row(scroll, it)

            footer = ctk.CTkFrame(panel, fg_color="transparent")
            footer.pack(fill="x", padx=6, pady=(0, 6))
            ctk.CTkLabel(
                footer,
                text=f"共 {len(items)} 条记录",
                font=ctk.CTkFont(size=11),
                text_color=G_TEXT_DIM,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                footer, text="清空全部", width=90, height=28,
                font=ctk.CTkFont(size=12),
                fg_color=G_PANEL_HI, hover_color=THEME["error_h"],
                text_color=G_ERROR,
                command=self._clear_history,
            ).pack(side="right", padx=4)

        self.history_panel = panel
        self.history_panel_visible = True
        self.history_btn.configure(text="▲ 收起")

    def _hide_history_panel(self):
        if self.history_panel is not None:
            try:
                self.history_panel.destroy()
            except Exception:
                pass
            self.history_panel = None
        self.history_panel_visible = False
        self.history_btn.configure(text="▼ 历史")

    def _build_history_row(self, parent, item: dict):
        cid = item.get("course_id", "")
        cname = item.get("course_name") or "(未命名课程)"
        prof = item.get("professor") or ""
        last = (item.get("last_used") or "").replace("T", " ")[:16]
        cnt = item.get("count", 1)

        row = ctk.CTkFrame(parent, fg_color=G_PANEL, corner_radius=4)
        row.pack(fill="x", padx=2, pady=2)

        # 主体：点击整行回填课程 ID
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=8, pady=4)

        title = f"{cid}  ·  {cname}"
        if prof:
            title += f"  ·  {prof}"
        ctk.CTkLabel(
            info, text=title, font=ctk.CTkFont(size=13, weight="bold"),
            text_color=G_TEXT, anchor="w",
        ).pack(fill="x")
        meta = f"上次使用 {last}    使用次数 ×{cnt}" if last else f"使用次数 ×{cnt}"
        ctk.CTkLabel(
            info, text=meta, font=ctk.CTkFont(size=10),
            text_color=G_TEXT_DIM, anchor="w",
        ).pack(fill="x")

        def _use(_evt=None, _cid=cid):
            self._apply_history(_cid)
        for w in (row, info, *info.winfo_children()):
            try:
                w.bind("<Button-1>", _use)
            except Exception:
                pass

        ctk.CTkButton(
            row, text="使用", width=56, height=28,
            font=ctk.CTkFont(size=12),
            fg_color=G_ACCENT, hover_color=G_ACCENT_H,
            command=lambda _cid=cid: self._apply_history(_cid),
        ).pack(side="right", padx=(2, 6), pady=4)
        ctk.CTkButton(
            row, text="删除", width=56, height=28,
            font=ctk.CTkFont(size=12),
            fg_color=G_PANEL_HI, hover_color=THEME["error_h"],
            text_color=G_ERROR,
            command=lambda _cid=cid: self._remove_history(_cid),
        ).pack(side="right", padx=2, pady=4)

    def _apply_history(self, course_id: str):
        if not course_id:
            return
        self.course_entry.delete(0, "end")
        self.course_entry.insert(0, course_id)
        self._hide_history_panel()
        self.status_label.configure(
            text=f"已填入课程 {course_id}，点击「获取课程列表」继续",
            text_color=G_ACCENT,
        )

    def _remove_history(self, course_id: str):
        if not download_history:
            return
        download_history.remove_entry(course_id)
        # 刷新面板
        self._hide_history_panel()
        self._show_history_panel()

    def _clear_history(self):
        if not download_history:
            return
        if not messagebox.askyesno("清空下载历史", "确认清空全部历史记录？此操作不可恢复。"):
            return
        download_history.clear_history()
        self._hide_history_panel()
        self._show_history_panel()


class CourseSelectFrame(ctk.CTkFrame):
    """课程选择框架"""
    def __init__(self, master, course_id: str, video_list: list,
                 course_name: str, professor: str, output_dir: str, on_start_download: Callable, on_back: Callable):
        logger.info(f"CourseSelectFrame.__init__ 开始: {course_name}, {len(video_list)}个视频")
        super().__init__(master)
        self.output_dir = output_dir  # 保存输出目录
        self.course_id = course_id
        self.video_list = video_list
        self.course_name = course_name
        self.professor = professor
        self.on_start_download = on_start_download
        self.on_back = on_back
        self.selected_indices = set()
        
        # 顶部信息
        logger.info("创建顶部信息区域...")
        self.configure(fg_color=G_BG)
        self.header = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.header.pack(fill="x", padx=20, pady=10)
        
        self.back_btn = ctk.CTkButton(
            self.header, text="← 返回", width=80,
            command=on_back
        )
        self.back_btn.pack(side="left")
        
        self.course_info = ctk.CTkLabel(
            self.header, 
            text=f"📚 {course_name} - {professor}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=G_TEXT
        )
        self.course_info.pack(side="left", padx=20)
        
        logger.info("创建选项区域...")
        # 选项区域
        self.options_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.options_frame.pack(fill="x", padx=20, pady=10)
        
        # 下载类型
        self.type_label = ctk.CTkLabel(
            self.options_frame, text="下载类型:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.type_label.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        
        self.download_type = ctk.StringVar(value="screen")
        self.type_video = ctk.CTkRadioButton(
            self.options_frame, text="摄像头录像",
            variable=self.download_type, value="video"
        )
        self.type_video.grid(row=0, column=1, padx=10, pady=10)
        
        self.type_screen = ctk.CTkRadioButton(
            self.options_frame, text="电脑屏幕",
            variable=self.download_type, value="screen"
        )
        self.type_screen.grid(row=0, column=2, padx=10, pady=10)
        
        # 音频选项
        self.audio_var = ctk.BooleanVar(value=False)
        self.audio_check = ctk.CTkCheckBox(
            self.options_frame, text="同时下载蓝牙话筒音频",
            variable=self.audio_var
        )
        self.audio_check.grid(row=0, column=3, padx=20, pady=10)

        # 并发上限设置
        self.workers_label = ctk.CTkLabel(
            self.options_frame, text="并发上限:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.workers_label.grid(row=1, column=0, padx=10, pady=10, sticky="w")

        # 默认 16：docs/speed_stage4_fullseg.md 全段实测饱和点，再高无收益
        self.workers_var = tk.IntVar(value=16)
        self.workers_value = ctk.CTkLabel(
            self.options_frame, text="16",
            font=ctk.CTkFont(size=12), text_color=G_TEXT_DIM
        )

        def update_workers_label(value):
            self.workers_var.set(int(value))
            self.workers_value.configure(text=str(int(value)))

        self.workers_slider = ctk.CTkSlider(
            self.options_frame, from_=4, to=32, number_of_steps=28,
            command=update_workers_label
        )
        self.workers_slider.set(16)
        self.workers_slider.grid(row=1, column=1, columnspan=2, padx=10, pady=10, sticky="we")
        self.workers_value.grid(row=1, column=3, padx=10, pady=10, sticky="e")
        
        # 视频列表
        self.list_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.list_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        # 全选按钮
        self.select_all_btn = ctk.CTkButton(
            self.list_frame, text="全选/取消全选", 
            command=self.toggle_select_all
        )
        self.select_all_btn.pack(anchor="w", pady=5)
        
        # 创建滚动区域
        self.scrollable = ctk.CTkScrollableFrame(self.list_frame, height=300)
        self.scrollable.pack(fill="both", expand=True, pady=5)
        
        self.checkboxes = []
        for i, video in enumerate(video_list):
            var = ctk.BooleanVar(value=False)
            cb = ctk.CTkCheckBox(
                self.scrollable, 
                text=f"[{i}] {video['title']}",
                variable=var,
                command=lambda idx=i, v=var: self.on_checkbox_change(idx, v)
            )
            cb.pack(anchor="w", pady=2)
            self.checkboxes.append((cb, var))
        
        # 底部按钮
        self.bottom_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.bottom_frame.pack(fill="x", padx=20, pady=10)
        
        self.selected_label = ctk.CTkLabel(
            self.bottom_frame, text="已选择: 0 个视频",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.selected_label.pack(side="left", padx=10)
        
        self.start_btn = ctk.CTkButton(
            self.bottom_frame, text="开始下载",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=40,
            command=self.start_download
        )
        self.start_btn.pack(side="right", padx=10)
        
        logger.info("CourseSelectFrame.__init__ 完成")
    
    def on_checkbox_change(self, idx, var):
        if var.get():
            self.selected_indices.add(idx)
        else:
            self.selected_indices.discard(idx)
        self.selected_label.configure(text=f"已选择: {len(self.selected_indices)} 个视频")
    
    def toggle_select_all(self):
        if len(self.selected_indices) == len(self.video_list):
            # 取消全选
            for cb, var in self.checkboxes:
                var.set(False)
            self.selected_indices.clear()
        else:
            # 全选
            for i, (cb, var) in enumerate(self.checkboxes):
                var.set(True)
                self.selected_indices.add(i)
        self.selected_label.configure(text=f"已选择: {len(self.selected_indices)} 个视频")
    
    def start_download(self):
        if not self.selected_indices:
            messagebox.showwarning("提示", "请至少选择一个视频")
            return

        # 创建下载任务
        tasks = []
        for idx in sorted(self.selected_indices):
            task = DownloadTask(
                session_info=self.video_list[idx],
                course_name=self.course_name,
                professor=self.professor,
                download_type=self.download_type.get(),
                download_audio=self.audio_var.get(),
                output_dir=self.output_dir  # 使用自定义输出目录
            )
            # 设置并发上限（默认 32，可在选择页调整）
            task.max_workers = self.workers_var.get()
            tasks.append(task)

        self.on_start_download(tasks)


class DownloadFrame(ctk.CTkFrame):
    """下载进度框架"""
    def __init__(self, master, tasks: List[DownloadTask], on_complete: Callable):
        super().__init__(master)
        self.tasks = tasks
        self.on_complete = on_complete
        self.task_widgets = {}
        self.download_queue = queue.Queue()
        self.is_downloading = True
        self.current_task_idx = 0
        
        # 创建进度消息队列（线程安全）
        self.progress_queue = queue.Queue()
        self.poll_interval = 50  # 50ms 轮询一次
        
        # 标题
        self.configure(fg_color=G_BG)
        self.header = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.header.pack(fill="x", padx=20, pady=10)
        
        self.title_label = ctk.CTkLabel(
            self.header, text="📥 下载进度",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=G_TEXT
        )
        self.title_label.pack(side="left")
        
        self.stop_btn = ctk.CTkButton(
            self.header, text="停止下载", 
            fg_color=G_ERROR, hover_color=THEME["error_h"],
            command=self.stop_download
        )
        self.stop_btn.pack(side="right")

        # 快捷：打开输出目录
        self.open_dir_btn = ctk.CTkButton(
            self.header, text="打开输出目录",
            fg_color=G_ACCENT, hover_color=G_ACCENT_H,
            command=lambda: os.startfile(os.path.abspath("output")) if sys.platform.startswith("win") else None
        )
        self.open_dir_btn.pack(side="right", padx=10)

        # 重试失败任务
        self.retry_btn = ctk.CTkButton(
            self.header, text="重试失败任务",
            fg_color=G_WARN, hover_color=THEME["warn_h"],
            command=self.retry_failed
        )
        self.retry_btn.pack(side="right", padx=10)
        
        # 总体进度
        self.overall_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.overall_frame.pack(fill="x", padx=20, pady=10)
        
        self.overall_label = ctk.CTkLabel(
            self.overall_frame, text=f"总进度: 0/{len(tasks)}",
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.overall_label.pack(anchor="w", pady=5)
        
        self.overall_progress = ctk.CTkProgressBar(self.overall_frame, height=20)
        self.overall_progress.pack(fill="x", pady=5)
        self.overall_progress.set(0)
        
        # 任务列表
        self.tasks_scroll = ctk.CTkScrollableFrame(self, height=350, fg_color=G_PANEL)
        self.tasks_scroll.pack(fill="both", expand=True, padx=20, pady=10)
        
        for i, task in enumerate(tasks):
            self.create_task_widget(i, task)
        
        # 日志区域
        self.log_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.log_frame.pack(fill="x", padx=20, pady=10)
        
        self.log_label = ctk.CTkLabel(
            self.log_frame, text="日志:",
            font=ctk.CTkFont(size=12), text_color=G_TEXT
        )
        self.log_label.pack(anchor="w")
        
        self.log_text = ctk.CTkTextbox(self.log_frame, height=100)
        self.log_text.configure(font=("Consolas", 11))
        self.log_text.pack(fill="x", pady=5)
        
        # 启动进度队列轮询
        self.poll_progress_queue()
        
        # 开始下载
        self.start_downloads()
    
    def create_task_widget(self, idx: int, task: DownloadTask):
        frame = ctk.CTkFrame(self.tasks_scroll, fg_color=G_PANEL)
        frame.pack(fill="x", pady=5)
        
        # 任务名称
        name_label = ctk.CTkLabel(
            frame, text=task.name[:60] + ("..." if len(task.name) > 60 else ""),
            font=ctk.CTkFont(size=12),
            anchor="w",
            text_color=G_TEXT
        )
        name_label.pack(anchor="w", padx=10, pady=(5, 0))
        
        # 进度条
        progress_bar = ctk.CTkProgressBar(frame, height=15)
        progress_bar.pack(fill="x", padx=10, pady=2)
        progress_bar.set(0)
        
        # 状态信息
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(fill="x", padx=10, pady=(0, 5))
        
        status_label = ctk.CTkLabel(
            info_frame, text="等待中",
            font=ctk.CTkFont(size=11),
            text_color=G_TEXT_DIM
        )
        status_label.pack(side="left")
        
        thread_label = ctk.CTkLabel(
            info_frame, text="",
            font=ctk.CTkFont(size=11),
            text_color=G_TEXT_DIM
        )
        thread_label.pack(side="right")
        
        self.task_widgets[idx] = {
            'frame': frame,
            'name_label': name_label,
            'progress_bar': progress_bar,
            'status_label': status_label,
            'thread_label': thread_label
        }
    
    def update_task_progress(self, idx: int, downloaded: int, total: int, 
                             status: int, threads: int = 0, max_threads: int = 0):
        """更新任务进度 (从下载线程调用)"""
        if idx not in self.task_widgets:
            return
        
        widgets = self.task_widgets[idx]
        task = self.tasks[idx]
        
        # 更新进度
        if total > 0:
            progress = downloaded / total
            widgets['progress_bar'].set(progress)
        
        # 更新状态
        status_text = ""
        status_color = G_TEXT_DIM
        if status == -2:  # 获取视频信息中
            status_text = "正在获取视频信息..."
            status_color = G_ACCENT
        elif status == 0:  # 下载中
            percent = int(100 * downloaded / total) if total > 0 else 0
            status_text = f"下载中: {downloaded}/{total} ({percent}%)"
            status_color = G_WARN
        elif status == 1:  # 合并中
            status_text = "合并视频中..."
            status_color = G_ACCENT
        elif status == 2:  # 完成
            status_text = "✓ 下载完成"
            status_color = G_SUCCESS
            widgets['progress_bar'].set(1)
        elif status == -1:  # 失败
            status_text = f"✗ 失败: {task.error_msg}"
            status_color = G_ERROR
        
        widgets['status_label'].configure(text=status_text, text_color=status_color)
        
        # 更新线程信息 - 总是显示
        if max_threads > 0:
            widgets['thread_label'].configure(text=f"线程: {threads}/{max_threads}")
    
    def poll_progress_queue(self):
        """轮询进度队列，从后台线程获取更新并安全地更新GUI"""
        try:
            # 一次处理最多20条消息，避免阻塞
            for _ in range(20):
                try:
                    msg = self.progress_queue.get_nowait()
                except queue.Empty:
                    break
                
                msg_type = msg.get('type')
                if msg_type == 'progress':
                    self.update_task_progress(
                        msg['idx'], msg['downloaded'], msg['total'],
                        msg['status'], msg['threads'], msg['max_threads']
                    )
                elif msg_type == 'log':
                    self.log(msg['message'])
                elif msg_type == 'overall':
                    self.overall_label.configure(text=msg['text'])
                    self.overall_progress.set(msg['value'])
        except Exception as e:
            logger.error(f"poll_progress_queue 错误: {e}")
        
        # 继续轮询（如果还在下载）
        if self.is_downloading:
            self.after(self.poll_interval, self.poll_progress_queue)
    
    def log(self, message: str):
        """添加日志"""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
    
    def start_downloads(self):
        """开始所有下载任务"""
        def download_worker():
            completed = 0
            for idx, task in enumerate(self.tasks):
                if not self.is_downloading:
                    break
                
                self.current_task_idx = idx
                self.after(0, lambda i=idx: self.update_task_progress(i, 0, 0, 0))
                self.after(0, lambda t=task: self.log(f"▶ 开始下载: {t.name}"))
                logger.info(f"开始下载任务 {idx+1}/{len(self.tasks)}: {task.name}")
                
                try:
                    # 获取视频URL
                    videos = task.session_info.get('videos', [])
                    logger.debug(f"视频信息: {videos}")
                    if not videos:
                        raise Exception("该课程没有视频信息，请确认课程有录像")
                    
                    video_info = videos[0]
                    logger.debug(f"video_info keys: {list(video_info.keys())}")
                    
                    if task.download_type == 'screen':
                        url = video_info.get('vga', '')
                        path = f"{task.output_dir}/{task.course_name}-screen"
                        if not url:
                            # 尝试备用字段
                            url = video_info.get('screen', '') or video_info.get('desktop', '')
                    else:
                        url = video_info.get('main', '')
                        path = f"{task.output_dir}/{task.course_name}-video"
                        if not url:
                            # 尝试备用字段
                            url = video_info.get('camera', '') or video_info.get('video', '')
                    
                    logger.info(f"视频URL: {url[:80] if url else '(空)'}...")
                    logger.info(f"保存路径: {path}")
                    
                    if not url:
                        # 打印可用的字段供调试
                        available_keys = list(video_info.keys())
                        self.after(0, lambda keys=available_keys: self.log(f"⚠ 可用字段: {keys}"))
                        raise Exception(f"未找到视频URL (类型: {task.download_type})，视频可能未生成")
                    
                    # 创建输出目录 (使用绝对路径)
                    full_path = os.path.join(utils.get_app_path(), path)
                    os.makedirs(full_path, exist_ok=True)
                    logger.info(f"创建输出目录: {full_path}")
                    
                    # 用于日志节流的变量
                    last_log_downloaded = [0]  # 使用列表以便在闭包中修改
                    
                    # 进度回调 - 使用队列进行线程安全通信
                    current_idx = idx
                    task_name = task.name
                    progress_queue = self.progress_queue  # 捕获队列引用
                    
                    def progress_callback(downloaded, total, status, threads=0, max_threads=0, 
                                         idx=current_idx, name=task_name, q=progress_queue):
                        # 发送进度更新到队列
                        q.put({
                            'type': 'progress',
                            'idx': idx,
                            'downloaded': downloaded,
                            'total': total,
                            'status': status,
                            'threads': threads,
                            'max_threads': max_threads
                        })
                        
                        # 在日志中显示进度（每5%输出一次）
                        if total > 0 and status == 0:
                            step = max(1, total // 20)  # 5% 步进
                            if downloaded >= last_log_downloaded[0] + step or downloaded == total:
                                last_log_downloaded[0] = downloaded
                                percent = int(100 * downloaded / total)
                                q.put({
                                    'type': 'log',
                                    'message': f"📥 {name[:25]}... {downloaded}/{total} ({percent}%) 🧵{threads}/{max_threads}"
                                })
                        elif status == 1:
                            q.put({'type': 'log', 'message': f"🔄 正在合并: {name[:35]}..."})
                        elif status == 2:
                            q.put({'type': 'log', 'message': f"✅ 完成: {name}"})
                    
                    # 开始下载
                    m3u8dl.M3u8Download(
                        url=url,
                        workDir=path,
                        name=task.name,
                        max_workers=task.max_workers,
                        progress_callback=progress_callback,
                        gui_mode=True  # GUI模式，禁用强制退出
                    )
                    
                    # 下载音频
                    if task.download_audio and task.session_info.get('video_ids'):
                        video_id = task.session_info['video_ids'][0]
                        audio_url = utils.get_audio_url(video_id)
                        if audio_url:
                            self.after(0, lambda: self.log(f"下载音频: {task.name}"))
                            utils.download_audio(audio_url, path, task.name)
                    
                    completed += 1
                    task.status = "完成"
                    self.after(0, lambda c=completed: self.overall_label.configure(
                        text=f"总进度: {c}/{len(self.tasks)}"
                    ))
                    self.after(0, lambda c=completed: self.overall_progress.set(
                        c / len(self.tasks)
                    ))
                    
                except Exception as e:
                    import traceback
                    error_full = str(e)
                    error_traceback = traceback.format_exc()
                    task.status = "失败"
                    task.error_msg = error_full[:50]
                    
                    # 在日志中输出完整错误信息
                    logger.error(f"下载失败: {task.name}")
                    logger.error(f"错误详情: {error_full}")
                    logger.error(f"堆栈跟踪:\n{error_traceback}")
                    
                    self.after(0, lambda i=idx, t=task: self.update_task_progress(
                        i, 0, 0, -1
                    ))
                    self.after(0, lambda t=task, e=error_full: self.log(f"❌ 失败: {t.name}\n   原因: {e[:100]}"))
            
            # 完成
            if self.is_downloading:
                self.after(0, lambda: self.log("所有任务完成!"))
                self.after(0, lambda: self.stop_btn.configure(
                    text="返回", fg_color="green", hover_color="darkgreen",
                    command=self.on_complete
                ))
        
        threading.Thread(target=download_worker, daemon=True).start()

    def retry_failed(self):
        failed = [t for t in self.tasks if t.status == "失败"]
        if not failed:
            self.log("没有失败的任务需要重试")
            return
        self.log(f"重试失败任务: {len(failed)} 个")

        def _redownload():
            for idx, task in enumerate(self.tasks):
                if task.status != "失败":
                    continue
                if not self.is_downloading:
                    break
                # 重置状态
                task.status = "等待中"
                task.error_msg = ""
                self.after(0, lambda i=idx: self.update_task_progress(i, 0, 0, 0))
                self.after(0, lambda t=task: self.log(f"开始重试: {t.name}"))
                try:
                    videos = task.session_info.get('videos', [])
                    if not videos:
                        raise Exception("该课程没有视频信息")
                    video_info = videos[0]
                    if task.download_type == 'screen':
                        url = video_info.get('vga', '')
                        path = f"{task.output_dir}/{task.course_name}-screen"
                        if not url:
                            url = video_info.get('screen', '') or video_info.get('desktop', '')
                    else:
                        url = video_info.get('main', '')
                        path = f"{task.output_dir}/{task.course_name}-video"
                        if not url:
                            url = video_info.get('camera', '') or video_info.get('video', '')
                    if not url:
                        available_keys = list(video_info.keys())
                        self.after(0, lambda keys=available_keys: self.log(f"可用字段: {keys}"))
                        raise Exception(f"未找到视频URL (类型: {task.download_type})")
                    os.makedirs(path, exist_ok=True)
                    
                    # 用于日志节流的变量
                    last_log_downloaded = [0]
                    
                    current_idx = idx
                    task_name = task.name
                    progress_queue = self.progress_queue  # 捕获队列引用
                    
                    def progress_callback(downloaded, total, status, threads=0, max_threads=0, 
                                         idx=current_idx, name=task_name, q=progress_queue):
                        # 发送进度更新到队列
                        q.put({
                            'type': 'progress',
                            'idx': idx,
                            'downloaded': downloaded,
                            'total': total,
                            'status': status,
                            'threads': threads,
                            'max_threads': max_threads
                        })
                        
                        # 在日志中显示进度（每5%输出一次）
                        if total > 0 and status == 0:
                            step = max(1, total // 20)  # 5% 步进
                            if downloaded >= last_log_downloaded[0] + step or downloaded == total:
                                last_log_downloaded[0] = downloaded
                                percent = int(100 * downloaded / total)
                                q.put({
                                    'type': 'log',
                                    'message': f"📥 {name[:25]}... {downloaded}/{total} ({percent}%) 🧵{threads}/{max_threads}"
                                })
                        elif status == 1:
                            q.put({'type': 'log', 'message': f"🔄 正在合并: {name[:35]}..."})
                        elif status == 2:
                            q.put({'type': 'log', 'message': f"✅ 完成: {name}"})
                    m3u8dl.M3u8Download(
                        url=url,
                        workDir=path,
                        name=task.name,
                        max_workers=task.max_workers,
                        progress_callback=progress_callback,
                        gui_mode=True
                    )
                    task.status = "完成"
                except Exception as e:
                    task.status = "失败"
                    task.error_msg = str(e)[:30]
                    self.after(0, lambda i=idx, t=task: self.update_task_progress(i, 0, 0, -1))
                    self.after(0, lambda t=task, e=e: self.log(f"失败: {t.name} - {e}"))
            self.after(0, lambda: self.log("重试任务完成!"))

        threading.Thread(target=_redownload, daemon=True).start()
    
    def stop_download(self):
        self.is_downloading = False
        self.log("正在停止下载...")
        messagebox.showinfo("提示", "下载已停止")
        self.on_complete()


class PostProcessFrame(ctk.CTkFrame):
    """后处理框架 - 课件提取 + 音频转录"""

    def __init__(self, master, on_back: callable):
        super().__init__(master)
        self.on_back = on_back
        self.is_processing = False
        self.configure(fg_color=G_BG)

        # 顶部标题栏
        self.header = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.header.pack(fill="x", padx=20, pady=10)

        self.back_btn = ctk.CTkButton(
            self.header, text="← 返回", width=80, command=on_back
        )
        self.back_btn.pack(side="left")

        self.title_label = ctk.CTkLabel(
            self.header, text="🔧 后处理工具 - 课件提取 & 音频转录",
            font=ctk.CTkFont(size=18, weight="bold"), text_color=G_TEXT,
        )
        self.title_label.pack(side="left", padx=20)

        # GPU 检测提示横幅（在创建参数控件前先决定默认设备）
        gpu_ok, gpu_msg = self._detect_gpu()
        banner = ctk.CTkFrame(self, fg_color=G_ACCENT if gpu_ok else G_WARN)
        banner.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(
            banner,
            text=gpu_msg,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#ffffff",
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=12, pady=8)
        self._gpu_ok = gpu_ok

        # 目录选择
        dir_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        dir_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            dir_frame, text="视频目录:", font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).pack(anchor="w", pady=(10, 5))

        dir_row = ctk.CTkFrame(dir_frame, fg_color="transparent")
        dir_row.pack(fill="x")
        self.dir_entry = ctk.CTkEntry(dir_row, height=40, font=ctk.CTkFont(size=14))
        self.dir_entry.insert(0, os.path.join(get_app_dir(), "output"))
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(
            dir_row, text="浏览...", width=80, height=40,
            command=self._browse_dir,
        ).pack(side="right")

        # 功能选择
        func_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        func_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            func_frame, text="处理功能:", font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        self.do_slides_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            func_frame, text="课件/PPT 提取 (FFmpeg GPU)", variable=self.do_slides_var,
        ).grid(row=0, column=1, padx=15, pady=10)

        self.do_transcribe_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            func_frame, text="音频转录 (Whisper GPU)", variable=self.do_transcribe_var,
        ).grid(row=0, column=2, padx=15, pady=10)

        # 转录参数
        param_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        param_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            param_frame, text="Whisper 模型:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        self.model_var = ctk.StringVar(value="large-v3")
        self.model_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.model_var,
            values=["tiny", "base", "small", "medium", "large-v3"],
            width=150,
        )
        self.model_menu.grid(row=0, column=1, padx=10, pady=10)

        ctk.CTkLabel(
            param_frame, text="语言:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).grid(row=0, column=2, padx=10, pady=10, sticky="w")

        self.lang_var = ctk.StringVar(value="zh")
        self.lang_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.lang_var,
            values=["zh", "en", "ja", "auto"],
            width=100,
        )
        self.lang_menu.grid(row=0, column=3, padx=10, pady=10)

        ctk.CTkLabel(
            param_frame, text="设备:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).grid(row=1, column=0, padx=10, pady=10, sticky="w")

        self.device_var = ctk.StringVar(value="auto" if getattr(self, "_gpu_ok", False) else "cpu")
        self.device_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.device_var,
            values=["auto", "cuda", "cpu"],
            width=150,
        )
        self.device_menu.grid(row=1, column=1, padx=10, pady=10)

        ctk.CTkLabel(
            param_frame, text="精度:",
            font=ctk.CTkFont(size=14), text_color=G_TEXT,
        ).grid(row=1, column=2, padx=10, pady=10, sticky="w")

        self.compute_var = ctk.StringVar(value="float16" if getattr(self, "_gpu_ok", False) else "int8")
        self.compute_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.compute_var,
            values=["float16", "int8", "float32"],
            width=100,
        )
        self.compute_menu.grid(row=1, column=3, padx=10, pady=10)

        # 开始按钮
        self.start_btn = ctk.CTkButton(
            self, text="开始处理", height=45,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=G_ACCENT, hover_color=G_ACCENT_H,
            command=self._start_process,
        )
        self.start_btn.pack(pady=10, padx=40, fill="x")

        # 进度条
        progress_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        progress_frame.pack(fill="x", padx=20, pady=5)
        self.progress_label = ctk.CTkLabel(
            progress_frame, text="就绪", font=ctk.CTkFont(size=12), text_color=G_TEXT,
        )
        self.progress_label.pack(anchor="w", pady=(5, 2))
        self.progress_bar = ctk.CTkProgressBar(progress_frame, height=18)
        self.progress_bar.pack(fill="x", pady=(0, 5))
        self.progress_bar.set(0)

        # 日志
        log_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        log_frame.pack(fill="both", expand=True, padx=20, pady=10)
        ctk.CTkLabel(
            log_frame, text="日志:", font=ctk.CTkFont(size=12), text_color=G_TEXT,
        ).pack(anchor="w")
        self.log_text = ctk.CTkTextbox(log_frame, height=200, font=("Consolas", 11))
        self.log_text.pack(fill="both", expand=True, pady=5)

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择视频目录", initialdir=self.dir_entry.get())
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def _detect_gpu(self):
        """返回 (是否可用 GPU, 用户可读提示)。检测顺序: torch.cuda → nvidia-smi。"""
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                name = torch.cuda.get_device_name(0)
                return True, f"✅ 检测到 GPU: {name}  —  Whisper 将以 float16 全速运行（约 30-50x 实时）"
        except Exception:
            pass
        try:
            import subprocess as _sp
            r = _sp.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return True, f"✅ 检测到 NVIDIA GPU：{r.stdout.strip().splitlines()[0]}  —  建议先安装 CUDA 12.x + cuDNN 9 以启用加速"
        except Exception:
            pass
        return False, (
            "⚠️  未检测到 GPU/CUDA — 将使用 CPU 转录（large-v3 可能很慢，建议改用 medium 或 small 模型）。\n"
            "    GPU 用户：请安装 NVIDIA 驱动 + CUDA 12.x + cuDNN 9，并在此处选择 cuda/float16。"
        )

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")

    def _start_process(self):
        if self.is_processing:
            return
        input_dir = self.dir_entry.get().strip()
        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showerror("错误", "请选择有效的视频目录")
            return
        if not self.do_slides_var.get() and not self.do_transcribe_var.get():
            messagebox.showwarning("提示", "请至少选择一项处理功能")
            return

        self.is_processing = True
        self.start_btn.configure(state="disabled", text="处理中...")
        self.back_btn.configure(state="disabled")
        self._log("开始处理...")

        def worker():
            try:
                import batch_process

                def gui_progress(current, total, msg):
                    self.after(0, lambda: self.progress_bar.set(current / total if total > 0 else 0))
                    self.after(0, lambda m=msg: self.progress_label.configure(text=m))
                    self.after(0, lambda m=msg: self._log(m))

                results = batch_process.batch_process_all(
                    input_dir,
                    do_slides=self.do_slides_var.get(),
                    do_transcribe=self.do_transcribe_var.get(),
                    model_size=self.model_var.get(),
                    device=self.device_var.get(),
                    compute_type=self.compute_var.get(),
                    language=self.lang_var.get(),
                    progress_callback=gui_progress,
                )

                # 汇总
                slide_results = results.get("slides", [])
                transcript_results = results.get("transcripts", [])
                slide_ok = sum(1 for r in slide_results if "成功" in r.get("status", ""))
                trans_ok = sum(1 for r in transcript_results if "成功" in r.get("status", ""))

                summary = f"处理完成! 课件: {slide_ok}/{len(slide_results)}, 转录: {trans_ok}/{len(transcript_results)}"
                self.after(0, lambda: self._log(f"✅ {summary}"))
                self.after(0, lambda: self.progress_label.configure(text=summary))
                self.after(0, lambda: self.progress_bar.set(1))

            except ImportError as e:
                self.after(0, lambda e=e: self._log(f"❌ 模块导入失败: {e}"))
                self.after(0, lambda e=e: messagebox.showerror(
                    "依赖缺失",
                    f"缺少必要的库: {e}\n\n"
                    "请在 GPU 环境中安装:\n"
                    "pip install faster-whisper tqdm imagehash python-pptx"
                ))
            except Exception as e:
                self.after(0, lambda e=e: self._log(f"❌ 处理失败: {e}"))
                self.after(0, lambda e=e: messagebox.showerror("处理失败", str(e)[:300]))
            finally:
                self.after(0, lambda: self.start_btn.configure(state="normal", text="开始处理"))
                self.after(0, lambda: self.back_btn.configure(state="normal"))
                self.is_processing = False

        threading.Thread(target=worker, daemon=True).start()


class YanheDownloaderApp(ctk.CTk):
    """主应用程序"""
    def __init__(self):
        super().__init__()
        
        self.title(f"延河课堂下载器 - {edition_label()}")
        self.geometry("900x700")
        self.minsize(800, 600)
        
        # 设置窗口图标
        try:
            icon_path = get_resource_path("yhkt.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
                logger.info(f"窗口图标设置成功: {icon_path}")
            else:
                logger.warning(f"图标文件不存在: {icon_path}")
        except Exception as e:
            logger.warning(f"设置窗口图标失败: {e}")

        self.configure(fg_color=G_BG)
        
        # 居中显示
        self.center_window()
        
        # 创建主容器
        self.container = ctk.CTkFrame(self)
        self.container.pack(fill="both", expand=True)
        
        # 显示登录界面
        self.show_login()
    
    def center_window(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"+{x}+{y}")
    
    def clear_container(self):
        for widget in self.container.winfo_children():
            widget.destroy()
    
    def show_login(self):
        logger.info("显示登录界面")
        self.clear_container()
        self.login_frame = LoginFrame(
            self.container, 
            on_login_success=self.show_course_select,
            on_post_process=self.show_post_process
        )
        self.login_frame.pack(fill="both", expand=True)
        logger.info("登录界面已显示")
    
    def show_course_select(self, course_id, video_list, course_name, professor):
        output_dir = getattr(self, 'output_directory', 'output')
        logger.info(f"显示课程选择界面: {course_name}, {len(video_list)}个视频, 输出目录={output_dir}")
        try:
            self.clear_container()
            logger.info("容器已清空，创建 CourseSelectFrame...")
            self.select_frame = CourseSelectFrame(
                self.container,
                course_id=course_id,
                video_list=video_list,
                course_name=course_name,
                professor=professor,
                output_dir=output_dir,
                on_start_download=self.show_download,
                on_back=self.show_login
            )
            logger.info("CourseSelectFrame 创建完成，开始 pack...")
            self.select_frame.pack(fill="both", expand=True)
            logger.info("课程选择界面已显示")
            # 强制更新界面
            self.update_idletasks()
            logger.info("界面已强制更新")
        except Exception as e:
            logger.error(f"show_course_select 异常: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            messagebox.showerror("界面错误", f"无法显示课程选择界面:\n{e}")
    
    def show_download(self, tasks: List[DownloadTask]):
        logger.info(f"显示下载界面: {len(tasks)}个任务")
        self.clear_container()
        self.download_frame = DownloadFrame(
            self.container,
            tasks=tasks,
            on_complete=self.show_login
        )
        self.download_frame.pack(fill="both", expand=True)

    def show_post_process(self):
        logger.info("显示后处理工具界面")
        self.clear_container()
        self.postprocess_frame = PostProcessFrame(
            self.container,
            on_back=self.show_login
        )
        self.postprocess_frame.pack(fill="both", expand=True)


def main():
    logger.info("main() 开始执行")
    # 确保output目录存在
    os.makedirs("output", exist_ok=True)
    
    try:
        logger.info("创建 YanheDownloaderApp")
        app = YanheDownloaderApp()
        logger.info("启动 mainloop")
        app.mainloop()
    except Exception as e:
        logger.error(f"主程序异常: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
