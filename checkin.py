#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GLaDOS 自动签到

目标：
- 仅使用 https://glados.cloud
- 支持 koa:* / gld:* Session Cookie
- 先验证登录状态，再签到，再读取积分
- 签到成功与否主要依据 API 结构化 code，而不是固定 message 文案
- 正常时在 GitHub Actions 日志显示积分、剩余天数、签到结果
- Cookie / 网络 / API / 签到异常时返回 exit code 1
- 不使用 PushPlus / Telegram 等第三方通知
"""

import os
import sys
from datetime import datetime

import requests


# Windows 控制台 UTF-8
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")


# ============================================================
# 配置
# ============================================================

BASE_URL = "https://glados.cloud"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# 这里只用于识别明显的鉴权错误。
# 不用于判断签到“成功”。
AUTH_ERROR_MARKERS = (
    "没有权限",
    "无权限",
    "未登录",
    "请登录",
    "登录失效",
    "登录过期",
    "unauthorized",
    "forbidden",
    "permission denied",
    "not authorized",
    "not logged in",
    "not login",
    "login required",
)


# ============================================================
# 日志
# ============================================================

def log(message):
    """输出带时间戳的日志。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# Cookie
# ============================================================

def inspect_cookie(raw, index):
    """
    检查 Cookie 中已知的 Session 项。

    注意：
    这里只做诊断，不把已知 Cookie 名称当成最终鉴权标准。

    真正是否登录有效，最终由：
        GET /api/user/status
    判断。

    这样即使未来 GLaDOS 再修改 Cookie 名称，
    也不会因为本地白名单导致有效 Cookie 被直接拒绝。
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

        if key:
            cookie_items[key] = value

    # koa Session
    has_koa_sess = bool(cookie_items.get("koa:sess"))
    has_koa_sig = bool(cookie_items.get("koa:sess.sig"))
    koa_complete = has_koa_sess and has_koa_sig

    # gld Session
    has_gld_sess = bool(cookie_items.get("gld:sess"))
    has_gld_sig = bool(cookie_items.get("gld:sess.sig"))
    gld_complete = has_gld_sess and has_gld_sig

    # 某组只有一半时给出警告，但仍交给服务器验证
    if has_koa_sess != has_koa_sig:
        log(
            f"⚠️ 账号 {index}: "
            "检测到 koa Session Cookie 不完整"
        )

    if has_gld_sess != has_gld_sig:
        log(
            f"⚠️ 账号 {index}: "
            "检测到 gld Session Cookie 不完整"
        )

    log(
        f"🔐 账号 {index} Cookie Session | "
        f"koa={'✅' if koa_complete else '—'} | "
        f"gld={'✅' if gld_complete else '—'}"
    )

    if not koa_complete and not gld_complete:
        log(
            f"⚠️ 账号 {index}: "
            "没有识别到完整的 koa/gld Session，"
            "继续交由 glados.cloud 实际验证"
        )

    # 不修改用户提供的 Cookie
    # 完整原样发送给 glados.cloud
    return raw


def get_cookies():
    """
    从 GLADOS_COOKIE 环境变量读取 Cookie。

    单账号：
        一个完整 Cookie

    多账号：
        每行一个完整 Cookie

    不再使用 & 分隔，避免 Cookie 本身包含特殊字符时被误拆。
    """
    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []

    items = [
        item.strip()
        for item in raw.splitlines()
        if item.strip()
    ]

    if not items:
        log("❌ GLADOS_COOKIE 为空")
        return []

    cookies = []

    for index, item in enumerate(items, 1):
        cookie = inspect_cookie(item, index)

        if cookie:
            cookies.append(cookie)

    return cookies


# ============================================================
# API 工具
# ============================================================

def get_api_message(result):
    """安全获取 API message。"""
    if not isinstance(result, dict):
        return ""

    return str(
        result.get("message", "")
    ).strip()


def get_api_code(result):
    """
    获取 API code。

    返回：
        int  -> 可以正常解析
        None -> 缺少或无法解析
    """
    if not isinstance(result, dict):
        return None

    value = result.get("code")

    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_auth_failure(result):
    """
    根据 message 判断是否属于明显鉴权失败。

    GLaDOS 有时可能：
        HTTP 200
        + JSON message="没有权限"

    所以不能只看 HTTP 状态码。
    """
    message = get_api_message(result).lower()

    if not message:
        return False

    return any(
        marker in message
        for marker in AUTH_ERROR_MARKERS
    )


def safe_result_description(result):
    """
    生成安全的 API 诊断信息。

    只输出：
    - code
    - message
    - JSON 顶层字段名

    不输出 data 内容，避免公开 Actions 日志泄露账号信息。
    """
    if not isinstance(result, dict):
        return "非 JSON 对象"

    code = result.get("code", "?")
    message = get_api_message(result) or "(空)"

    fields = ", ".join(
        str(key)
        for key in result.keys()
    )

    return (
        f"code={code}, "
        f"message={message}, "
        f"fields=[{fields}]"
    )


def is_normal_checkin_result(result):
    """
    判断签到接口是否属于正常状态。

    当前结构化状态：
        code == 0 -> 本次签到成功
        code == 1 -> 重复签到 / 今日已经签到

    重要：
    - 不匹配具体成功 message
    - message 改文案不会影响判断
    - 明显鉴权失败始终优先判定为失败
    - 未知 code 保守地判定为失败
    """
    if not isinstance(result, dict):
        return False

    # 必须先排除类似：
    # code=1 + message="没有权限"
    # 这种潜在情况
    if is_auth_failure(result):
        return False

    code = get_api_code(result)

    return code in (0, 1)


# ============================================================
# GLaDOS API
# ============================================================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie

        self.left_days = "?"
        self.points = "?"

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

        if method == "POST":
            headers["Content-Type"] = (
                "application/json;charset=UTF-8"
            )

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=data if method == "POST" else None,
                timeout=15,
            )

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

        # 其他非 200
        if response.status_code != 200:
            try:
                result = response.json()
                detail = safe_result_description(result)

            except ValueError:
                detail = "响应不是 JSON"

            log(
                f"❌ {method} {path} | "
                f"HTTP {response.status_code} | "
                f"{detail}"
            )
            return None

        # HTTP 200，继续解析 JSON
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

    # --------------------------------------------------------
    # 1. 验证登录状态
    # --------------------------------------------------------

    def get_status(self):
        """
        获取账号状态。

        这是整个流程的第一步：
        由服务器实际判断 Cookie / Session 是否有效。
        """
        path = "/api/user/status"

        result = self.request(
            "GET",
            path,
        )

        if result is None:
            log(
                "❌ 无法完成 Cookie / Session 鉴权检查"
            )
            return False

        # HTTP 200 但 API 明确拒绝
        if is_auth_failure(result):
            log(
                "❌ Cookie / Session 鉴权失败"
            )
            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )
            return False

        data = result.get("data")

        if not isinstance(data, dict):
            log(
                "❌ 状态接口返回异常"
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
                f"❌ 无法解析剩余天数: "
                f"{left_days}"
            )
            return False

        log(
            "✅ Cookie 基础鉴权通过 "
            "(GET /api/user/status)"
        )

        return True

    # --------------------------------------------------------
    # 2. 签到
    # --------------------------------------------------------

    def checkin(self):
        """
        执行签到。

        不依赖 message 文案判断成功。
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
            log("❌ 签到接口请求失败")
            return False, "", None

        message = get_api_message(result)
        code = get_api_code(result)

        # 即使 code 看起来正常，也优先排除明显权限问题
        if is_auth_failure(result):
            log(
                "❌ 签到接口发生权限拒绝"
            )
            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )
            return False, message, code

        if not is_normal_checkin_result(result):
            log(
                "❌ 签到接口返回异常状态"
            )
            log(
                f"   API: "
                f"{safe_result_description(result)}"
            )
            return False, message, code

        if code == 0:
            log(
                "✅ 签到接口返回成功状态 "
                "(code=0)"
            )

        elif code == 1:
            log(
                "✅ 今日已签到 / 重复签到 "
                "(code=1)"
            )

        return True, message, code

    # --------------------------------------------------------
    # 3. 获取积分
    # --------------------------------------------------------

    def get_points(self):
        """读取当前积分。"""
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
                f"❌ 无法解析积分: "
                f"{points}"
            )
            return False

        return True


# ============================================================
# 单账号流程
# ============================================================

def run_account(index, total, cookie):
    """
    单账号执行顺序：

        Cookie
          ↓
        /status
          ↓
        /checkin
          ↓
        /points
    """
    log(
        f"---------- 账号 "
        f"{index}/{total} ----------"
    )

    client = GLaDOS(cookie)

    # Step 1
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

    # Step 2
    log(
        "🔎 Step 2/3: "
        "执行签到"
    )

    checkin_ok, message, code = client.checkin()

    if not checkin_ok:
        log(
            f"❌ 账号 {index} "
            "签到失败"
        )
        return False

    # Step 3
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

    # 全部正常
    display_message = (
        message
        if message
        else "(服务器未返回 message)"
    )

    log(
        f"✅ 账号 {index} | "
        f"积分: {client.points} | "
        f"剩余天数: {client.left_days} | "
        f"签到状态码: {code} | "
        f"签到结果: {display_message}"
    )

    return True


# ============================================================
# 主程序
# ============================================================

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
            if run_account(
                index,
                total,
                cookie,
            ):
                success_count += 1

        except Exception as exc:
            # 未预料异常也必须让 Action 失败
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
