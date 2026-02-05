"""
延河课堂下载器 - 现代化GUI界面
基于 AuYang261/BIT_yanhe_download 改进
新增：多线程自适应下载、Watchdog监控、关键帧提取等功能
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


def get_resource_path(relative_path):
    """获取资源文件的绝对路径，支持打包后的环境"""
    try:
        # PyInstaller 创建临时文件夹，将路径存储在 _MEIPASS
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def get_app_dir():
    """获取应用程序所在目录（用于存储配置文件等）"""
    if getattr(sys, 'frozen', False):
        # 打包后的 exe，使用 exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发环境，使用当前脚本目录
        return os.path.dirname(os.path.abspath(__file__))


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

# 设置外观（GitHub Dark / Grok 风格）
ctk.set_appearance_mode("dark")
# 使用内置主题，并在组件上覆盖关键色彩以呈现极客黑风格
# 参考配色：背景 #0d1117，面板 #161b22，文字 #c9d1d9，强调 #58a6ff，成功 #3fb950，警告 #f2cc60，错误 #f85149
ctk.set_default_color_theme("blue")
logger.info("CustomTkinter 主题设置完成")

# 统一配色常量
G_BG = "#0d1117"
G_PANEL = "#161b22"
G_TEXT = "#c9d1d9"
G_ACCENT = "#58a6ff"
G_SUCCESS = "#3fb950"
G_WARN = "#f2cc60"
G_ERROR = "#f85149"


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
        self.max_workers = 64  # 默认并发上限，与CLI保持一致


class LoginFrame(ctk.CTkFrame):
    """登录框架"""
    def __init__(self, master, on_login_success: Callable):
        super().__init__(master)
        self.on_login_success = on_login_success
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
            text_color="#8b949e"
        )
        self.subtitle.pack(pady=(0, 30))
        
        # 课程ID输入
        self.course_frame = ctk.CTkFrame(self, fg_color=G_PANEL)
        self.course_frame.pack(fill="x", padx=40, pady=10)
        
        self.course_label = ctk.CTkLabel(
            self.course_frame, text="课程ID:", 
            font=ctk.CTkFont(size=14), text_color=G_TEXT
        )
        self.course_label.pack(anchor="w", pady=(10, 5))
        
        self.course_entry = ctk.CTkEntry(
            self.course_frame, 
            placeholder_text="输入5位课程编号，如 40524",
            height=40,
            font=ctk.CTkFont(size=14)
        )
        self.course_entry.pack(fill="x", pady=(0, 10))

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
        code_frame = ctk.CTkFrame(self.token_frame, fg_color="#0d1117")
        code_frame.pack(fill="x", pady=(5, 10))

        # 代码显示
        self.code_label = ctk.CTkLabel(
            code_frame,
            text="javascript:alert(JSON.parse(localStorage.auth).token)",
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color="#58a6ff",
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
            fg_color="#238636",
            hover_color="#2ea043",
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
            text_color="#8b949e",
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
            hover_color="#3B83CE",
            command=self.fetch_course
        )
        self.fetch_btn.pack(pady=30, padx=40, fill="x")
        
        # 状态标签
        self.status_label = ctk.CTkLabel(
            self, text="", 
            font=ctk.CTkFont(size=12),
            text_color=G_WARN
        )
        self.status_label.pack(pady=10)
        logger.info("LoginFrame 初始化完成")
    
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
                            "检测到您可能开启了VPN或代理，这会导致连接延河课堂失败。\n\n"
                            "解决方案：\n"
                            "1. 关闭VPN或代理软件\n"
                            "2. 或者在代理设置中将 yanhekt.cn 加入白名单\n"
                            "3. 确保能正常访问 https://www.yanhekt.cn"
                        ))
                    elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                        self.after(0, lambda: self.show_error(
                            "连接超时",
                            "无法连接到延河课堂服务器。\n\n"
                            "可能原因：\n"
                            "1. 网络连接不稳定\n"
                            "2. 延河课堂服务器暂时不可用\n"
                            "3. 开启了VPN/代理导致连接问题\n\n"
                            "建议：关闭VPN后重试"
                        ))
                    elif "SSLError" in error_msg or "certificate" in error_msg.lower():
                        self.after(0, lambda: self.show_error(
                            "SSL证书错误",
                            "HTTPS连接失败，可能是代理软件拦截了请求。\n\n"
                            "解决方案：关闭VPN或代理软件后重试"
                        ))
                    else:
                        self.after(0, lambda e=error_msg: self.show_error(
                            "网络连接错误",
                            f"无法连接到延河课堂：\n{e[:200]}\n\n"
                            "建议：检查网络连接，关闭VPN后重试"
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
                        "请关闭VPN或代理软件后重试。\n延河课堂需要直连访问。"
                    ))
                elif "没有视频信息" in error_msg or "课程ID" in error_msg:
                    self.after(0, lambda e=error_msg: self.show_error(
                        "课程不存在", e
                    ))
                else:
                    self.after(0, lambda e=error_msg: self.show_error(
                        "获取课程失败", 
                        f"{e[:150]}\n\n常见解决方案：\n1. 检查课程ID是否正确\n2. 关闭VPN/代理\n3. 重新获取Token"
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
        self.copy_btn.configure(text="已复制!", fg_color="#3fb950", hover_color="#3fb950")
        self.after(2000, lambda: self.copy_btn.configure(
            text="复制代码", fg_color="#238636", hover_color="#2ea043"
        ))

    def show_error(self, title: str, message: str):
        """显示详细错误信息"""
        self.status_label.configure(text=f"❌ {title}", text_color=G_ERROR)
        messagebox.showerror(title, message)


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

        self.workers_var = tk.IntVar(value=64)  # 与CLI保持一致
        self.workers_value = ctk.CTkLabel(
            self.options_frame, text="32",
            font=ctk.CTkFont(size=12), text_color="#8b949e"
        )

        def update_workers_label(value):
            self.workers_var.set(int(value))
            self.workers_value.configure(text=str(int(value)))

        self.workers_slider = ctk.CTkSlider(
            self.options_frame, from_=4, to=64, number_of_steps=60,
            command=update_workers_label
        )
        self.workers_slider.set(32)
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
            fg_color=G_ERROR, hover_color="#a52b2b",
            command=self.stop_download
        )
        self.stop_btn.pack(side="right")

        # 快捷：打开输出目录
        self.open_dir_btn = ctk.CTkButton(
            self.header, text="打开输出目录",
            fg_color=G_ACCENT, hover_color="#3B83CE",
            command=lambda: os.startfile(os.path.abspath("output")) if sys.platform.startswith("win") else None
        )
        self.open_dir_btn.pack(side="right", padx=10)

        # 重试失败任务
        self.retry_btn = ctk.CTkButton(
            self.header, text="重试失败任务",
            fg_color=G_WARN, hover_color="#b49b3a",
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
            text_color="#8b949e"
        )
        status_label.pack(side="left")
        
        thread_label = ctk.CTkLabel(
            info_frame, text="",
            font=ctk.CTkFont(size=11),
            text_color="#8b949e"
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
        status_color = "#8b949e"
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


class YanheDownloaderApp(ctk.CTk):
    """主应用程序"""
    def __init__(self):
        super().__init__()
        
        self.title("延河课堂下载器 - Enhanced Edition")
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
            on_login_success=self.show_course_select
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
