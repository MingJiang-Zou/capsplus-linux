# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""结果面板（第二批）：OCR 文本 / 翻译两栏 + 后端标签 + 可复制 + 一键重跑到翻译。

纯逻辑（`PanelModel`）顶层**不 import gi**，判据直接调它；GTK 部分在 `_gtk()` 里懒加载。
三条口径（判据钉住，别改）：

  1. **后端必须一眼看清**（`本地` / `云`）—— 用户要能看出这一下花的是谁的钱；
  2. **出错要给可操作的下一步**（谁没起、没网怎么办），不是一句"失败了"；
  3. **"送去翻译"永远用 OCR 的原文**，不会串到译文上（重跑同一份原文，可反复按 F2）。
"""

from __future__ import annotations

KIND_OCR = "ocr"
KIND_TRANSLATE = "translate"
KINDS = (KIND_OCR, KIND_TRANSLATE)

BACKEND_LOCAL = "local"
BACKEND_CLOUD = "cloud"
BACKEND_LABELS = {BACKEND_LOCAL: "本地", BACKEND_CLOUD: "云"}

TITLE = "识别 / 翻译"
COPY_LABEL = "复制"
RETRANSLATE_LABEL = "送去翻译"
SWITCH_LABEL = "切到{other}"

PANEL_HINTS = {
    KIND_OCR: "F4 识别文字 · 可复制 · 可一键送去翻译",
    KIND_TRANSLATE: "F2 翻译 · 原文/译文可切 · 可一键切本地/云",
}


def backend_label(backend: str) -> str:
    """后端的中文标签。认不出来就原样返回 —— 不编一个假的"本地"。"""
    return BACKEND_LABELS.get(backend, backend or "未指定")


def other_backend(backend: str) -> str:
    return BACKEND_CLOUD if backend == BACKEND_LOCAL else BACKEND_LOCAL


class PanelModel:
    """面板状态。所有字段都能被判据直接断言，不需要窗口。"""

    def __init__(self):
        self.kind = None
        self.backend = ""
        self.busy = False
        self.message = ""          # 出错时的话术（可操作的那句）
        self.note = ""             # 引擎附注（模型名 / 耗时 / 额度）
        self.ocr_text = ""
        self.src_text = ""
        self.dst_text = ""
        self.ms = 0
        self.view = KIND_OCR       # 两栏可切：看原文还是看译文

    # ---- 状态迁移 ----
    def start(self, kind: str, backend: str) -> None:
        assert kind in KINDS, f"未知面板类型 {kind}"
        self.kind = kind
        self.backend = backend
        self.busy = True
        self.message = ""
        if kind == KIND_TRANSLATE:
            self.view = KIND_TRANSLATE

    def set_ocr(self, text: str, backend: str, ms: int = 0, note: str = "") -> None:
        self.kind = KIND_OCR
        self.backend = backend
        self.busy = False
        self.ms = int(ms)
        self.note = note
        self.message = ""
        self.ocr_text = text or ""
        self.view = KIND_OCR

    def set_translation(self, src: str, dst: str, backend: str, ms: int = 0, note: str = "") -> None:
        self.kind = KIND_TRANSLATE
        self.backend = backend
        self.busy = False
        self.ms = int(ms)
        self.note = note
        self.message = ""
        self.src_text = src or ""
        self.dst_text = dst or ""
        self.view = KIND_TRANSLATE

    def set_error(self, message: str, note: str = "") -> None:
        self.busy = False
        self.message = message or ""
        self.note = note

    # ---- 读 ----
    def headline(self) -> str:
        """一行标题：干的是什么 · 走的哪条后端 · 多久 · 附注。"""
        if not self.kind:
            return TITLE
        what = {"ocr": "OCR 识别", "translate": "翻译"}[self.kind]
        parts = [f"{what} · {backend_label(self.backend)}"]
        if self.ms:
            parts.append(f"{self.ms} ms")
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)

    def body(self) -> str:
        """当前该显示在正文里的文本（`view` 决定看原文还是译文）。"""
        if self.kind == KIND_TRANSLATE:
            return self.dst_text if self.view == KIND_TRANSLATE else self.src_text
        return self.ocr_text

    def primary_text(self) -> str:
        """复制按钮该复制哪一段：有译文优先译文，否则原文（别让人复制到空气）。"""
        if self.dst_text:
            return self.dst_text
        return self.ocr_text or self.src_text

    def translate_input(self) -> str:
        """重跑翻译用的原文：**只认 OCR 原文**，串不到译文上（口径 ③）。"""
        return self.ocr_text

    def can_retranslate(self) -> bool:
        return bool(self.ocr_text.strip())

    def can_copy(self) -> bool:
        return bool(self.primary_text().strip())

    def hints(self) -> str:
        return PANEL_HINTS.get(self.kind, TITLE)

    def switch_label(self) -> str:
        return SWITCH_LABEL.format(other=backend_label(other_backend(self.backend)))

    def view_label(self) -> str:
        """两栏切换按钮的文字：显示的是"切过去会看到什么"。"""
        if self.kind != KIND_TRANSLATE:
            return ""
        return "看原文" if self.view == KIND_TRANSLATE else "看译文"


# ------------------------------------------------------------------------------ GTK 部分

def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    return Gtk, Gdk, GLib


class PanelView:
    """面板的窗口部分：除 `build()`/`update()` 外不碰 gi（判据能用假的 model 直接调）。"""

    def __init__(self, on_copy=None, on_retranslate=None, on_switch=None, on_toggle_view=None,
                 clip=None):
        self.on_copy = on_copy
        self.on_retranslate = on_retranslate
        self.on_switch = on_switch
        self.on_toggle_view = on_toggle_view
        self.model = PanelModel()
        self.widget = None
        self.collapsed = False

    def build(self):
        """结果抽屉：**标题行（后端 + 耗时）+ 右上角复制 + 可折叠**，与工具条分层（不挤一行）。"""
        Gtk, _Gdk, _GLib = _gtk()
        from capsplus.shot_icons import render_icon
        from capsplus import shot_style

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for cls in shot_style.class_names_for("drawer").split():
            box.get_style_context().add_class(cls)
        box.set_margin_top(3)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_bottom(3)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._headline = Gtk.Label(xalign=0)                    # 标题行：后端 + 耗时
        self._headline.get_style_context().add_class("cp-drawer-title")
        header.pack_start(self._headline, True, True, 0)
        self._copy = Gtk.Button()                               # 右上角：复制（主按钮）
        # 别写「Ctrl+C」：Ctrl+C 在标注窗里复制的是**图片**，这里复制的是**这段文字**。
        # 2026-10-20 用户报"点复制按钮复制不出来"时，那行 tooltip 也在误导排查方向。
        self._copy.set_tooltip_text("复制这段文字")
        # 图标颜色取**主题的选中态前景色**（写死白色在浅色主题下就是一块空方块 —— 判据/窗口图抓到过）
        self._copy.set_image(Gtk.Image.new_from_surface(
            render_icon("copy", 16, shot_style.readable_colour(self._copy))))
        for cls in shot_style.class_names_for("primary").split():
            self._copy.get_style_context().add_class(cls)
        self._copy.connect("clicked", lambda *_a: self.on_copy and self.on_copy(self.model))
        header.pack_end(self._copy, False, False, 0)
        self._collapse = Gtk.Button(label="▾")                  # 折叠（标题行上，不占地方）
        self._collapse.set_tooltip_text("收起 / 展开结果")
        for cls in shot_style.class_names_for("action").split():
            self._collapse.get_style_context().add_class(cls)
        self._collapse.connect("clicked", lambda *_a: self.set_collapsed(not self.collapsed))
        header.pack_end(self._collapse, False, False, 0)
        box.pack_start(header, False, False, 0)

        # 正文用可选中、可滚动的 TextView：**不是为了好看**，是为了让人能手动框选复制
        self._scroller = Gtk.ScrolledWindow()
        # **高度跟着内容走**（lead：两行文字却留一大片空白，看着像坏了）：
        # 最小给两行的高度、自然高度跟随内容、超过上限就滚动。
        self._scroller.set_min_content_height(48)
        try:
            self._scroller.set_max_content_height(200)
            self._scroller.set_propagate_natural_height(True)
        except Exception:                     # 老 GTK 没有这两个 API 就算了（不抛）
            pass
        self._text = Gtk.TextView()
        self._text.set_editable(False)
        self._text.set_cursor_visible(False)
        self._text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text.get_style_context().add_class("cp-drawer-text")
        self._scroller.add(self._text)
        box.pack_start(self._scroller, True, True, 0)

        self._footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._message = Gtk.Label(xalign=0)                     # 出错/说明：可操作的那句
        self._message.set_line_wrap(True)
        self._footer.pack_start(self._message, True, True, 0)
        self._toggle = Gtk.Button()                             # 原文/译文可切
        self._toggle.connect("clicked", lambda *_a: self.on_toggle_view and self.on_toggle_view())
        self._retranslate = Gtk.Button(label=RETRANSLATE_LABEL)
        self._retranslate.connect("clicked",
                                  lambda *_a: self.on_retranslate and self.on_retranslate(self.model))
        self._switch = Gtk.Button()
        self._switch.connect("clicked", lambda *_a: self.on_switch and self.on_switch(self.model))
        for btn in (self._toggle, self._retranslate, self._switch):
            self._footer.pack_end(btn, False, False, 0)
        box.pack_start(self._footer, False, False, 0)
        self.widget = box
        self.update(self.model)
        return box

    def set_collapsed(self, collapsed: bool) -> bool:
        """折叠：只留标题行（标题行永远在 —— 后端与耗时必须随时看得见）。"""
        self.collapsed = bool(collapsed)
        if self.widget is None:
            return self.collapsed
        for part in ("_scroller", "_footer"):
            widget = getattr(self, part, None)
            if widget is not None:
                widget.set_visible(not self.collapsed)
        if getattr(self, "_collapse", None) is not None:
            self._collapse.set_label("▸" if self.collapsed else "▾")
        return self.collapsed

    def set_text(self, text: str) -> None:
        _Gtk, _Gdk, _GLib = _gtk()
        self._text.get_buffer().set_text(text or "")

    def update(self, model: PanelModel) -> None:
        self.model = model
        if self.widget is None:
            return
        self._headline.set_text(model.headline())
        self.set_text(model.body())
        self._message.set_text(model.message)
        self._copy.set_sensitive(model.can_copy())
        self._retranslate.set_sensitive(model.can_retranslate())
        self._switch.set_label(model.switch_label() if model.backend else SWITCH_LABEL.format(other="云"))
        self._switch.set_sensitive(bool(model.backend))
        self._toggle.set_label(model.view_label())
        self._toggle.set_visible(model.kind == KIND_TRANSLATE)