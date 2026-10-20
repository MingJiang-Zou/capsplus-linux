// SPDX-License-Identifier: GPL-2.0-only
// Copyright (c) 2026 capsplus-linux contributors
// 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
//   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
//
// 带 **mock Shell** 的 import 冒烟：把扩展里的 `gi://*` 与 `resource://*` 换成桩，真 `import()` 一次。
//
// 为什么不能直接 import 原文件：`gi://` 只有 GJS 认得（node 解析不到），`resource:///org/gnome/...`
// 更是 shell 自己的资源 URI。所以本脚本先把那些 import 语句**改写成桩绑定**，写一份临时 `.mjs`
// 再 import —— 这样不需要任何 loader hook（少猜一个 API 就少一个坑），也没有网络/文件副作用。
//
// 能验什么（这正是预检要的那两块）：
//   * 模块级代码会不会抛（真事故就是这里炸的：半截 UTF-8 ⇒ SyntaxError）；
//   * 导出的是不是**一个带 `enable`/`disable` 的类**（写成模块级 `export function` 时 shell
//     报 `not a constructor` 且静默不加载，现象与没装一模一样）。
// **不能**验什么（别把它当运行时验收）：桩不是真 gi —— 任何"这个 API 存不存在、签名对不对"它都
// 看不出来。那要 `tools/ext-bench.sh` 起一个真的 headless shell（判词在 `capsplus/extcheck.py`）。
//
// 用法：node tools/preflight-import-smoke.mjs <extension.js 路径>
//   成功 → stdout 打 `SMOKE_OK …`，退出 0；失败 → stderr 打原因，退出 1；用法错 → 退出 2。
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const target = process.argv[2];
if (!target) {
    console.error('用法：node preflight-import-smoke.mjs <extension.js>');
    process.exit(2);
}

/** 万能桩：可调用、可 new、任意属性访问都还有桩。
 *  `then` 必须返回 undefined —— 否则 import 时会被当成 thenable 挂住。 */
function mkStub(name) {
    const fn = function (..._args) { return mkStub(name); };
    return new Proxy(fn, {
        get(t, prop) {
            if (typeof prop === 'symbol') return t[prop];
            if (prop === 'then') return undefined;
            if (prop === 'prototype') return t.prototype;
            return mkStub(name + '.' + String(prop));
        },
        apply() { return mkStub(name); },
    });
}

/** 把 import 语句换成桩绑定；返回 `{ source, specs }`。
 *  只认**单行** import（本仓扩展就是单行）；多行形态直接报错而不是悄悄漏掉一个绑定。 */
function stubbify(src) {
    const specs = [];
    const lines = src.split('\n');
    const out = lines.map((line, idx) => {
        const m = line.match(/^\s*import\s+(?:\*\s+as\s+(\w+)|(\w+)|\{([^}]*)\})\s+from\s+['"]([^'"]+)['"]\s*;?\s*$/);
        if (!m) return line;
        const [, star, dflt, named, spec] = m;
        if (!/^(gi|resource):/.test(spec)) return line;      // 真能解析的（相对路径）别动
        specs.push(spec);
        if (star) return `const ${star} = mkStub(${JSON.stringify(spec)});`;
        if (dflt) return `const ${dflt} = mkStub(${JSON.stringify(spec)});`;
        return `const { ${named.split(',').map(s => s.trim().split(/\s+as\s+/).pop()).join(', ')} } = mkStub(${JSON.stringify(spec)});`;
    });
    for (const [i, line] of lines.entries()) {
        if (/^\s*import\b/.test(line) && !/['"]/.test(line)) {
            throw new Error(`第 ${i + 1} 行的 import 不是单行形态，冒烟改不动它：${line.trim()}`);
        }
    }
    return { source: out.join('\n'), specs };
}

let dir;
let failed = null;
try {
    const raw = readFileSync(target);
    // 先按严格 UTF-8 解一次：半截文件在这里就会现形（也是真事故的第一现场）
    const src = new TextDecoder('utf-8', { fatal: true }).decode(raw);
    const { source, specs } = stubbify(src);
    dir = mkdtempSync(join(tmpdir(), 'capsplus-smoke-'));
    const probe = join(dir, 'candidate.mjs');
    writeFileSync(probe, `const mkStub = ${mkStub.toString()};\n` + source, 'utf8');

    const mod = await import(pathToFileURL(probe).href);
    const exported = mod.default;
    if (typeof exported !== 'function') {
        throw new Error(`default 导出不是类/函数（是 ${typeof exported}）——GNOME 45+ 要 \`export default class\``);
    }
    for (const method of ['enable', 'disable']) {
        if (typeof exported.prototype?.[method] !== 'function') {
            throw new Error(`这个类上没有 ${method}() —— shell 会在启用/停用时抛`);
        }
    }
    console.log(`SMOKE_OK 带桩 import 通过：default 是类，enable/disable 都在（桩掉 ${specs.length} 个 shell 侧 import）`);
} catch (e) {
    failed = e;
}

// ⚠ **清场要在 process.exit() 之前**（2026-10-20 实测踩到）：写在 `finally` 里不成立 ——
//   `process.exit()` 立刻终止进程，`finally` 根本轮不到执行。于是**每一条失败路径**都留一个
//   `/tmp/capsplus-smoke-*`：本仓为这类事吃过亏（`/tmp/capsplus-extjs-*` 一天攒了 141 个），
//   所以这里显式先清、再决定退出码。
if (dir) rmSync(dir, { recursive: true, force: true });
if (failed) {
    console.error(`SMOKE_FAIL ${failed.name}: ${failed.message}`);
    process.exit(1);
}