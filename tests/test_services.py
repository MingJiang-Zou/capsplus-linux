# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""服务配置层（`capsplus/services.py`）的判据。

要守住的六条：
  ① **读写往返**：存进去再读回来，端点、顺序、模式、语言一个字都不许变；
  ② **坏 TOML 不抛**：语法错 → 带 error 的配置对象，界面照常能开（老规矩：状态带动作）；
  ③ **写回后权限 0600**：文件里可能有明文凭据，0600 是底线；
  ④ **脱敏**：任何输出里都不许出现 key 的**值**（只说"来自哪"）；
  ⑤ **优先级**：环境变量 > 凭据文件 > env 文件 > 明文（模块头第 ② 条）；
  ⑥ **auto 按书写顺序**：配置里端点的先后 = 尝试的先后。

**每条判据都带反例具型**（真注入故障、判据真红），不是"结构绿"：
  ① 写回不 chmod 0600 → 权限判据红；
  ② 脱敏函数把 key 值打出来 → 脱敏判据红；
  ③ 优先级反过来（配置压过环境变量）→ 优先级判据红。
故障注入用 `mock.patch` 在**运行时**打进去 —— 不改 `capsplus/services.py` 的源码，
免得"为了验证判据"把别人正在用的地基改出窗口期。
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import services as SV                          # noqa: E402

# 假凭据：判据只许拿它当"不许出现的那个串"
SECRET = "sk-TEST-9f3c2b7a-do-not-leak-me"


# ---------------------------------------------------------------------------
# 判据本身（正例与反例共用同一批函数 —— 反例才证明得了判据有牙齿）
# ---------------------------------------------------------------------------
def permission_ok(path: Path) -> bool:
    """判据①：文件权限必须是 0600。"""
    return stat.S_IMODE(path.stat().st_mode) == 0o600


def output_leaks(text: str, secret: str = SECRET) -> bool:
    """判据②：输出里出现了 key 的值 = 泄露。"""
    return bool(secret) and secret in (text or "")


def env_wins(ep: SV.Endpoint, env: dict) -> bool:
    """判据③：环境变量压过配置文件（模块头第 ② 条）。"""
    value, source = ep.resolved_api_key(env=env)
    return value == env.get("CAPSPLUS_TEST_KEY", "") and "环境变量" in source


def endpoint_names(cap: SV.Capability) -> list[str]:
    return [ep.name for ep in cap.ordered_endpoints()]


