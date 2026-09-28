#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GLaDOS 自动签到

设计目标：
- 只使用 https://glados.cloud
- GLADOS_COOKIE 必须同时包含 koa:sess 和 koa:sess.sig
- 首先调用 /api/user/status 验证 Cookie / Session
- 再调用 /api/user/checkin 执行签到
- 最后调用 /api/user/points 获取积分
- 正常运行时输出积分和剩余天数
- Cookie、网络、API、签到等任意异常时返回 exit code 1
- 不发送 PushPlus / Telegram 等第三方通知
"""

import os
import sys
from datetime import datetime

import requests


# Windows 控制台 UTF-8
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


# 已知正常签到结果
NORMAL_CHECKIN_MESSAGES = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
)


# 用于识别登录 / 鉴权失败
AUTH_ERROR_MARKERS = (
    "没有权限",
    "无权限",
    "未登录",
    "请登录",
    "登录失效",
    "登录过期",
    "unauthorized",
    "forbidden",
    "not authorized",
    "not login",
    "not logged in",
    "login required",
)


# ================= 日志 =================

def log(message):
    """输出带时间戳日志。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ================= Cookie =================

def extract_cookie(raw):
    """
    严格检查 GLaDOS Cookie。

    必须同时包含：
    - koa:sess
    - koa:sess.sig

    不接受：
    - 单独 koa:sess
    - 单独 Token / JWT
    - JSON Token
    """
    if not raw:
        return None

    raw = raw.strip()

    if not raw:
        return None

    cookie_items = {}

    for item in raw.split(";"):
        item = item.strip()

        if "=" not in item:
            continue

        key, value = item.split("=", 1)

        key = key.strip()
        value = value.strip()

        cookie_items[key] = value

    sess = cookie_items.get("koa:sess")
    sess_sig = cookie_items.get("koa:sess.sig")

    if not sess and not sess_sig:
        log(
            "❌ GLADOS_COOKIE 格式错误："
            "没有找到 koa:sess 和 koa:sess.sig"
        )
        return None

    if not sess:
        log(
            "❌ GLADOS_COOKIE 不完整："
            "缺少 koa:sess"
        )
        return None

    if not sess_sig:
        log(
            "❌ GLADOS_COOKIE 不完整："
            "缺少 koa:sess.sig"
        )
        return None

    return raw


def get_cookies():
    """
    从环境变量读取 Cookie。

    单账号：
        直接填写完整 Cookie

    多账号：
        每行一个完整 Cookie
    """
    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []

    # 多账号使用换行分隔
    items = raw.splitlines()

    cookies = []

    for index, item in enumerate(items, 1):
        item = item.strip()

        if not item:
            continue

        cookie = extract_cookie(item)

        if cookie:
            cookies.append(cookie)
        else:
            log(
                f"❌ 第 {index} 个账号的 Cookie "
                "格式检查失败"
            )

    if not cookies:
        log("❌ 没有找到有效的 GLADOS_COOKIE")

    return cookies


# ================= API 判断 =================

def get_api_message(result):
    """安全读取 API message。"""
    if not isinstance(result, dict):
        return ""

    return str(
        result.get("message", "")
    ).strip()


def get_api_code(result):
    """安全读取 API code。"""
    if not isinstance(result, dict):
        return "?"

    return str(
        result.get("code", "?")
    )


def is_auth_failure(result):
    """
    判断 API JSON 是否明确表示鉴权失败。

    注意：
    GLaDOS 可能使用 HTTP 200 +
    {"message": "没有权限"}
    来表示鉴权失败。
    """
    message = get_api_message(result).lower()

    if not message:
        return False

    return any(
        marker in message
        for marker in AUTH_ERROR_MARKERS
    )


def is_normal_checkin_result(result):
    """判断是否属于正常签到结果。"""
    if not isinstance(result, dict):
        return False

    message = get_api_message(result).lower()

    return any(
        marker in message
        for marker in NORMAL_CHECKIN_MESSAGES
    )


