# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""apply 的关键路径测试（全部在临时沙盒里，绝不碰 /etc/keyd）。

覆盖两条 2026-10-15 演练抓到的真 bug：
  ① 快照必须在 install 之前 —— 否则备份里存的是新装的（坏）配置；
  ② 裁剪必须在快照之前腾位 —— 否则新快照把"要回滚的那一版"挤出保留窗口，
     previous_backup() 会取到刚做的快照，回滚 = 回滚无效。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import apply as A          # noqa: E402
from capsplus.render import render        # noqa: E402

OLD_CONFIG = "[ids]\n*\n\n[main]\ncapslock = overload(capl, capslock)\n\n[capl]\ne = up\n"


class ApplySandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (A.KEYD_CONF, A.BACKUP_DIR, A.attic_dir,
                       A.install_conf, A.reload_keyd, A.keyd_log, A._run,
                       A.render_with_overrides)
        A.KEYD_CONF = root / "capslock-plus.conf"
        A.BACKUP_DIR = root / "backup"
        A.attic_dir = lambda: root / "attic"
        # 渲染一律钉在仓库默认上：绝不读真实的 ~/.config/capsplus/mappings.toml
        # （否则本机改过的键位会把测试搞红，测试也就没法复现了）
        self._real_render_with_overrides = self._saved[7]
        A.render_with_overrides = render
        self.logs: list[str] = []
        self.reloads = 0
        self.real_run = self._saved[6]

        def fake_install(text):
            A.KEYD_CONF.write_text(text, encoding="utf-8")

        def fake_reload():
            self.reloads += 1

        def fake_log(since="10 seconds ago"):
            return self.logs.pop(0) if self.logs else ""

        A.install_conf = fake_install
        A.reload_keyd = fake_reload
        A.keyd_log = fake_log
        # 回滚里的恢复动作是 `sudo cp`：沙盒里换成普通 cp（源就是沙盒里的备份）
        A._run = lambda cmd, need_root=False, check=True, input_text=None: self.real_run(
            cmd, False, check, input_text)

    def tearDown(self):
        (A.KEYD_CONF, A.BACKUP_DIR, A.attic_dir,
         A.install_conf, A.reload_keyd, A.keyd_log, A._run,
         A.render_with_overrides) = self._saved
        self.tmp.cleanup()

    def backups(self):
        return sorted(p.name for p in A.BACKUP_DIR.glob("capslock-plus.conf.*"))


class TestApplyHappyPath(ApplySandbox):
    def test_first_apply_snapshots_previous_config(self):
        A.KEYD_CONF.write_text(OLD_CONFIG, encoding="utf-8")
        self.logs.append("keyd[1]: CONFIG: parsing ...\n")
        result = A.apply()
        self.assertTrue(result.ok)
        self.assertEqual(len(self.backups()), 1)
        saved = A.BACKUP_DIR / self.backups()[0]
        self.assertEqual(saved.read_text(encoding="utf-8"), OLD_CONFIG)

    def test_no_snapshot_when_config_absent(self):
        self.logs.append("clean\n")
        result = A.apply()
        self.assertIsNone(result.backup)
        self.assertEqual(self.backups(), [])


class TestRollback(ApplySandbox):
    def test_rollback_restores_previous_config(self):
        """两次 apply 的内容必须不同，才能验出"回滚到出错前那一版"。"""
        NEW_CONFIG = "[ids]\n*\n\n[main]\ncapslock = overload(capl, capslock)\n\n[capl]\nf = right\n"
        A.KEYD_CONF.write_text(OLD_CONFIG, encoding="utf-8")
        self.logs.append("clean\n")
        self.assertTrue(A.apply(text=NEW_CONFIG).ok)
        self.assertEqual(A.KEYD_CONF.read_text(encoding="utf-8"), NEW_CONFIG)

        BAD_CONFIG = NEW_CONFIG + "oops = nonexistent_key_action\n"  # 过得了渲染器自检，keyd 会拒
        self.logs.append("keyd[1]:     ERROR: line 7: invalid key or action\n")
        with self.assertRaises(A.ApplyError) as ctx:
            A.apply(text=BAD_CONFIG)
        self.assertIn("已回滚到上一版备份", str(ctx.exception))
        self.assertIn("line 7", str(ctx.exception))
        # 关键断言：回滚回去的是**出错前正在用的那一版**，不是刚装进去的坏配置
        self.assertEqual(A.KEYD_CONF.read_text(encoding="utf-8"), NEW_CONFIG)
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(self.reloads, 3)  # apply1 + apply2 + 回滚后

    def test_rollback_without_previous_config_removes_bad_file(self):
        self.logs.append("ERROR: line 3: bad action\n")
        with self.assertRaises(A.ApplyError) as ctx:
            A.apply()
        self.assertIn("没有可回滚的备份", str(ctx.exception))
        self.assertFalse(A.KEYD_CONF.exists())


class TestRetention(ApplySandbox):
    def test_keep_window_survives_snapshot(self):
        """连着 apply 12 次，备份数不超过 KEEP_BACKUPS，且最新一份是上一版配置。"""
        A.KEYD_CONF.write_text(OLD_CONFIG, encoding="utf-8")
        for i in range(A.KEEP_BACKUPS + 2):
            A.KEYD_CONF.write_text(f"# 第 {i} 版\n" + OLD_CONFIG, encoding="utf-8")
            self.logs.append("clean\n")
            self.assertTrue(A.apply().ok)
        self.assertLessEqual(len(self.backups()), A.KEEP_BACKUPS)
        newest = A.BACKUP_DIR / self.backups()[-1]
        self.assertIn("第 11 版", newest.read_text(encoding="utf-8"))  # 循环跑到 i=11

    def test_pruned_backups_go_to_attic(self):
        A.KEYD_CONF.write_text(OLD_CONFIG, encoding="utf-8")
        for i in range(A.KEEP_BACKUPS + 2):
            self.logs.append("clean\n")
            A.apply()
        attic = A.attic_dir()
        moved = list(attic.glob("capslock-plus.conf.*")) if attic.exists() else []
        self.assertGreater(len(moved), 0, "被裁掉的备份应 mv 进焚空炉，不许 rm")


class TestDryRun(ApplySandbox):
    def test_dry_run_touches_nothing(self):
        A.KEYD_CONF.write_text(OLD_CONFIG, encoding="utf-8")
        result = A.apply(dry_run=True)
        self.assertFalse(result.reloaded)
        self.assertEqual(A.KEYD_CONF.read_text(encoding="utf-8"), OLD_CONFIG)
        self.assertEqual(self.backups(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
