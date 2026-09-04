"""
限流熔断本地压测（不打真实服务器）。

用本地 mock 模拟延河 WAF 的已知行为（见 utils.py WafBreaker 注释）：
  - API 域 cbiz：窗口内请求超过阈值 → HTTP 200 + code=61101114 "系统繁忙"，持续惩罚期
  - CDN 域 cvideo：被限时分片返回 403

验证三件事：
  1. WafBreaker 单元行为（触发/等待/升级/恢复）
  2. api_get/direct_get 的接线：被限时自动触发熔断并在冷却期阻塞
  3. 价值量化：被限期间"旧客户端（无熔断）" vs "新客户端（熔断）"发出的请求量对比

运行：python scripts/test_waf_breaker.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils  # noqa: E402


# ---------------- Mock 基础设施 ----------------

class FakeResponse:
    def __init__(self, status_code, json_body=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}
        self.request = type("R", (), {"path_url": "/x"})()
        self.url = "https://cbiz.yanhekt.cn/x"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=8192):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ApiMock:
    """模拟 cbiz WAF：window 秒内超过 threshold 个请求 → penalty 秒内全部回 61101114。"""

    def __init__(self, threshold=5, window=2.0, penalty=6.0):
        self.threshold, self.window, self.penalty = threshold, window, penalty
        self.times = []
        self.blocked_until = 0.0
        self.total_requests = 0
        self.requests_during_block = 0
        self.lock = threading.Lock()

    def handle(self):
        with self.lock:
            now = time.time()
            self.total_requests += 1
            if now < self.blocked_until:
                self.requests_during_block += 1
                return FakeResponse(200, {"code": 61101114, "message": "系统繁忙，请稍后再试", "data": None})
            self.times = [t for t in self.times if now - t < self.window] + [now]
            if len(self.times) > self.threshold:
                self.blocked_until = now + self.penalty
                return FakeResponse(200, {"code": 61101114, "message": "系统繁忙，请稍后再试", "data": None})
            return FakeResponse(200, {"code": 0, "message": "", "data": {"token": "ok"}})

    def blocked(self):
        return time.time() < self.blocked_until


class CdnMock:
    """模拟 cvideo CDN：被限期间分片 403。"""

    def __init__(self):
        self.blocked_until = 0.0
        self.hits_403 = 0

    def handle(self):
        if time.time() < self.blocked_until:
            self.hits_403 += 1
            return FakeResponse(403, content=b"", headers={"Content-Type": "text/plain"})
        return FakeResponse(200, content=b"\x00" * 1024, headers={"Content-Type": "video/mp2t"})


class FakeSession:
    def __init__(self):
        self.api = None
        self.cdn = None

    def get(self, url, **kwargs):
        if "cvideo.yanhekt.cn" in url:
            return self.cdn.handle()
        return self.api.handle()


# ---------------- 测试 ----------------

def main():
    api_mock = ApiMock(threshold=5, window=2.0, penalty=4.0)
    cdn_mock = CdnMock()
    fake = FakeSession()
    fake.api, fake.cdn = api_mock, cdn_mock

    # 替换 utils 的会话工厂与全局参数（测试用短冷却）
    real_make_session = utils._make_session
    real_api_breaker = utils.waf_breaker
    real_cdn_breaker = utils.cdn_breaker
    real_api_interval = utils.API_MIN_INTERVAL
    utils._make_session = lambda mode: fake
    utils.waf_breaker = utils.WafBreaker(base_cooldown=1.0, max_cooldown=4.0)
    utils.cdn_breaker = utils.WafBreaker(base_cooldown=1.0, max_cooldown=4.0)
    utils.API_MIN_INTERVAL = 0.02

    passed, failed = [], []

    def check(name, cond, detail=""):
        (passed if cond else failed).append(name)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    try:
        print("\n== 1. API 熔断触发与等待 ==")
        codes = [utils.api_get("https://cbiz.yanhekt.cn/v1/auth/video/token?id=0",
                               headers=utils.headers).json()["code"] for _ in range(5)]
        check("前 5 个请求正常（code=0）", all(c == 0 for c in codes), f"{codes}")
        r = utils.api_get("https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=utils.headers)  # 第6个 → WAF
        check("第 6 个请求触发 WAF（code=61101114）", r.json()["code"] == 61101114)
        check("熔断已触发（冷却中）", utils.waf_breaker.cooldown_remaining() > 0,
              f"剩余 {utils.waf_breaker.cooldown_remaining():.2f}s")
        t1 = time.time()
        utils.api_get("https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=utils.headers)  # 应等待冷却结束
        elapsed = time.time() - t1
        check("冷却期内请求被阻塞至窗口结束", elapsed >= 0.8, f"阻塞 {elapsed:.2f}s")
        # 冷却短于对方惩罚期时，客户端会再次被拒→熔断升级→更长冷却，
        # 最终在对方惩罚期结束后收敛恢复（生产行为：自动退避直至成功）
        recovered, deadline = False, time.time() + 15
        while time.time() < deadline:
            rr = utils.api_get("https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=utils.headers)
            if rr.json()["code"] == 0:
                recovered = True
                break
        check("熔断退避后最终自动恢复（code=0）", recovered)

        print("\n== 2. 级别升级与恢复 ==")
        utils.waf_breaker.trip("测试1")
        utils.waf_breaker.trip("测试2")
        cd = utils.waf_breaker.trip("测试3")
        check("连续触发冷却翻倍（1→2→4s 封顶）", 3.5 <= cd <= 4.0, f"第3次冷却 {cd:.1f}s")
        utils.waf_breaker.report_ok()
        cd2 = utils.waf_breaker.trip("恢复后")
        check("成功一次后级别归零", abs(cd2 - 1.0) < 0.05, f"重置后冷却 {cd2:.1f}s")

        print("\n== 3. CDN 403 熔断接线 ==")
        cdn_mock.blocked_until = time.time() + 2.0  # 模拟 CDN 进入限流
        r = utils.direct_get("https://cvideo.yanhekt.cn/seg_001.ts", stream=True)
        check("CDN 被限时返回 403", r.status_code == 403)
        check("CDN 熔断触发", utils.cdn_breaker.cooldown_remaining() > 0)

        print("\n== 4. API 全局限速（pacing） ==")
        utils.API_MIN_INTERVAL = 0.05
        t2 = time.time()
        for _ in range(5):
            utils.direct_get("https://cbiz.yanhekt.cn/v2/course/session/list?course_id=1", headers=utils.headers)
        el = time.time() - t2
        check("5 个连续 API 调用间隔 >= 0.05s×4", el >= 0.18, f"耗时 {el:.2f}s")

        print("\n== 5. 压测对比：被限期间 16 线程硬敲 vs 熔断退避 ==")
        # 场景 A：旧客户端（无熔断）——直接打 mock，持续 3 秒
        api_old = ApiMock(threshold=5, window=2.0, penalty=4.0)
        old_sess = FakeSession(); old_sess.api = api_old
        stop = threading.Event()

        def old_hammer(sess, mock):
            while not stop.is_set():
                try:
                    sess.get("https://cbiz.yanhekt.cn/v1/x")
                except Exception:
                    pass
                time.sleep(0.01)

        threads = [threading.Thread(target=old_hammer, args=(old_sess, api_old), daemon=True) for _ in range(16)]
        for t in threads: t.start()
        time.sleep(3.0)
        stop.set()
        for t in threads: t.join(timeout=2)
        old_blocked_hits = api_old.requests_during_block

        # 场景 B：新客户端（熔断+退避）
        api_new = ApiMock(threshold=5, window=2.0, penalty=4.0)
        new_sess = FakeSession(); new_sess.api = api_new
        utils._make_session = lambda mode: new_sess
        utils.waf_breaker = utils.WafBreaker(base_cooldown=1.0, max_cooldown=4.0)  # 重置
        stop2 = threading.Event()

        def new_polite(sess):
            while not stop2.is_set():
                try:
                    utils.api_get("https://cbiz.yanhekt.cn/v1/x")
                except Exception:
                    pass
                time.sleep(0.01)

        threads = [threading.Thread(target=new_polite, args=(new_sess,), daemon=True) for _ in range(16)]
        for t in threads: t.start()
        time.sleep(3.0)
        stop2.set()
        for t in threads: t.join(timeout=6)
        new_blocked_hits = api_new.requests_during_block

        print(f"    旧客户端（16线程硬敲，无熔断）: 被限期间仍发出 {old_blocked_hits} 个请求")
        print(f"    新客户端（16线程+熔断退避）  : 被限期间仅发出 {new_blocked_hits} 个请求")
        check("熔断使被限期间请求量大幅下降", new_blocked_hits < old_blocked_hits / 3,
              f"{old_blocked_hits} → {new_blocked_hits}")

    finally:
        utils._make_session = real_make_session
        utils.waf_breaker = real_api_breaker
        utils.cdn_breaker = real_cdn_breaker
        utils.API_MIN_INTERVAL = real_api_interval

    print(f"\n==== 结果: {len(passed)} 通过, {len(failed)} 失败 ====")
    if failed:
        print("失败项:", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
