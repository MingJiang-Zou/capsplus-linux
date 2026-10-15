"""设置窗口的纯逻辑层：**刻意不 import gi**，所以能在没有显示服务器的环境跑单测。

这里只放"界面迟早要算的事"：
  - 覆盖层怎么增删（`set_override` / `delete_override`，返回新表，不改原表）
  - 段名 / 键名 / 动作怎么当场校验（红字文案直接用返回值）
  - keyd 宏的硬边界（单条 `macro(...)` 累计 ≤15 个按键事件）→ 黄字提醒
  - 「应用」按钮的整条流程与状态栏文案（`run_apply`，出错不抛到 GUI 外层）
  - 改动预览（`changed_bindings`：默认渲染 vs 覆盖渲染到底差哪几条）
  - 写盘前把原件快照到 state 目录（焚化炉不充当备份，所以这里不叫删除）

界面（`settings/settings_app.py`）只负责摆控件 + 调这些函数。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import apply as cap_apply
from .mapping_overrides import (
    ALLOWED_SECTIONS, OverrideError, save_overrides, toml_table, validate_action,
)

# keyd 宏硬边界（2026-10-15 用虚拟键盘实测）：一条 macro 累计 **≤15 个按键事件**，
# 超了不是截断而是**整条静默不发**，比写错字更难查，所以界面必须当场黄字提醒。
MACRO_EVENT_LIMIT = 15
_MOD_PREFIXES = ("C-", "S-", "A-", "M-", "G-")

# 「动作」下拉里的常用项（界面把这一列并进"自定义"可编辑框，用户仍可手输任意合法动作）
COMMON_ACTIONS = [
    # 光标 / 选中
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "S-up", "S-down", "S-left", "S-right", "S-home", "S-end", "S-pageup", "S-pagedown",
    "C-left", "C-right", "C-home", "C-end", "S-C-left", "S-C-right", "S-C-home", "S-C-end",
    # 编辑
    "enter", "backspace", "delete", "space", "tab", "esc",
    "C-c", "C-x", "C-v", "C-z", "C-a",
    # 其它
    "noop", "layer(capl)", "macro(up up)",
]


# ---------------------------------------------------------------------------
# 宏长度
# ---------------------------------------------------------------------------
def macro_event_count(action: str) -> int:
    """一条 `macro(...)` 累计多少个**按键事件**（修饰键各算 1 个）。

    `macro(up up)` -> 2；`macro(S-up)` -> 2（按下 Shift + 按下 up）；
    `macro(end S-home backspace)` -> 1+2+1 = 4。非 macro 动作返回 0。
    """
    text = (action or "").strip()
    if not text.startswith("macro(") or not text.endswith(")"):
        return 0
    total = 0
    for token in text[len("macro("):-1].split():
        name = token
        extra = 0
        while len(name) > 2 and name[:2] in _MOD_PREFIXES:
            extra += 1
            name = name[2:]
        total += 1 + extra
    return total


def macro_warning(action: str, limit: int = MACRO_EVENT_LIMIT) -> str:
    """超限返回一句黄字（含具体数字），没超限返回 ""。"""
    count = macro_event_count(action)
    if not count or count <= limit:
        return ""
    return (f"这条宏有 {count} 个按键事件，超过 keyd 的 {limit} 个上限："
            f"实测**整条会被静默丢掉**（不是截断），请拆短或改用 pageup/pagedown")


# ---------------------------------------------------------------------------
# 校验（返回值一律是"一句中文"，"" = 没问题）
# ---------------------------------------------------------------------------
def validate_section(section: str) -> str:
    text = (section or "").strip()
    if not text:
        return "段名不能为空"
    if text not in ALLOWED_SECTIONS:
        return (f"只允许改 {' / '.join(f'[{s}]' for s in ALLOWED_SECTIONS)}；"
                f"[main] 是 Caps 的 overload 声明，改错就进不了层")
    return ""


def validate_key_name(key: str, known_keys: set[str] | None = None) -> str:
    text = (key or "").strip()
    if not text:
        return "键名不能为空"
    if any(ch.isspace() for ch in text):
        return f"键名不能含空格: {text!r}"
    if known_keys is not None and text not in known_keys:
        return f"不认识的键名: {text}（见 keyd.rvaiya list-keys）"
    return ""


def action_problem(action: str, key: str, section: str,
                   known_keys: set[str] | None = None) -> str:
    """一次把"段/键/动作"三样都问一遍，返回第一句毛病（"" = 可以写盘）。"""
    for problem in (validate_section(section), validate_key_name(key, known_keys),
                    validate_action(action, known_keys) or ""):
        if problem:
            return problem
    return ""


# ---------------------------------------------------------------------------
# 覆盖层的增与删（都返回**新表**，原表不动 —— 界面取消时不必回滚）
# ---------------------------------------------------------------------------
def copy_values(values: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {section: dict(table) for section, table in values.items()}


def set_override(values: dict[str, dict[str, str]], section: str, key: str, action: str,
                 known_keys: set[str] | None = None) -> dict[str, dict[str, str]]:
    """新增/改掉一条绑定。非法就抛 ValueError（消息就是界面要显的中文）。"""
    problem = action_problem(action, key, section, known_keys)
    if problem:
        raise ValueError(problem)
    out = copy_values(values)
    out.setdefault(section.strip(), {})[key.strip()] = action.strip()
    return out


def delete_override(values: dict[str, dict[str, str]], section: str,
                    key: str) -> dict[str, dict[str, str]]:
    """「恢复默认」= 把这一条从覆盖层删掉（默认值本来就还在仓库里）。"""
    out = copy_values(values)
    table = out.get(section.strip())
    if not table:
        return out
    table.pop(key.strip(), None)
    if not table:
        del out[section.strip()]
    return out


# ---------------------------------------------------------------------------
# 渲染 diff：默认 vs 默认+覆盖（界面「预览改动」用，绝不碰系统）
# ---------------------------------------------------------------------------
def changed_bindings(before_text: str, after_text: str) -> list[str]:
    """两份 keyd 文本之间**真正变了哪几条**（`[段] 键: 旧 -> 新`）。

    刻意用逐行段头判断而不是 `split("[capl+alt]")`：渲染出的头部注释里也写着
    `[capl]` / `[capl+alt]` 字串，按字串切会切错位置（这条踩过）。
    """
    return [f"[{section}] {key}: {old or '（新增）'} -> {new}"
            for section, key, old, new in binding_deltas(before_text, after_text)]


def binding_deltas(before_text: str, after_text: str):
    """把两份文本解析成 `{(段,键): 动作}` 再比，返回 (段, 键, 旧值, 新值) 列表。"""
    before, after = _binding_map(before_text), _binding_map(after_text)
    out = []
    for pair, new in after.items():
        old = before.get(pair)
        if old != new:
            out.append((pair[0], pair[1], old or "", new))
    for pair, old in before.items():
        if pair not in after:
            out.append((pair[0], pair[1], old, "（删掉）"))
    return out


def _binding_map(text: str) -> dict[tuple[str, str], str]:
    """逐行找段头（`[` 开头且 `]` 结尾的裸行），注释行一律跳过。"""
    table: dict[tuple[str, str], str] = {}
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().strip('"')
            continue
        if "=" not in line:
            continue
        key, _, action = line.partition("=")
        table[(section, key.strip())] = action.strip()
    return table


# ---------------------------------------------------------------------------
# 应用（「应用」按钮的整条流程；任何异常都收成状态栏文本，不抛给 GUI）
# ---------------------------------------------------------------------------
def run_apply(dry_run: bool = False, overrides_path: Path | None = None,
              known_keys: set[str] | None = None) -> tuple[bool, str]:
    """返回 `(是否成功, 状态栏文案)`。文案里带上 OverrideError / journal 错误行。

    dry_run=True 只渲染 + 归一化自检，**不碰 /etc/keyd**（界面自检、单测都走这条）。
    `overrides_path` 指定覆盖层时用**它自己渲染**再走 apply：界面把配置指到临时目录做演练，
    绝不能出现"写的是临时文件、渲染的是 ~/.config 那份"这种两张皮。
    """
    text = None
    if overrides_path is not None:
        from .mapping_overrides import load_and_merge
        from .render import render
        data, overrides = load_and_merge(Path(overrides_path), known_keys)
        if overrides.error:
            return False, f"覆盖层有问题，没有应用：{overrides.path} —— {overrides.error}"
        if overrides.errors:
            return False, ("覆盖层有问题，没有应用：\n  " + "\n  ".join(overrides.errors))
        text = render(data)
    try:
        result = cap_apply.apply(dry_run=dry_run, text=text)
    except OverrideError as exc:
        return False, f"覆盖层有问题，没有应用：\n{exc}"
    except cap_apply.ApplyError as exc:
        # ApplyError 里已经写了"回滚到哪个备份 + 错误行"，原样显出来最有用
        return False, f"应用失败：\n{exc}"
    except Exception as exc:                              # 兜底：按钮不许把窗口带崩
        return False, f"没预料的失败：{type(exc).__name__}: {exc}"
    if dry_run:
        return True, result.log or "试渲染通过，未改动系统"
    tail = result.log.strip().splitlines()[-1] if result.log.strip() else ""
    backup = Path(result.backup).name if result.backup else "无"
    return True, f"已应用并 reload；备份 {backup}" + (f"\n最近日志：{tail}" if tail else "")


# ---------------------------------------------------------------------------
# 写盘前的原件快照（备份目录 = ~/.local/state/capsplus/config-backup/<时间戳>/）
# ---------------------------------------------------------------------------
def snapshot_config(path: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """把要覆盖的原件存一份带时间戳的副本。文件不存在返回 None。

    超出 `keep` 份的旧副本 **mv 进焚化炉**（规矩：不许 rm）。
    """
    source = Path(path)
    if not source.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = Path(backup_dir) / stamp
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / source.name
    shutil.copy2(source, target)
    _prune_snapshots(Path(backup_dir), keep)
    return target


def _prune_snapshots(backup_dir: Path, keep: int) -> None:
    copies = sorted(p for p in backup_dir.iterdir() if p.is_dir()) if backup_dir.is_dir() else []
    stale = copies[:-keep] if keep > 0 else copies
    if not stale:
        return
    grave = cap_apply.attic_dir()
    grave.mkdir(parents=True, exist_ok=True)
    for directory in stale:
        shutil.move(str(directory), str(grave / directory.name))


def save_overrides_with_snapshot(values: dict[str, dict[str, str]], path: Path,
                                 backup_dir: Path) -> Path:
    """覆盖 mappings.toml 前先快照原件，再写（`save_overrides` 自己是原子写）。"""
    snapshot_config(path, backup_dir)
    return save_overrides(values, path)


def override_toml_hint(section: str) -> str:
    """界面上显示 keyd 的段名、写盘走 TOML 的段名 —— 只有两者**不一样**时才值得提示。

    `[capl]` 本来就两边相同，跟着说一句"必须加引号"是纯噪音（实测截图里就刺眼）。
    """
    quoted = toml_table(section)
    if quoted == section:
        return ""
    return f'界面里的 [{section}] 写进文件是 [{quoted}]（带 + 的段名 TOML 必须加引号）'
