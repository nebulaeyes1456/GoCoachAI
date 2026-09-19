# -*- coding: utf-8 -*-
"""外源模型调用（PinAI 异源渠道，GoCoachAI2 用）。

用途：把**低风险、可机器验证**的任务交给异源模型（默认 `PinAI/gpt-6`，
effort high，用户 2026-09-19 指令），产出**一律经本仓库工具机器验证后**才入
库/入台账（见记忆与 `数学结构发现工作台/meta/外部模型接入.md` §4/§9 纪律）。

发送前自检（写在代码里，调用前逐条过）：
① 内容是否只含公开知识/自有代码（围棋定式、棋理、本项目脚本）？
② 是否含密钥、个人信息、机器配置、成本配额、用户指令原文、内部协议？
③ 是否属于「未稳定、泄露有实质损失」的内容？——三条任一为否则不发。

key 只从环境变量 `PINAI_API_KEY` 读，不回显、不落盘、不写入产出。

用法：
    python scripts/ask_external.py --task-file data/tmp/prompt_joseki.md \\
        --out data/tmp/external_joseki.md [--model PinAI/gpt-6] [--effort high]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.pinaic.com/v1"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file", required=True, help="提示词/任务书文件")
    ap.add_argument("--out", required=True, help="产出落盘路径")
    ap.add_argument("--model", default="PinAI/gpt-6")
    ap.add_argument("--effort", default="high",
                    choices=("low", "medium", "high"))
    ap.add_argument("--system", default="", help="（可选）system 提示词文件")
    ap.add_argument("--timeout", type=float, default=1800.0)
    args = ap.parse_args()

    key = os.environ.get("PINAI_API_KEY", "")
    if not key:
        print("[X] 环境变量 PINAI_API_KEY 未设置（不回显、不落盘）")
        sys.exit(2)
    task = Path(args.task_file).read_text(encoding="utf-8")
    system = (Path(args.system).read_text(encoding="utf-8")
              if args.system else "")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": task})

    import requests  # 延迟导入

    payload = {
        "model": args.model,
        "reasoning_effort": args.effort,
        "messages": messages,
        "temperature": 0.3,
    }
    print(f"调用 {args.model}（effort={args.effort}，"
          f"输入 {len(task)} 字符）…")
    t0 = time.time()
    r = requests.post(BASE + "/chat/completions", json=payload,
                      headers={"Authorization": "Bearer " + key},
                      timeout=args.timeout)
    if r.status_code != 200:
        print(f"[X] HTTP {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    content = r.json()["choices"][0]["message"]["content"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8", newline="\n")
    print(f"[OK] {time.time() - t0:.0f}s，产出 {len(content)} 字符 → {out}")


if __name__ == "__main__":
    main()