class TestRoundTrip(unittest.TestCase):
    """① 读写往返。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_round_trip_unchanged(self):
        before = SV.default_services()
        ok, text = SV.save_services(before, self.path)
        self.assertTrue(ok, text)
        after = SV.load_services(self.path)
        self.assertEqual(after.error, "")
        for name in SV.CAPABILITIES:
            cap_before = before.get(name)
            cap_after = after.get(name)
            self.assertEqual(cap_after.mode, cap_before.mode, name)
            self.assertEqual(cap_after.target, cap_before.target, name)
            self.assertEqual(cap_after.lang, cap_before.lang, name)
            self.assertEqual(endpoint_names(cap_after), endpoint_names(cap_before), name)
            for ep_before in cap_before.ordered_endpoints():
                ep_after = cap_after.endpoints[ep_before.name]
                self.assertEqual(ep_after.kind, ep_before.kind)
                self.assertEqual(ep_after.style, ep_before.style)
                self.assertEqual(ep_after.base_url, ep_before.base_url)
                self.assertEqual(ep_after.model, ep_before.model)
                self.assertEqual(ep_after.api_key_env, ep_before.api_key_env)
                self.assertEqual(ep_after.credentials_file, ep_before.credentials_file)
                self.assertEqual(ep_after.credentials_key, ep_before.credentials_key)
                self.assertEqual(ep_after.env_file, ep_before.env_file)
                self.assertEqual(ep_after.env_key, ep_before.env_key)
                self.assertEqual(ep_after.timeout_s, ep_before.timeout_s)

    def test_leading_comments_survive(self):
        """用户手写的文件头注释必须还在（写回时原样保留，别把人家的说明顶掉）。"""
        self.path.write_text('# 我自己写的说明\n# 第二行\n\n[translate]\n', encoding="utf-8")
        svc = SV.load_services(self.path)
        ok, text = SV.save_services(svc, self.path)
        self.assertTrue(ok, text)
        again = self.path.read_text(encoding="utf-8")
        self.assertIn("我自己写的说明", again)
        self.assertIn("第二行", again)

    def test_values_with_quotes_are_escaped(self):
        """值里有引号/反斜杠也要写得出**读得回**的配置（不许写出坏 TOML）。"""
        svc = SV.default_services()
        svc.capability("translate").endpoints["local"].base_url = 'http://x/v1?a="b\\c"'
        ok, text = SV.save_services(svc, self.path)
        self.assertTrue(ok, text)
        back = SV.load_services(self.path)
        self.assertEqual(back.error, "")
        self.assertEqual(back.capability("translate").endpoints["local"].base_url,
                         'http://x/v1?a="b\\c"')

    def test_missing_file_is_not_an_error(self):
        """还没配过 = 用默认值，**不是错误**（别在界面上报红吓人）。"""
        svc = SV.load_services(self.dir / "nope.toml")
        self.assertEqual(svc.error, "")
        self.assertEqual(endpoint_names(svc.capability("translate")), ["local", "cloud"])


class TestBrokenFile(unittest.TestCase):
    """② 坏 TOML：不抛，带 error。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_broken_toml_does_not_raise(self):
        self.path.write_text("[translate\nmode = ", encoding="utf-8")
        svc = SV.load_services(self.path)          # 不许抛
        self.assertTrue(svc.error, "坏文件必须带 error 说明")
        self.assertIn("读不了", svc.error)
        # 退回了内置默认，但**还能用**
        self.assertEqual(endpoint_names(svc.capability("translate")), ["local", "cloud"])

    def test_wrong_field_type_does_not_raise(self):
        """字段类型不对也不许崩（timeout_s 写成字符串）。"""
        self.path.write_text('[translate]\ntimeout_s = "很快"\n', encoding="utf-8")
        svc = SV.load_services(self.path)
        self.assertEqual(svc.error, "")
        self.assertEqual(svc.capability("translate").timeout_s, 0.0)

    def test_unknown_keys_are_kept(self):
        """不认识的键不许丢（进 extra）—— 用户自己加的字段可能被别的工具读。"""
        self.path.write_text('[translate.myep]\nkind = "local"\nmy_own_field = "7"\n',
                             encoding="utf-8")
        svc = SV.load_services(self.path)
        ep = svc.capability("translate").endpoints["myep"]
        self.assertEqual(ep.extra.get("my_own_field"), "7")


class TestPermission(unittest.TestCase):
    """③ 写回后权限 0600（+ 反例具型）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_written_file_is_0600(self):
        svc = SV.default_services()
        svc.capability("translate").endpoints["local"].api_key = SECRET
        ok, text = SV.save_services(svc, self.path)
        self.assertTrue(ok, text)
        self.assertTrue(permission_ok(self.path),
                        f"权限是 {oct(stat.S_IMODE(self.path.stat().st_mode))}，不是 0600")

    def test_permission_criterion_has_teeth(self):
        """反例具型①：**注入"忘了 chmod 0600"这个故障 → 权限判据必须红**。

        注入方式：把 `Path.chmod` 打成空操作（模拟"写完忘了改权限"）。
        判据必须自己变红 —— 否则它只是一句"结构绿"。
        """
        svc = SV.default_services()
        with mock.patch.object(Path, "chmod", lambda self, mode=None: None):
            ok, _text = SV.save_services(svc, self.path)
        self.assertTrue(ok, "注入故障后写入本身仍应成功（故障只影响权限）")
        self.assertFalse(permission_ok(self.path),
                         "注入「忘了 chmod 0600」之后权限判据仍然是绿的 = 判据是摆设")


class TestMasking(unittest.TestCase):
    """④ 脱敏：输出里不许出现 key 的值（+ 反例具型）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def build(self) -> SV.Services:
        svc = SV.default_services()
        svc.capability("translate").endpoints["local"].api_key = SECRET
        svc.capability("translate").endpoints["cloud"].api_key_env = "CAPSPLUS_TEST_KEY"
        return svc

    def test_format_describe_never_prints_the_key(self):
        svc = self.build()
        text = SV.format_describe(svc)
        self.assertFalse(output_leaks(text), f"摘要里漏出了 key：\n{text}")

    def test_describe_never_prints_the_key(self):
        svc = self.build()
        for item in SV.describe(svc):
            for ep in item["endpoints"]:
                for value in ep.values():
                    self.assertNotEqual(value, SECRET, f"脱敏摘要里出现了 key：{ep}")

    def test_key_source_is_told_not_the_value(self):
        """要说清楚"来自哪"，但只说来源，不说值。

        注意：`resolved_api_key()` 只在**真的找到值**时才报来源 ——
        环境变量没设时说"没配"（这与 `services.py` 的实现一致）。
        """
        svc = self.build()
        with mock.patch.dict(os.environ, {"CAPSPLUS_TEST_KEY": "value-from-env"}):
            text = SV.format_describe(svc)
        self.assertIn("凭据", text)
        self.assertIn("CAPSPLUS_TEST_KEY", text)          # 来源（变量名）可以说
        self.assertFalse(output_leaks(text))

    def test_masking_criterion_has_teeth(self):
        """反例具型②：**注入"脱敏函数把 key 值打出来"这个故障 → 脱敏判据必须红**。

        注入方式：让 `resolved_api_key` 把**值**塞进"来源说明"里
        （这正是真实事故的形状：有人为了"方便看是��一把 key"把值拼进了描述）。
        """
        svc = self.build()

        def leaky(self, env=None):
            return SECRET, SECRET

        with mock.patch.object(SV.Endpoint, "resolved_api_key", leaky):
            text = SV.format_describe(svc)
        self.assertTrue(output_leaks(text),
                        "注入「把 key 值打出来」之后脱敏判据仍然是绿的 = 判据是摆设")


