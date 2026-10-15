import os, sys, time, subprocess
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_kbd import *
tap=Tap(); vk=make_vkbd(); time.sleep(1.2); tap.drain(0.4)
cases=[
 (["capslock","e"],{K["up"]:1},"光标上"),(["capslock","d"],{K["down"]:1},"光标下"),
 (["capslock","s"],{K["left"]:1},"光标左"),(["capslock","f"],{K["right"]:1},"光标右"),
 (["capslock","a"],{K["leftctrl"]:1,K["left"]:1},"词左"),(["capslock","g"],{K["leftctrl"]:1,K["right"]:1},"词右"),
 (["capslock","p"],{K["home"]:1},"行首"),(["capslock","semicolon"],{K["end"]:1},"行尾"),
 (["capslock","u"],{K["leftshift"]:1,K["home"]:1},"选中到行首"),
 (["capslock","o"],{K["leftshift"]:1,K["end"]:1},"选中到行尾"),
 (["capslock","h"],{K["leftctrl"]:1,K["leftshift"]:1,K["left"]:1},"选中词左"),
 (["capslock","dot"],{K["leftctrl"]:1,K["leftshift"]:1,K["right"]:1},"选中词右"),
 (["capslock","comma"],{K["leftctrl"]:2,K["left"]:1,K["leftshift"]:1,K["right"]:1},"选中当前词"),
 (["capslock","t"],{K["up"]:10},"上10行"),(["capslock","b"],{K["down"]:10},"下10行"),
 (["capslock","y"],{K["leftshift"]:7,K["up"]:7},"选中上7行(原10行,受宏上限压缩)"),
 (["capslock","n"],{K["leftshift"]:7,K["down"]:7},"选中下7行"),
 (["capslock","w"],{K["backspace"]:1},"删左1字"),(["capslock","r"],{K["delete"]:1},"删右1字"),
 (["capslock","leftalt","u"],{K["leftshift"]:1,K["leftctrl"]:1,K["home"]:1},"增强:选中到文首"),
 (["capslock","leftalt","p"],{K["leftctrl"]:1,K["home"]:1},"增强:跳到文首"),
 (["capslock","leftalt","semicolon"],{K["leftctrl"]:1,K["end"]:1},"增强:跳到文末"),
 (["capslock","leftalt","d"],{K["down"]:3},"增强:下3行"),
 (["capslock","leftalt","i"],{K["leftshift"]:3,K["up"]:3},"增强:选中上3行"),
 (["capslock","leftalt","t"],{K["pageup"]:1},"增强:上翻页"),
 (["capslock","leftalt","b"],{K["pagedown"]:1},"增强:下翻页"),
 (["capslock","leftalt","y"],{K["leftshift"]:1,K["pageup"]:1},"增强:选中上翻页"),
 (["capslock","leftalt","comma"],{K["home"]:1,K["leftshift"]:1,K["end"]:1},"增强:选中整行"),
 (["capslock","leftalt","r"],{K["leftctrl"]:1,K["leftshift"]:1,K["right"]:1,K["delete"]:1},"增强:删到词尾"),
 (["f24"],{K["f24"]:1},"透传基线"),
]
ok=bad=0; extras_only=[]
for keys,want,label in cases:
    tap.quiet(); tap.drain(0.2); composer(vk,keys)
    got=Counter(c for _,c,v in tap.until_quiet() if v==1)
    w=Counter(want); missing=w-got; extra=got-w
    good=not missing; ok,bad=(ok+1,bad) if good else (ok,bad+1)
    if good and extra and set(extra)<={K["leftalt"],K["leftctrl"]}: extras_only.append(label)
    combo="+".join(k.upper() for k in keys)
    msg=""
    if missing: msg+=" 缺:"+",".join(f"{N.get(k,k)}x{v}" for k,v in missing.items())
    if extra: msg+=" 多:"+",".join(f"{N.get(k,k)}x{v}" for k,v in extra.items())
    print(f"{'✅' if good else '❌'} {combo:26s} {label}{msg}")
print(f"\n映射合计 {ok} 通过 / {bad} 失败")
print(f"复合层固定多出 alt+ctrl 空点的条目: {len(extras_only)} 条（keyd 自身行为，非配置错误）")
# caps+q → Qbar
before=subprocess.run(["pgrep","-fc","capsplus-qbar"],capture_output=True,text=True).stdout.strip() or "0"
tap.quiet(); composer(vk,["capslock","q"]); time.sleep(3.0)
after=subprocess.run(["pgrep","-fc","capsplus-qbar"],capture_output=True,text=True).stdout.strip() or "0"
print(f"\nCAPS+Q → Qbar 进程数 {before} → {after}", "✅ 触发成功" if int(after)>int(before) else "❌ 未起来")
print(subprocess.run(["pgrep","-af","capsplus-qbar"],capture_output=True,text=True).stdout.strip()[:300])
fcntl.ioctl(vk,UI_DEV_DESTROY); os.close(vk); tap.close()
