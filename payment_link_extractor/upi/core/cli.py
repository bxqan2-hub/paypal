# -*- coding: utf-8 -*-
"""命令行入口：`upi-zero-link` 或 `python -m upi_zero_link`。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .protocol import NetworkDown, extract

DEFAULT_PROXY = os.environ.get("UPI_PROXY") or "socks5h://127.0.0.1:1080"


def log(message: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def load_accounts(args) -> list[dict]:
    if args.accounts:
        data = json.loads(Path(args.accounts).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("accounts") if isinstance(data.get("accounts"), list) else [data]
        if not isinstance(data, list):
            raise SystemExit("账号文件必须是 JSON 数组，或带 accounts 数组的对象")
        accounts = []
        for item in data:
            if not isinstance(item, dict):
                raise SystemExit("账号文件每一项都要是对象")
            accounts.append({
                "email": str(item.get("email") or ""),
                "access_token": str(item.get("access_token") or item.get("token") or ""),
                "session_token": str(item.get("session_token") or ""),
            })
        return accounts
    if not args.token:
        raise SystemExit("要么给 --token，要么给 --accounts 文件")
    return [{"email": args.email or "", "access_token": args.token, "session_token": ""}]


def make_proxies(args) -> list[str]:
    proxies = args.proxy or [DEFAULT_PROXY]
    return [p for p in proxies if p]


def notify(record: dict) -> None:
    """可选提醒：Windows 上响铃 + 托盘气泡，其他平台退回终端响铃。"""
    text = record.get("url") or ""
    if sys.platform.startswith("win"):
        import subprocess
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "1..4|ForEach-Object{[console]::beep(1200,250);[console]::beep(800,250)};"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Warning;"
            "$n.BalloonTipTitle='UPI link ready';"
            "$n.BalloonTipText='%s';"
            "$n.Visible=$true;$n.ShowBalloonTip(120000);Start-Sleep -Seconds 15"
        ) % text.replace("'", "")
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except OSError:
            pass
    for _ in range(4):
        print("\a", end="", flush=True)
        time.sleep(0.3)


def save_links(path: Path, record: dict) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except (OSError, ValueError):
        data = []
    data.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def run_account(account: dict, proxies: list[str], *, rounds: int, tries: int,
                quiet: bool) -> dict | None:
    """按出口轮换反复试同一个号；出核验通过的链就返回。"""
    for index in range(max(1, rounds)):
        proxy = proxies[index % len(proxies)]
        label = "第 %d/%d 轮（出口 %s）" % (index + 1, max(1, rounds), _label(proxy))
        log("%s %s" % (account.get("email") or "?", label))
        try:
            result = extract(account, proxy, tries=tries,
                             emit=(lambda *a: None) if quiet else log)
        except NetworkDown as exc:
            log("  出口断了，换下一条：%s" % str(exc)[:140])
            continue
        except ValueError as exc:
            raise SystemExit(str(exc))
        if result.get("ok"):
            result["exit"] = _label(proxy)
            return result
        log("  未出链：%s" % result.get("error"))
        if result.get("error") in ("declined", "nonzero_due", "upi_unavailable", "link_unverified"):
            # 该号已经跑到终点，再打只是白烧资格
            break
    return None


def _label(proxy: str) -> str:
    tail = proxy.rsplit("@", 1)[-1]
    return tail or proxy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="upi-zero-link",
        description="印度 ₹0 UPI Autopay 提链（协议层，无浏览器）")
    parser.add_argument("--token", help="ChatGPT access_token")
    parser.add_argument("--email", default="", help="用于 Stripe billing_details 的邮箱")
    parser.add_argument("--accounts", help="账号 JSON 文件：[{\"email\":..,\"access_token\":..}, ...]")
    parser.add_argument("--proxy", action="append", default=None,
                        help="印度出口，可重复；默认取环境变量 UPI_PROXY 或 socks5h://127.0.0.1:1080")
    parser.add_argument("--rounds", type=int, default=4, help="每个号最多换几条出口（默认 4）")
    parser.add_argument("--tries", type=int, default=2, choices=(1, 2),
                        help="同一会话里 confirm 重试次数（默认 2）")
    parser.add_argument("--all", action="store_true", help="出链后继续跑完剩下的账号")
    parser.add_argument("--quiet", action="store_true", help="只打结论，不打过程")
    parser.add_argument("--notify", action="store_true", help="出链时响铃 / 弹窗")
    parser.add_argument("--out", default="links.json", help="结果落盘路径（默认 links.json）")
    args = parser.parse_args(argv)

    accounts = load_accounts(args)
    proxies = make_proxies(args)
    out_path = Path(args.out)
    log("账号 %d 个，出口 %s，每号最多 %d 轮" % (len(accounts), ", ".join(_label(p) for p in proxies),
                                              max(1, args.rounds)))

    hits = []
    for account in accounts:
        result = run_account(account, proxies, rounds=args.rounds, tries=args.tries,
                             quiet=args.quiet)
        if not result:
            log("%s 没出核验通过的链" % (account.get("email") or "?"))
            continue
        expires = int(result.get("expires_at") or 0)
        left = max(0, expires - int(time.time())) if expires else 0
        record = {
            "email": result.get("email") or account.get("email") or "",
            "url": result.get("url") or "",
            "qr_png": result.get("qr_png") or "",
            "expires_at": result.get("expires_at") or "",
            "seconds_left": left,
            "due": result.get("due"),
            "exit": result.get("exit") or "",
            "issued_at": result.get("at") or int(time.time()),
            "intent_state": "verified",
        }
        save_links(out_path, record)
        if args.notify:
            notify(record)
        hits.append(record)
        print()
        print("###### ★ ₹0 UPI link (verified) ######")
        print("account : %s" % record["email"])
        print("url     : %s" % record["url"])
        print("qr      : %s" % (record["qr_png"] or "-"))
        print("due     : %s   exit: %s" % (record["due"], record["exit"]))
        if expires:
            print("expires : %s (%d seconds left)" % (
                time.strftime("%H:%M:%S", time.localtime(expires)), left))
        print("saved   : %s" % out_path)
        print("######################################")
        print()
        if not args.all:
            return 0

    return 0 if hits else 1


if __name__ == "__main__":
    sys.exit(main())
