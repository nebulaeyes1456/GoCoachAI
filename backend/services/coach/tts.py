"""语音合成（edge-tts，微软免费在线 TTS）。

人设预设：
- gentle（温柔大姐姐）：晓晓，语速稍慢、音调柔和；
- tsundere（傲娇小萝莉）：晓伊，语速快、音调上扬；
- default：晓晓原声。

依赖 edge-tts 与 aiohttp（离线时由调用方降级提示）。
"""
from __future__ import annotations

import asyncio
import io

import edge_tts

PERSONAS = {
    "gentle": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-8%", "pitch": "+2Hz"},
    "tsundere": {"voice": "zh-CN-XiaoyiNeural", "rate": "+12%", "pitch": "+18Hz"},
    "default": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz"},
}

PERSONA_LABELS = {
    "gentle": "温柔大姐姐",
    "tsundere": "傲娇小萝莉",
    "default": "标准女声",
}


def synthesize(text: str, persona: str = "default", max_len: int = 6000) -> bytes:
    """文本 → mp3 字节流。过长文本截断（防滥用）。"""
    cfg = PERSONAS.get(persona, PERSONAS["default"])
    text = (text or "").strip()[:max_len]
    if not text:
        raise ValueError("待合成文本为空")
    buf = io.BytesIO()

    async def run() -> None:
        tts = edge_tts.Communicate(
            text, cfg["voice"], rate=cfg["rate"], pitch=cfg["pitch"]
        )
        async for chunk in tts.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])

    asyncio.run(run())
    return buf.getvalue()
