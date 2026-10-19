# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""把 OCR / 翻译接到标注窗（第二批 F2/F4 的**接线层**）。

标注窗（`shot_annotate`）只认两个回调：`on_ocr(surface) -> dict` 与 `on_translate(text) -> dict`
（dict 的形状与 `shot_annotate.normalize_result` 的约定一致）。本模块负责把"窗里的画面"变成
后端能吃的 PNG，并按后端偏好分派。分开写的理由：`shot_annotate` 不该知道百度/本机模型/venv 的事。

三件事在这里定：
  1. **画面 → PNG**：OCR/翻译看的是**带标注的画面**（`Annotator.rendered()` 给的 surface），
     落成 PNG 塞进 `$XDG_RUNTIME_DIR/capsplus-shot/`（tmpfs，注销即清）。
  2. **后端偏好**：`TextProviders.ocr_backend / translate_backend`（None = auto）。面板上的
     「切到云/切到本地」翻的就是它 —— 下一次按 F2/F4 立刻换后端，不用退窗。
  3. **云直读图的快捷路径**：本机模型只吃文本（实测 `vision:false`），而云端多模态能**直接读图**。
     所以按 F2 时若"还没有 OCR 原文"、而当前偏好是云 → 直接把图发过去（跳过 OCR，一步出译文）。
"""

from __future__ import annotations

import time
from pathlib import Path

from capsplus import shot_ocr as O
from capsplus import shot_translate as T
from capsplus.shot_panel import BACKEND_CLOUD, other_backend


def runtime_dir() -> Path:
    return O.runtime_dir()


class TextProviders:
    """F2/F4 的真实现。判据注入假的 backend 函数即可全程离线。"""

    def __init__(self, ocr_fn=None, translate_fn=None, translate_image_fn=None,
                 out_dir=None, name_prefix="text"):
        self.ocr_fn = ocr_fn or O.ocr
        self.translate_fn = translate_fn or T.translate
        self.translate_image_fn = translate_image_fn or T.translate_image
        self.out_dir = Path(out_dir) if out_dir else runtime_dir()
        self.name_prefix = name_prefix
        self.ocr_backend = None          # None = auto（由 shot_ocr 决定）
        self.translate_backend = None
        self.saved = []                  # 落过盘的 PNG（排查用，也方便判据断言）

    # ---- 画面落盘 ----
    def png_of(self, surface, tag="frame") -> str:
        from capsplus.shot_annotate import png_bytes
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{self.name_prefix}-{tag}-{int(time.time() * 1000)}.png"
        path.write_bytes(png_bytes(surface))
        self.saved.append(str(path))
        return str(path)

    # ---- 标注窗要的两个回调 ----
    def on_ocr(self, surface) -> dict:
        path = self.png_of(surface, "ocr")
        return self.ocr_fn(path, self.ocr_backend)

    def on_translate(self, text) -> dict:
        return self.translate_fn(text, self.translate_backend)

    def on_translate_image(self, surface) -> dict:
        """云端直读图（跳过 OCR）。"""
        path = self.png_of(surface, "translate")
        return self.translate_image_fn(path)

    def can_read_images(self) -> bool:
        """当前翻译偏好支不支持直读图（只有云支持；本机模型实测只吃文本）。"""
        return self.translate_backend == BACKEND_CLOUD

    # ---- 面板上的「切到云/本地」 ----
    def switch_translate(self, current: str) -> str:
        self.translate_backend = other_backend(current) if current else BACKEND_CLOUD
        return self.translate_backend

    def switch_ocr(self, current: str) -> str:
        self.ocr_backend = other_backend(current) if current else BACKEND_CLOUD
        return self.ocr_backend

    def status(self) -> str:
        return (f"OCR 偏好：{self.ocr_backend or 'auto'}\n"
                f"翻译偏好：{self.translate_backend or 'auto'}")