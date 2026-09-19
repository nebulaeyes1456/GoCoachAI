# -*- coding: utf-8 -*-
"""外源模型调用（PinAI 异源渠道，GoCoachAI2 用）。

用途：把**低风险、可机器验证**的任务交给异源模型（默认 `PinAI/gpt-6`，
effort high，用户 2026-09-19 指令），产出**一律经本仓库工具机器验证后**才入
库（见 `数学结构发现工作台/meta/外部模型接入.md` §4/§9 纪律）。

只用标准库 urllib（**不依赖 requests**——本项目 venv 未装第三方 HTTP 库）。

发送前自检（调用前逐条过）：
① 内容是否只含公开知识/自有代码（围棋定式、棋理、本项目脚本）？
② 是否含密钥、个人信息、机器配置、成本配额、用户指令原文、内部协议？
③ 是否属于「未稳定、泄露有实质损失」的内容？——三条任一为否则不发。

key 只从环境变量 `PINAI_API_KEY` 读，不回显、不落盘、不写入产出。

用法（项目根）：
    .\\.venv\\Scripts\\python.exe scripts\\ask_external.py \\
        --task-file data\\tmp\\prompt_joseki.md --out data\\tmp\\external_joseki.md --effort high
    # 先自检（不联网）：
    .\\.venv\\Scripts\\python.exe scripts\\ask_external.py \\
        --task-file data\\tmp\\prompt_joseki.md --out data\\tmp\\x.md --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.pinaic.com/v1"


def _read(path_text: str, what: str) -> str:
    """读文件：相对路径先按当前目录、再按项目根解析；失败时列出试过的绝对路径。"""
    cands = [Path(path_text)]
    if not Path(path_text).is_absolute():
        cands.append(ROOT / path_text)
    for c in cands:
        if c.is_file():
            print("[i] " + what + ": " + str(c.resolve()))
            return c.read_text(encoding="utf-8")
    tried = "；".join(str(c.resolve()) for c in cands)
    print("[X] 找不到" + what + "：" + path_text)
    print("    试过：" + tried)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file", required=True, help="提示词/任务书文件")
    ap.add_argument("--out", required=True, help="产出落盘路径")
    ap.add_argument("--model", default="PinAI/gpt-6")
    ap.add_argument("--effort", default="high",
                    choices=("low", "medium", "high"))
    ap.add_argument("--system", default="", help="（可选）system 提示词文件")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="只做本地自检（文件/密钥/请求体），不联网")
    ap.add_argument("--fallbacks", default="PinAI/gpt-5.6,PinAI/gpt-5.5,PinAI/gpt-5.4",
                    help="上游 5xx/超时时的降级模型链（逗号分隔，按可用最强）")
    ap.add_argument("--list-models", action="store_true",
                    help="只列出自定义网关当前暴露的模型（GET /v1/models）")
    args = ap.parse_args()

    key = os.environ.get("PINAI_API_KEY", "")
    if not key:
        print("[X] 环境变量 PINAI_API_KEY 未设置（不回显、不落盘）")
        print('    当前 PowerShell 窗口临时设置：$env:PINAI_API_KEY="<你的key>"')
        sys.exit(2)
    if args.list_models:
        req = urllib.request.Request(
            BASE + "/models",
            headers={"Authorization": "Bearer " + key}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ids = sorted(d.get("id", "") for d in (data.get("data") or []))
            print("[i] 网关当前暴露 " + str(len(ids)) + " 个模型：")
            for i in ids:
                print("    " + i)
        except Exception as exc:  # noqa: BLE001
            print("[X] 列模型失败：" + repr(exc))
            sys.exit(1)
        return

    task = _read(args.task_file, "任务书")
    system = _read(args.system, "system 提示词") if args.system else ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": task})
    payload = {
        "model": args.model,
        "reasoning_effort": args.effort,
        "messages": messages,
        "temperature": 0.3,
    }
    body = json.dumps(payload).encode("utf-8")
    url = BASE + "/chat/completions"
    print("将调用 " + args.model + "（effort=" + args.effort + "，任务书 "
          + str(len(task)) + " 字符，system " + str(len(system))
          + " 字符）目标 " + url)

    if args.dry_run:
        print("[dry-run] 自检通过：文件可读、密钥已就位、请求体 "
              + str(len(body)) + " 字节（未联网）")
        return

    models = [args.model] + [
        m.strip() for m in args.fallbacks.split(",") if m.strip() and m.strip() != args.model
    ]
    raw = ""
    used = ""
    t0 = time.time()
    for idx, model in enumerate(models):
        payload["model"] = model
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
            method="POST",
        )
        if idx:
            print("[i] 降级重试 → " + model)
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            used = model
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            print("    [X] " + model + " → HTTP " + str(exc.code) + ": " + detail)
        except Exception as exc:  # noqa: BLE001 —— 网络/证书/超时
            print("    [X] " + model + " → " + repr(exc))
        if idx + 1 < len(models):
            time.sleep(3)
    if not used:
        print("[X] 全部模型均不可用（含降级链：" + ", ".join(models) + "）")
        print("    可先跑 --list-models 看网关当前暴露了哪些模型。")
        sys.exit(1)
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        print("[X] 返回体无法解析，原文前 300 字：\n" + raw[:300])
        sys.exit(1)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8", newline="\n")
    meta_path = Path(str(out) + ".meta.txt")
    meta_path.write_text(
        "model=" + used + "\neffort=" + args.effort
        + "\ntask_file=" + str(args.task_file)
        + "\nseconds=" + str(int(time.time() - t0))
        + "\nchars=" + str(len(content)) + "\n",
        encoding="utf-8", newline="\n",
    )
    print("[OK] " + str(int(time.time() - t0)) + "s，产出 "
          + str(len(content)) + " 字符 → " + str(out)
          + "（实际模型 " + used + "，旁注 " + meta_path.name + "）")


if __name__ == "__main__":
    main()