class TestPriority(unittest.TestCase):
    """⑤ 优先级：环境变量 > 凭据文件 > env 文件 > 明文（+ 反例具型）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.credentials = self.dir / "creds.yaml"
        self.credentials.write_text("refs:\n  MY_KEY: from-file\n", encoding="utf-8")
        self.env_file = self.dir / "my.env"
        self.env_file.write_text("MY_KEY=from-env-file\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def make(self) -> SV.Endpoint:
        return SV.Endpoint(name="e", api_key="from-plaintext",
                           api_key_env="CAPSPLUS_TEST_KEY",
                           credentials_file=str(self.credentials), credentials_key="MY_KEY",
                           env_file=str(self.env_file), env_key="MY_KEY")

    def test_environment_variable_wins_over_everything(self):
        ep = self.make()
        env = {"CAPSPLUS_TEST_KEY": "from-env"}
        value, source = ep.resolved_api_key(env=env)
        self.assertEqual(value, "from-env")
        self.assertIn("环境变量", source)
        self.assertTrue(env_wins(ep, env))

    def test_credentials_file_wins_over_plaintext(self):
        ep = SV.Endpoint(name="e", api_key="from-plaintext",
                         credentials_file=str(self.credentials), credentials_key="MY_KEY")
        value, source = ep.resolved_api_key(env={})
        self.assertEqual(value, "from-file")
        self.assertIn("凭据文件", source)

    def test_env_file_wins_over_plaintext(self):
        ep = SV.Endpoint(name="e", api_key="from-plaintext",
                         env_file=str(self.env_file), env_key="MY_KEY")
        value, source = ep.resolved_api_key(env={})
        self.assertEqual(value, "from-env-file")

    def test_plaintext_is_the_last_resort(self):
        ep = SV.Endpoint(name="e", api_key="from-plaintext")
        value, source = ep.resolved_api_key(env={})
        self.assertEqual(value, "from-plaintext")
        self.assertIn("配置文件", source)

    def test_priority_criterion_has_teeth(self):
        """反例具型③：**注入"优先级反过来（配置压过环境变量）"→ 优先级判据必须红**。

        注入方式：`resolved_api_key` 先看明文再看环境变量 ——
        正好是"有人图省事，把配置文件里的值当成最高优先级"这个真实事故的形状。
        """
        ep = self.make()
        env = {"CAPSPLUS_TEST_KEY": "from-env"}

        def reversed_priority(self, env=None):
            env = os.environ if env is None else env
            if self.api_key:
                return self.api_key, "配置文件里直接写的"
            if self.api_key_env:
                return env.get(self.api_key_env, ""), f"环境变量 {self.api_key_env}"
            return "", "没配"

        with mock.patch.object(SV.Endpoint, "resolved_api_key", reversed_priority):
            self.assertFalse(env_wins(ep, env),
                             "注入「优先级反过来」之后优先级判据仍然是绿的 = 判据是摆设")


class TestAutoOrder(unittest.TestCase):
    """⑥ auto 模式按书写顺序。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_file_is_the_only_truth_for_endpoints(self):
        """**配置文件里写过哪几个端点，就只有哪几个，顺序就是书写顺序**（auto 的优先级全靠它）。

        为什么是这个口径（2026-10-21 Lead 复核时定的）：原先是"拿内置默认做底再按名字覆盖"，
        于是内置的 `local` 永远排第一 —— 用户把自己的端点写在前面**不生效**，
        而 `Capability.ordered_endpoints` 的注释写的就是"文件里的书写顺序 = 优先级"，两者对不上。
        治本做法：**配置文件是唯一真相**。用户写了哪几个、什么顺序，就是哪几个、什么顺序。
        （另一个分身当时在客户端做了"按文件顺序重排"的绕行；地基改成文件优先后两边一致，那处可以退役。）
        """
        self.path.write_text(
            '[translate]\nmode = "auto"\n\n'
            '[translate.zeta]\nkind = "remote"\nbase_url = "http://z/v1"\n\n'
            '[translate.alpha]\nkind = "remote"\nbase_url = "http://a/v1"\n\n'
            '[translate.mid]\nkind = "remote"\nbase_url = "http://m/v1"\n',
            encoding="utf-8")
        svc = SV.load_services(self.path)
        cap = svc.capability("translate")
        self.assertEqual(cap.mode, "auto")
        self.assertEqual(endpoint_names(cap), ["zeta", "alpha", "mid"],
                         "文件里写了哪几个端点就只有哪几个，且按书写顺序")
        self.assertEqual(cap.endpoint().name, "zeta", "auto 时第一个是优先级最高的")

    def test_a_capability_the_file_never_mentions_keeps_the_builtin_default(self):
        """没提到的能力用内置默认（**没有配置文件时行为一个字不变**，这是硬要求）。"""
        self.path.write_text('[translate.local]\nbase_url = "http://elsewhere/v1"\n',
                             encoding="utf-8")
        svc = SV.load_services(self.path)
        translate = svc.capability("translate")
        self.assertEqual(endpoint_names(translate), ["local"],
                         "文件里只写了 local ⇒ 就只有 local（用户明确配置了，不替他加 cloud）")
        self.assertEqual(translate.endpoints["local"].base_url, "http://elsewhere/v1")
        self.assertEqual(endpoint_names(svc.capability("ocr")), ["local", "cloud"],
                         "文件没提 OCR ⇒ OCR 继续用内置默认")

    def test_no_config_file_keeps_the_builtin_default_entirely(self):
        """压根没有配置文件 ⇒ 内置默认原封不动（老行为逐字不变）。"""
        missing = self.path.parent / "not-there.toml"
        svc = SV.load_services(missing)
        self.assertEqual(svc.error, "", "文件不存在不是错误")
        self.assertEqual(endpoint_names(svc.capability("translate")), ["local", "cloud"])
        self.assertEqual(svc.capability("translate").mode, "local",
                         "翻译默认档是 local —— 默认不许静默切到计费云端")

    def test_named_mode_picks_that_endpoint(self):
        self.path.write_text(
            '[translate]\nmode = "alpha"\n\n'
            '[translate.zeta]\nbase_url = "http://z/v1"\n\n'
            '[translate.alpha]\nbase_url = "http://a/v1"\n',
            encoding="utf-8")
        svc = SV.load_services(self.path)
        self.assertEqual(svc.capability("translate").endpoint().name, "alpha")

    def test_order_survives_a_save(self):
        """写回之后顺序还是那个顺序（渲染不许偷偷重排）。

        走的是"设置窗保存"那条真路：默认配置 → 写盘 → 读回 → 再写回 → 读回，
        每一轮端点顺序都必须一模一样（`auto` 的优先级全靠它）。
        """
        svc = SV.default_services()
        ok, _text = SV.save_services(svc, self.path)
        self.assertTrue(ok)
        first = SV.load_services(self.path)
        self.assertEqual(endpoint_names(first.capability("translate")), ["local", "cloud"])
        ok, _text = SV.save_services(first, self.path)
        self.assertTrue(ok)
        again = SV.load_services(self.path)
        self.assertEqual(endpoint_names(again.capability("translate")), ["local", "cloud"])
        self.assertEqual(endpoint_names(again.capability("ocr")), ["local", "cloud"])
        self.assertEqual(endpoint_names(again.capability("asr")), ["local"])


if __name__ == "__main__":
    unittest.main()
