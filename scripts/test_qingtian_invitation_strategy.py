"""QingTian invitation_strategy 工作流连通性测试（纯代码模式）。

自包含脚本，不依赖项目内的 QingTianClient，仅用 requests 直连 QingTian 平台。
提交一份 invitation_strategy 任务并轮询结果，用于验证工作流连通性。

用法：
    1. 填写下方 API_KEY（QINGTIAN_INVITATION_STRATEGY_API_KEY）。
    2. 运行：
           python scripts/test_qingtian_invitation_strategy.py

流程：
    1. 提交  —— POST /run_app_workflow 返回 runId（连通性 + 鉴权）
    2. 轮询  —— POST /query_run_app_process 直到终态
    3. 输出  —— 打印工作流返回的 output
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

# ---------------------------------------------------------------------------
# 配置 —— 运行前填写。
# ---------------------------------------------------------------------------

API_KEY = "d96auef9vlnmsndeicag"  # QINGTIAN_INVITATION_STRATEGY_API_KEY

# base_url 末尾带斜杠；客户端拼接 run_app_workflow / query_run_app_process。
BASE_URL = "https://qingtian.dfmc.com.cn/api/proxy/api/v1/"
USER_ID = "klh"

# 默认任务参数。
PHONE_NUMBER = "13203950139"
WORK_ENV = "production"
BATCH_ID = "batch001"

# 轮询设置。
POLL_INTERVAL_SEC = 2
POLL_TIMEOUT_SEC = 2400  # 40 分钟，与生产默认一致
REQUEST_TIMEOUT_SEC = 120

# 状态判定。
NON_TERMINAL_STATUSES = {"to_start", "processing"}
SUCCESS_STATUS = "success"
FAILURE_STATUSES = {"failed", "stopped", "interrupted", "timeout"}

# ---------------------------------------------------------------------------


def make_request(
    url: str,
    headers: dict[str, str],
    data: dict[str, Any],
    max_retries: int = 3,
) -> Any | None:
    """
    发起 POST 请求并返回解析后的 JSON

    参数:
        url (str): 请求地址
        headers (dict): 请求头
        data (dict): 请求体（JSON）
        max_retries (int): 最大重试次数，默认3次

    返回:
        dict: 成功返回解析后的 JSON，失败返回 None
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            print(f"  请求失败（第 {attempt}/{max_retries} 次）：{exc}")
            if attempt < max_retries:
                time.sleep(POLL_INTERVAL_SEC)
    return None


def run_workflow(max_retries: int = 3) -> Any | None:
    """
    提交 invitation_strategy 工作流并返回 runId

    参数:
        max_retries (int): 最大重试次数，默认3次

    返回:
        str: 成功返回 runId，失败返回 None
    """
    API_URL = BASE_URL + "run_app_workflow"
    HEADERS = {
        "Content-Type": "application/json",
        "Apikey": API_KEY,
    }
    # invitation_strategy 工作流的输入结构。
    input_data = {
        "phone_number": PHONE_NUMBER,
        "work_env": WORK_ENV,
        "batch_id": BATCH_ID,
    }
    BASE_DATA = {
        "UserID": USER_ID,
        "AppKey": API_KEY,
        "InputData": json.dumps(input_data, ensure_ascii=False),
    }

    result = make_request(API_URL, HEADERS, BASE_DATA, max_retries)

    if result and "runId" in result:
        return result["runId"]
    # 兼容大小写不同的字段名。
    if isinstance(result, dict):
        run_id = result.get("RunID") or result.get("run_id")
        if run_id:
            return run_id
    return None


def query_run_result(run_id: str, max_retries: int = 3) -> Any | None:
    """
    轮询工作流运行结果直到终态

    参数:
        run_id (str): run_workflow 返回的 runId
        max_retries (int): 单次查询的最大重试次数，默认3次

    返回:
        str: 成功返回工作流 output 字符串；失败或超时返回 None
    """
    API_URL = BASE_URL + "query_run_app_process"
    HEADERS = {
        "Content-Type": "application/json",
        "Apikey": API_KEY,
    }
    BASE_DATA = {
        "RunID": run_id,
        "UserID": USER_ID,
        "AppKey": API_KEY,
    }

    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while True:
        result = make_request(API_URL, HEADERS, BASE_DATA, max_retries)
        if result is None:
            print("  查询请求失败，终止轮询")
            return None

        status = result.get("status") if isinstance(result, dict) else None
        print(f"  status={status}")

        if status == SUCCESS_STATUS:
            output = result.get("output")
            if isinstance(output, str) and output.strip():
                return output
            print("  工作流成功但未返回 output 字符串（工作流配置问题）")
            return None

        if status in FAILURE_STATUSES:
            message = result.get("message") or result.get("msg") or ""
            code = result.get("code")
            print(f"  工作流异常终止：status={status} code={code} message={message}")
            return None

        if time.monotonic() >= deadline:
            print(f"  轮询超时（{POLL_TIMEOUT_SEC}s），工作流未到达终态")
            return None

        time.sleep(POLL_INTERVAL_SEC)


def main() -> int:
    if not API_KEY.strip():
        print("ERROR: API_KEY 为空，请编辑脚本填写 "
              "（QINGTIAN_INVITATION_STRATEGY_API_KEY）。")
        return 2

    print("=" * 72)
    print("QingTian invitation_strategy 连通性测试（纯代码模式）")
    print(f"  base_url   : {BASE_URL}")
    print(f"  user_id    : {USER_ID}")
    print(f"  phone      : {PHONE_NUMBER}")
    print(f"  work_env   : {WORK_ENV}")
    print(f"  batch_id   : {BATCH_ID}")
    print("=" * 72)

    started = time.monotonic()

    print("[1/2] 提交工作流 /run_app_workflow ...")
    run_id = run_workflow()
    if not run_id:
        print(f"\n结果：提交失败，未拿到 runId（耗时 {time.monotonic() - started:.1f}s）")
        print("提交阶段就失败，通常是连通性/鉴权问题：URL、网络或 Apikey。")
        return 1
    print(f"      已提交，runId={run_id}")

    print("[2/2] 轮询结果 /query_run_app_process ...")
    output = query_run_result(run_id)
    elapsed = time.monotonic() - started
    if output is None:
        print(f"\n结果：失败（耗时 {elapsed:.1f}s）—— 平台已接收任务，但工作流未成功返回结果")
        return 1

    print(f"\n结果：成功（耗时 {elapsed:.1f}s）—— 工作流 status=success")
    print("-" * 72)
    print("原始 output：")
    try:
        print(json.dumps(json.loads(output), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(output)
        print("（注意：output 不是合法 JSON）")
    print("-" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