def safe_result_description(result):
    """
    输出 API 的安全诊断信息。

    只显示：
    - code
    - message
    - JSON 顶层字段名称

    不输出 data 内容，
    避免把邮箱等账号信息写进公开 Actions 日志。
    """
    if not isinstance(result, dict):
        return "非 JSON 对象"

    code = get_api_code(result)
    message = get_api_message(result) or "(空)"

    keys = ", ".join(
        str(key)
        for key in result.keys()
    )

    return (
        f"code={code}, "
        f"message={message}, "
        f"fields=[{keys}]"
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

        成功：
            返回 dict

        网络 / HTTP / JSON 异常：
            返回 None
        """

        url = f"{BASE_URL}{path}"

        headers = HEADERS.copy()

        headers["Cookie"] = self.cookie
        headers["Origin"] = BASE_URL
        headers["Referer"] = (
            f"{BASE_URL}/console/checkin"
        )

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
                log(
                    f"❌ 不支持的 HTTP 方法: "
                    f"{method}"
                )
                return None

        except requests.Timeout:
            log(
                f"❌ {method} {path} 请求超时"
            )
            return None

        except requests.ConnectionError as exc:
            log(
                f"❌ {method} {path} "
                f"网络连接失败: {exc}"
            )
            return None

        except requests.RequestException as exc:
            log(
                f"❌ {method} {path} "
                f"HTTP 请求异常: {exc}"
            )
            return None

        # HTTP 层鉴权失败
        if response.status_code in (401, 403):
            log(
                f"❌ {method} {path} | "
                f"HTTP {response.status_code} | "
                "服务器拒绝访问"
            )

            return None

        if response.status_code != 200:
            # 尝试提取服务端 message，
            # 但绝不打印 Cookie
            try:
                result = response.json()

                detail = safe_result_description(
                    result
                )

            except ValueError:
                detail = "响应不是 JSON"

            log(
                f"❌ {method} {path} | "
                f"HTTP {response.status_code} | "
                f"{detail}"
            )

            return None

        try:
            result = response.json()

        except ValueError:
            log(
                f"❌ {method} {path} | "
                "HTTP 200，但响应不是有效 JSON"
            )
            return None

        if not isinstance(result, dict):
            log(
                f"❌ {method} {path} | "
                "HTTP 200，但 JSON 数据结构异常"
            )
            return None

        return result

    # =============================
    # 1. 登录状态 / Cookie 鉴权
    # =============================

    def get_status(self):
        """
        获取账号状态。

        这是整个流程的第一步，
        用于判断 Cookie 是否具有基本登录权限。
        """

        path = "/api/user/status"

        result = self.request(
            "GET",
            path,
        )

        if result is None:
            log(
                "❌ 无法完成 Cookie 鉴权检查"
            )

            return False

        # HTTP 200，但 JSON 明确表示没权限
        if is_auth_failure(result):
            log(
                "❌ Cookie / Session 鉴权失败"
            )

            log(
                f"   接口: GET {path}"
            )

            log(
                "   HTTP: 200"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            log(
                "   判断: 登录 Cookie 已失效、"
                "不完整，或 glados.cloud "
                "不认可当前 Session"
            )

            return False

        data = result.get("data")

        if not isinstance(data, dict):
            log(
                "❌ 状态接口返回异常"
            )

            log(
                f"   接口: GET {path}"
            )

            log(
                "   HTTP: 200"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            return False

        left_days = data.get("leftDays")

        if left_days is None:
            log(
                "❌ 状态接口没有返回 leftDays"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            return False

        try:
            self.left_days = str(
                int(float(left_days))
            )

        except (TypeError, ValueError):
            log(
                "❌ 无法解析剩余天数"
            )

            return False

        log(
            "✅ Cookie 基础鉴权通过 "
            "(GET /api/user/status)"
        )

        return True

    # =============================
    # 2. 签到
    # =============================

    def checkin(self):
        """
        执行签到并详细检查返回结果。
        """

        path = "/api/user/checkin"

        result = self.request(
            "POST",
            path,
            {
                "token": "glados.cloud",
            },
        )

        if result is None:
            log(
                "❌ 签到接口请求失败"
            )

            return False, ""

        message = get_api_message(result)

        # 登录状态接口已经成功，
        # 但签到接口单独返回没有权限
        if is_auth_failure(result):
            log(
                "❌ 签到接口单独发生权限拒绝"
            )

            log(
                f"   接口: POST {path}"
            )

            log(
                "   HTTP: 200"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            log(
                "   判断: Cookie 已通过 status "
                "接口鉴权，但 checkin 接口拒绝操作"
            )

            return False, message

        if not is_normal_checkin_result(result):
            log(
                "❌ 签到接口返回未知/异常结果"
            )

            log(
                f"   接口: POST {path}"
            )

            log(
                "   HTTP: 200"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            return False, message

        return True, message

    # =============================
    # 3. 积分
    # =============================

    def get_points(self):
        """获取当前积分。"""

        path = "/api/user/points"

        result = self.request(
            "GET",
            path,
        )

        if result is None:
            log(
                "❌ 积分接口请求失败"
            )

            return False

        if is_auth_failure(result):
            log(
                "❌ 积分接口发生权限拒绝"
            )

            log(
                f"   接口: GET {path}"
            )

            log(
                "   HTTP: 200"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            return False

        points = result.get("points")

        if points is None:
            log(
                "❌ 积分接口没有返回 points"
            )

            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )

            return False

        try:
            self.points = str(
                int(float(points))
            )

        except (TypeError, ValueError):
            log(
                f"❌ 无法解析积分值: "
                f"{points}"
            )

            return False

        return True


# ================= 单账号 =================

def run_account(index, total, cookie):
    """
    单账号流程：

    1. status
       ↓
       判断 Cookie 是否有效

    2. checkin
       ↓
       判断签到接口是否有权限

    3. points
       ↓
       获取积分
    """

    log(
        f"---------- 账号 "
        f"{index}/{total} ----------"
    )

    client = GLaDOS(cookie)

    # ---------------------------------
    # Step 1：首先验证 Cookie
    # ---------------------------------

    log(
        "🔎 Step 1/3: "
        "检查 Cookie / Session 权限"
    )

    if not client.get_status():
        log(
            f"❌ 账号 {index} "
            "基础鉴权失败，停止后续请求"
        )

        return False

    # ---------------------------------
    # Step 2：签到
    # ---------------------------------

    log(
        "🔎 Step 2/3: "
        "检查签到接口权限并执行签到"
    )

    checkin_ok, message = client.checkin()

    if not checkin_ok:
        log(
            f"❌ 账号 {index} "
            "签到失败"
        )

        return False

    # ---------------------------------
    # Step 3：积分
    # ---------------------------------

    log(
        "🔎 Step 3/3: "
        "读取当前积分"
    )

    if not client.get_points():
        log(
            f"❌ 账号 {index} "
            "积分接口异常"
        )

        return False

    # ---------------------------------
    # 全部成功
    # ---------------------------------

    log(
        f"✅ 账号 {index} | "
        f"积分: {client.points} | "
        f"剩余天数: {client.left_days} | "
        f"签到结果: {message}"
    )

    return True


# ================= 主程序 =================

def main():
    log(
        "🚀 GLaDOS Checkin Starting..."
    )

    cookies = get_cookies()

    if not cookies:
        log(
            "❌ 无可用 Cookie，程序终止"
        )

        return 1

    total = len(cookies)
    success_count = 0

    for index, cookie in enumerate(
        cookies,
        start=1,
    ):
        try:
            success = run_account(
                index,
                total,
                cookie,
            )

            if success:
                success_count += 1

        except Exception as exc:
            # 防止程序本身发生未知异常时
            # 被 GitHub Actions 误判成成功
            log(
                f"❌ 账号 {index} "
                "发生未处理异常 | "
                f"{type(exc).__name__}: {exc}"
            )

    log(
        "----------------------------------------"
    )

    if success_count == total:
        log(
            "✅ 全部账号运行正常 "
            f"({success_count}/{total})"
        )

        # GitHub Actions -> Success
        return 0

    log(
        "❌ 存在异常账号 "
        f"({success_count}/{total})"
    )

    # GitHub Actions -> Failure
    return 1


if __name__ == "__main__":
    sys.exit(main())
