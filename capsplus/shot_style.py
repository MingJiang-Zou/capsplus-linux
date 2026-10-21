# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""标注窗的设计语言（**唯一一份**）：GTK3 的 CSS + 类名清单。

用户的反馈是"太丑、易用性有待提高"，根因之一是**没写样式**：全是裸原生按钮（无图标、无分组、
选中态只是"稍微深一点的灰"）。所以这里把外观收成一份 CSS，规则只有三条：

  1. **跟随 GNOME 深浅色主题**：颜色一律用主题变量（`@theme_bg_color` / `@theme_fg_color` /
     `@theme_selected_bg_color`）配 alpha，不写死 RGB —— 深色主题下自动变深色，不用两套。
  2. **每个交互控件都有 hover / active / 选中 三态**（判据扫"用到几个类就定义几个"，漏一个就红）。
  3. **主次分明**：主按钮（复制）用 `cp-btn-primary` 填色，危险/次要动作（关闭）低调。

类名清单见 `used_classes()` / `class_names_for()`；`tests/test_shot_style.py` 会拿代码里真正
`add_class(...)` 的名字与这里定义的名字对账。
"""

from __future__ import annotations

import re

# 选中态唯一的那个类名（工具/颜色/线宽共用；判据断言三者取到的激活类必须一致）
ACTIVE_CLASS = "cp-active"

# 控件尺寸（一处定义，判据与布局都用它，免得两边对不上）
TOOL_BTN = 30          # 工具按钮边长
SWATCH = 22            # 颜色圆点直径
WIDTH_DOT = 22         # 线宽圆点直径
BAR_PAD = 5            # 工具条内边距
BAR_GAP = 8            # 工具条与图像之间的空隙
BAR_RADIUS = 10
STATUS_FONT = 12

CSS = """
.cp-window { background-color: alpha(@theme_bg_color, 0.98); }

