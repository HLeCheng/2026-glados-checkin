#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GLaDOS 自动签到

功能：
- 使用 https://glados.cloud
- 自动签到
- 获取当前积分
- 获取剩余会员天数
- 支持多个 Cookie
- 不发送 PushPlus / Telegram 等第三方通知
- 正常签到时只输出日志
- Cookie 失效、网络异常、API 异常、签到异常时：
  返回非 0 退出码，让 GitHub Actions 标记为 Failure
"""

import json
import os
import sys
from datetime import datetime

import requests


# Windows 终端 UTF-8
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")


# ================= 配置 =================

BASE_URL = "https://glados.cloud"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


# 已知正常签到返回
NORMAL_CHECKIN_MESSAGES = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
)


# ================= 工具函数 =================

def log(message):
    """输出带时间戳的日志。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def extract_cookie(raw):
    """
    校验并返回完整 GLaDOS Cookie。

    必须同时包含：
    - koa:sess=
    - koa:sess.sig=

    不再接受：
    - 单独 koa:sess
    - 单独 token / JWT
    - JSON token
    """
    if not raw:
        return None

    raw = raw.strip()

    if not raw:
        return None

    has_sess = "koa:sess=" in raw
    has_sig = "koa:sess.sig=" in raw

    if has_sess and has_sig:
        return raw

    if has_sess and not has_sig:
        log(
            "❌ GLADOS_COOKIE 不完整："
            "已找到 koa:sess，但缺少 koa:sess.sig"
        )
        return None

    if has_sig and not has_sess:
        log(
            "❌ GLADOS_COOKIE 不完整："
            "已找到 koa:sess.sig，但缺少 koa:sess"
        )
        return None

    log(
        "❌ GLADOS_COOKIE 格式错误："
        "必须同时包含 koa:sess 和 koa:sess.sig"
    )
    return None


def get_cookies():
    """读取并严格校验 GLADOS_COOKIE。"""
    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []

    # 多账号建议使用换行分隔。
    # 同时保留对旧版 & 分隔方式的兼容。
    separator = "\n" if "\n" in raw else "&"

    cookies = []

    for index, item in enumerate(raw.split(separator), 1):
        item = item.strip()

        if not item:
            continue

        cookie = extract_cookie(item)

        if cookie:
            cookies.append(cookie)
        else:
            log(f"❌ 第 {index} 个账号的 Cookie 无效")

    if not cookies:
        log("❌ 没有找到有效的 GLADOS_COOKIE")

    return cookies


def is_normal_checkin_result(result):
    """
    判断签到结果是否正常。

    正常情况包括：
    - 本次签到成功
    - 今天已经签到过
    """
    if not isinstance(result, dict):
        return False

    message = str(result.get("message", "")).strip().lower()

    return any(
        marker in message
        for marker in NORMAL_CHECKIN_MESSAGES
    )


