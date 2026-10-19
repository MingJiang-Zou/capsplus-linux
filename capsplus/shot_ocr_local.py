#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""本地 OCR 的**助手脚本**（在独立 venv 里跑，只依赖 rapidocr + onnxruntime）。

为什么要单独一个进程 / 单独一个 venv（2026-10-19 定的）：
  * OCR 那套依赖（onnxruntime + opencv，几百 MB）**不该塞进 capsplus 主进程** —— 那会让
    "截图"这个天天用的工具背上一个重依赖，装不上就整条链路不可用；
  * 更不能去动 `~/voice-svc/venv`（语音服务的窝）—— 换掉那儿的 numpy/onnx 版本会伤到别人；
  * 所以：本地 OCR = `capsplus/shot_ocr.py` 用子进程调本文件，本文件只 import rapidocr。

协议（**只认 stdout 最后一行 JSON**，前后可以有引擎自己的日志）：
    {"ok": true, "text": "识别到的文字\n按行", "ms": 123, "engine": "rapidocr_onnxruntime 1.2.3"}
    {"ok": false, "hint": "✗ …（一句可操作的话）"}

用法：`<ocr-venv>/bin/python capsplus/shot_ocr_local.py /path/to.png`
"""

import json
import sys
import time


def main(argv) -> int:
    out = {"ok": False, "hint": "✗ 没给图片路径"}
    if len(argv) < 1:
        print(json.dumps(out, ensure_ascii=False))
        return 2
    path = argv[0]
    t0 = time.perf_counter()
    try:
        # 只 import 这一个重依赖：本脚本在 OCR venv 里跑，主进程从不 import 它
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:
        print(json.dumps({"ok": False,
                          "hint": f"✗ 这个 venv 里没有 rapidocr：{exc}（先 pip install rapidocr-onnxruntime）"},
                         ensure_ascii=False))
        return 3
    try:
        engine = _ENGINE()
        result, _elapse = engine(path)
    except Exception as exc:
        print(json.dumps({"ok": False, "hint": f"✗ OCR 引擎报错：{exc}"}, ensure_ascii=False))
        return 4
    text = "\n".join(line[1] for line in (result or []) if len(line) > 1 and line[1])
    ms = int((time.perf_counter() - t0) * 1000)
    print(json.dumps({"ok": True, "text": text, "ms": ms, "engine": _VERSION},
                     ensure_ascii=False))
    return 0


_ENGINE_CACHE = {}
_VERSION = "rapidocr-onnxruntime"


def _ENGINE():
    """引擎只建一次（建一次 ~1 秒，之后每张图就是纯推理）。"""
    if "e" not in _ENGINE_CACHE:
        from rapidocr_onnxruntime import RapidOCR
        try:
            from importlib.metadata import version
            global _VERSION
            _VERSION = f"rapidocr-onnxruntime {version('rapidocr-onnxruntime')}"
        except Exception:
            pass
        _ENGINE_CACHE["e"] = RapidOCR()
    return _ENGINE_CACHE["e"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))