/* 画布四周的中性底色：衬托截图，不抢内容 */
.cp-canvas { background-color: #1b1b1e; }

/* 一条主工具条：贴近图像、不遮挡图像 */
.cp-bar {
  background-color: alpha(@theme_bg_color, 0.96);
  border: 1px solid alpha(@theme_fg_color, 0.18);
  border-radius: 10px;
  padding: 5px;
  box-shadow: 0 2px 10px alpha(black, 0.35);
}

/* 普通图标按钮：三态齐全 */
.cp-btn { min-width: 30px; min-height: 30px; padding: 3px; border-radius: 7px;
          background-color: transparent; border: 1px solid transparent; }
.cp-btn:hover { background-color: alpha(@theme_fg_color, 0.12); }
.cp-btn:active { background-color: alpha(@theme_fg_color, 0.22); }
.cp-btn:disabled { background-color: transparent; }
.cp-btn:disabled image { opacity: 0.35; }


/* 主按钮（复制）：整条工具条上最显眼的一个 */
.cp-btn-primary { background-color: alpha(@theme_selected_bg_color, 0.95);
                  border-color: alpha(@theme_fg_color, 0.45); }
.cp-btn-primary:hover { background-color: @theme_selected_bg_color; }

/* 组间细分隔线 */
.cp-sep { background-color: alpha(@theme_fg_color, 0.22); min-width: 1px; margin: 4px 3px; }

/* 颜色圆点 / 线宽圆点：选中加描边 */
.cp-swatch { min-width: 22px; min-height: 22px; border-radius: 50%;
             border: 2px solid alpha(@theme_fg_color, 0.25); padding: 0; }
.cp-swatch:hover { border-color: alpha(@theme_fg_color, 0.65); }

.cp-dot { min-width: 22px; min-height: 22px; border-radius: 50%; padding: 0;
          border: 2px solid transparent; background-color: transparent; }
.cp-dot:hover { border-color: alpha(@theme_fg_color, 0.45); }

/* 结果抽屉（识别/翻译）：单独一层，**不许**和工具条挤在一行 */
.cp-drawer { background-color: alpha(@theme_bg_color, 0.97);
             border: 1px solid alpha(@theme_fg_color, 0.18); border-radius: 10px; }
.cp-drawer-title { font-weight: bold; font-size: 12px; }
.cp-drawer-text { font-size: 12px; }
.cp-drawer-mono { font-family: monospace; font-size: 12px; }

/* 状态栏：字号别缩到看不清，对比度够；**常用键一档、其余收成次要**，行距与分区间隔都放大 */
.cp-status { font-size: 13px; padding: 6px 10px; color: alpha(@theme_fg_color, 0.95); }
.cp-status-main { font-size: 13px; padding: 6px 10px 2px 10px; color: alpha(@theme_fg_color, 0.98); }
.cp-status-more { font-size: 11px; padding: 0 10px 6px 10px; color: alpha(@theme_fg_color, 0.62); }
.cp-status-warn { font-size: 13px; padding: 6px 10px; color: #f5c211; }

/* ⚠ **必须放在最后**：`.cp-swatch` / `.cp-dot` 里写了 `background-color: transparent`，
   GTK CSS 同权重时**后写的赢** —— 放在前面的话选中态的底色会被盖掉（实测：看着像"只有描边没底色"）。

   **选中态只有一套语言**：这个类同时给工具、颜色、线宽用（lead 要求"同一个类或同一套 CSS 规则"）。
   底色 + 描边是共用部分；颜色/线宽的中心实心点是它们自己的填充（外圈 + 中心点 = 同族语言）。*/
.cp-active { background-color: alpha(@theme_selected_bg_color, 0.90);
             border-color: alpha(@theme_fg_color, 0.85);
             border-width: 2px; }


"""


def luminance(rgba) -> float:
    """感知亮度（0=黑 1=白）。"""
    r, g, b = rgba[0], rgba[1], rgba[2]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def theme_colour(widget, selected: bool = False) -> tuple:
    """取当前主题的前景色（4 个 float）。

    `selected=True` 取"选中态前景色"。⚠ **别拿它当主按钮图标的颜色**：某些主题下它与主按钮底色
    同亮（白压白 = 看不见的按钮，2026-10-19 窗口图上一眼看到）—— 用 `readable_colour()`。
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        flags = Gtk.StateFlags.SELECTED if selected else Gtk.StateFlags.NORMAL
        rgba = widget.get_style_context().get_color(flags)
        alpha = rgba.alpha if rgba.alpha else 1.0
        return (rgba.red, rgba.green, rgba.blue, alpha)
    except Exception:
        return (1.0, 1.0, 1.0, 1.0) if selected else (0.12, 0.12, 0.12, 1.0)


def background_luminance(widget, fallback: float = 0.5) -> float:
    """控件**背后那块底色**的亮度（透明就往上找窗口底色，量不到用 fallback）。

    给判据用：图标颜色必须与它拉开差距 —— 白图标压白底就是"看不见的按钮"。
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        node = widget
        while node is not None:
            ctx = node.get_style_context()
            for state in (Gtk.StateFlags.NORMAL, Gtk.StateFlags.SELECTED):
                bg = ctx.get_background_color(state)
                if bg.alpha > 0.05:
                    return luminance((bg.red, bg.green, bg.blue))
            node = node.get_parent() if hasattr(node, "get_parent") else None
        return fallback
    except Exception:
        return fallback


def readable_colour(widget, fallback=None) -> tuple:
    """**按控件背景的明暗**挑图标颜色：浅底给深图标、深底给浅图标。

    为什么不赌主题变量（第一版赌了）：主按钮底色来自 `@theme_selected_bg_color`，某些主题/无主题
    环境下前景色与底色同亮 ⇒ 白图标压白底，渲染出来就是个空方块。这里直接量背景亮度再反着挑，
    任何主题都成立；量不到就用调用方给的兜底。
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        ctx = widget.get_style_context()
        for state in (Gtk.StateFlags.NORMAL, Gtk.StateFlags.SELECTED):
            bg = ctx.get_background_color(state)
            if bg.alpha > 0.05:
                return (0.08, 0.08, 0.10, 1.0) if luminance((bg.red, bg.green, bg.blue)) > 0.5 \
                    else (0.97, 0.97, 0.97, 1.0)
        return fallback or theme_colour(widget)
    except Exception:
        return fallback or (0.12, 0.12, 0.12, 1.0)


def used_classes() -> set:
    """CSS 里定义了样式的类名（`.cp-xxx`）。"""
    return set(re.findall(r"\.(cp-[a-z0-9-]+)", CSS))


def class_names_for(kind: str, active: bool = False, extra: str = "") -> str:
    """组装一个控件的类名串：`cp-btn` / `cp-btn cp-active` / …（激活类只有 `cp-active` 一个）。

    ⚠ 类名**只在 `CSS` 里定义一次**（判据对账），这里只做选择，不新增字面量。
    """
    table = {"tool": "cp-btn", "action": "cp-btn", "primary": "cp-btn cp-btn-primary",
             "swatch": "cp-swatch", "dot": "cp-dot", "bar": "cp-bar",
             "drawer": "cp-drawer", "status": "cp-status", "canvas": "cp-canvas",
             "window": "cp-window", "sep": "cp-sep"}
    classes = table.get(kind, "cp-btn").split()
    if active and kind in ("tool", "action", "primary", "swatch", "dot"):
        classes.append(ACTIVE_CLASS)          # 三种选中态**共用同一个类**（判据钉死）
    if extra:
        classes.extend(extra.split())
    return " ".join(classes)


def install(screen=None, provider_factory=None) -> bool:
    """把这份 CSS 装到屏幕上（跟随主题）。返回是否装上（无 GTK / 失败都返回 False，不抛）。

    用 `Gtk.StyleContext.add_provider_for_screen` + `STYLE_PROVIDER_PRIORITY_APPLICATION`：
    只影响本应用，不动系统主题。
    """
    try:
        import gi
        # ⚠ **Gdk 必须显式要求 3.0**：只 require Gtk 3.0 的话，`from gi.repository import Gdk`
        # 会解析到 **Gdk 4.0** ⇒ `ValueError: Namespace Gdk is already loaded with version 4.0`
        # ⇒ 这里被 except 吞掉、返回 False ⇒ **整份设计语言一个字符都没装上**（判据抓到过）。
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk
        screen = screen or Gdk.Screen.get_default()
        if screen is None:
            return False
        provider = provider_factory() if provider_factory else Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        return True
    except Exception:
        return False


# 常用键（大一点、亮一点）与其余键（次要一档）—— 信息层次，见 status_text
PRIMARY_KEYS = ("Enter 复制并关", "Esc 关", "Ctrl+C 复制", "Ctrl+S 存盘", "Ctrl+Z 撤销")
SECONDARY_KEYS = ("F2 翻译", "F3 贴图", "F4 OCR", "Ctrl+Y 重做",
                  "方向键微调（Shift=10px）", "Del 删掉选中", "右键菜单")


def status_text(focused: bool = True, selected: str = "", message: str = "",
                picked: str = "", tool: str = "", hover: str = "") -> str:
    """状态栏**主行**：消息 · 当前工具（或鼠标悬停的那个按钮）· 选中了什么 · 常用键（大一点）。

    `tool` / `hover` 由 `shot_annotate` 用**它自己那份表**算好传进来，这里不另抄一份键位：
    用户说"工具栏有几个按钮不知道什么意思" ⇒ 悬停在哪个按钮上，状态栏就把那个按钮的
    "名字 + 快捷键"写出来，光靠图标认不出的按钮（比如吸管）也能当场看明白（2026-10-20）。
    ⚠ 与 `status_more()` 分两档显示（lead："太密、层次没出来"）：常用键给主行，其余键收成次要那行。
    """
    parts = []
    if message:
        parts.append(message)
    parts.append(f"悬停：{hover}" if hover else (f"工具：{tool}" if tool else ""))
    parts.append(f"已选：{selected}" if selected else "未选中标注（点一下标注就能拖/改）")
    if picked:                                   # 吸管取到的颜色（提取颜色那条功能）
        parts.append(f"取色 {picked}")
    if not focused:
        parts.append("窗口没有焦点：F2/F3/F4 现在**不会**生效 —— 点一下窗口再按")
    parts.append("　·　".join(PRIMARY_KEYS))
    return "　·　".join(p for p in parts if p)


def status_more(focused: bool = True, extra: str = "") -> str:
    """状态栏**次要那行**：其余键（小一号、暗一档），外加 `extra`（工具/颜色/线宽的键位）。"""
    return "　·　".join(p for p in ("　·　".join(SECONDARY_KEYS), extra) if p)