# ================= GLaDOS API =================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.points = "?"
        self.left_days = "?"

    def request(self, method, path, data=None):
        """
        请求 glados.cloud API。

        返回：
            dict -> 请求及 JSON 解析成功
            None -> 网络 / HTTP / JSON 异常
        """

        url = f"{BASE_URL}{path}"

        headers = HEADERS.copy()

        headers["Cookie"] = self.cookie
        headers["Origin"] = BASE_URL
        headers["Referer"] = f"{BASE_URL}/console/checkin"

        try:
            if method == "GET":
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=15,
                )

            elif method == "POST":
                headers["Content-Type"] = (
                    "application/json;charset=UTF-8"
                )

                response = requests.post(
                    url,
                    headers=headers,
                    json=data,
                    timeout=15,
                )

            else:
                log(f"❌ 不支持的 HTTP 方法: {method}")
                return None

        except requests.Timeout:
            log(f"❌ 请求超时: {url}")
            return None

        except requests.ConnectionError as exc:
            log(f"❌ 网络连接失败: {exc}")
            return None

        except requests.RequestException as exc:
            log(f"❌ HTTP 请求异常: {exc}")
            return None

        # Cookie / Session 常见失效状态
        if response.status_code in (401, 403):
            log(
                f"❌ HTTP {response.status_code}: "
                "Cookie 可能已过期或登录状态已失效"
            )
            return None

        if response.status_code != 200:
            log(
                f"❌ API 返回异常状态码: "
                f"HTTP {response.status_code}"
            )
            return None

        try:
            result = response.json()

        except ValueError:
            log("❌ API 返回内容不是有效 JSON")
            return None

        if not isinstance(result, dict):
            log("❌ API 返回的数据结构异常")
            return None

        return result

    def checkin(self):
        """执行签到。"""
        return self.request(
            "POST",
            "/api/user/checkin",
            {
                "token": "glados.cloud",
            },
        )

    def get_status(self):
        """获取剩余会员天数。"""
        result = self.request(
            "GET",
            "/api/user/status",
        )

        if not isinstance(result, dict):
            return False

        data = result.get("data")

        if not isinstance(data, dict):
            log("❌ 状态接口缺少 data")
            return False

        left_days = data.get("leftDays")

        if left_days is None:
            log("❌ 状态接口没有返回 leftDays")
            return False

        try:
            self.left_days = str(
                int(float(left_days))
            )

        except (TypeError, ValueError):
            log(
                f"❌ 无法解析剩余天数: "
                f"{left_days}"
            )
            return False

        return True

    def get_points(self):
        """获取当前积分。"""
        result = self.request(
            "GET",
            "/api/user/points",
        )

        if not isinstance(result, dict):
            return False

        points = result.get("points")

        if points is None:
            log("❌ 积分接口没有返回 points")
            return False

        try:
            self.points = str(
                int(float(points))
            )

        except (TypeError, ValueError):
            log(
                f"❌ 无法解析积分: "
                f"{points}"
            )
            return False

        return True


# ================= 主程序 =================

def run_account(index, total, cookie):
    """
    执行单个账号。

    返回：
        True  -> 完全正常
        False -> 任意环节异常
    """

    log(f"---------- 账号 {index}/{total} ----------")

    client = GLaDOS(cookie)

    # ======================
    # 1. 签到
    # ======================

    checkin_result = client.checkin()

    if checkin_result is None:
        log(f"❌ 账号 {index} 签到请求失败")
        return False

    message = str(
        checkin_result.get("message", "")
    ).strip()

    if not is_normal_checkin_result(checkin_result):
        log(
            f"❌ 账号 {index} 签到返回异常 | "
            f"结果: {message or 'Unknown'}"
        )
        return False

    # ======================
    # 2. 获取剩余天数
    # ======================

    if not client.get_status():
        log(
            f"❌ 账号 {index} 状态接口异常，"
            "无法获取剩余天数"
        )
        return False

    # ======================
    # 3. 获取积分
    # ======================

    if not client.get_points():
        log(
            f"❌ 账号 {index} 积分接口异常，"
            "无法获取当前积分"
        )
        return False

    # ======================
    # 全部正常
    # ======================

    log(
        f"✅ 账号 {index} | "
        f"积分: {client.points} | "
        f"剩余天数: {client.left_days} | "
        f"结果: {message}"
    )

    return True


def main():
    log("🚀 GLaDOS Checkin Starting...")

    cookies = get_cookies()

    if not cookies:
        log("❌ 无可用 Cookie，程序终止")
        return 1

    total = len(cookies)
    success_count = 0

    for index, cookie in enumerate(cookies, 1):
        try:
            if run_account(
                index,
                total,
                cookie,
            ):
                success_count += 1

        except Exception as exc:
            # 防止未知程序异常被误判成成功
            log(
                f"❌ 账号 {index} "
                f"发生未处理异常: "
                f"{type(exc).__name__}: {exc}"
            )

    log("----------------------------------------")

    if success_count == total:
        log(
            f"✅ 全部账号运行正常 "
            f"({success_count}/{total})"
        )

        # GitHub Actions -> Success
        return 0

    log(
        f"❌ 存在异常账号 "
        f"({success_count}/{total})"
    )

    # GitHub Actions -> Failure
    return 1


if __name__ == "__main__":
    sys.exit(main())
