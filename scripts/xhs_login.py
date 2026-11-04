"""小红书 MCP 扫码登录助手（替代 MCP Inspector，不需要 Node）。

用法：pixi run xhs-login
流程：调用 get_login_qrcode 取二维码 → 保存为 PNG 并用系统看图器打开 → 轮询 check_login_status 直到登录成功。
"""
from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chigua.config import PROJECT_ROOT, settings  # noqa: E402

QR_PATH = PROJECT_ROOT / "reports" / "xhs_login_qrcode.png"


async def main() -> int:
    from chigua.tools.mcp_client import open_mcp_session

    url = settings.xhs_mcp_url or "http://localhost:18060/mcp"
    print(f"连接 {url} …")
    async with open_mcp_session(url, settings.xhs_mcp_token, timeout=120) as session:
        if True:

            status = await session.call_tool("check_login_status", {})
            text = " ".join(c.text for c in status.content if getattr(c, "type", "") == "text")
            print("当前状态：", text[:200])
            if "true" in text.lower() and "false" not in text.lower() or "已登录" in text:
                print("已经是登录状态，无需再扫。")
                return 0

            print("获取二维码 …（可能需要十几秒，首次会下载浏览器）")
            qr = await session.call_tool("get_login_qrcode", {})
            saved = False
            for c in qr.content:
                ctype = getattr(c, "type", "")
                if ctype == "image":
                    QR_PATH.parent.mkdir(parents=True, exist_ok=True)
                    QR_PATH.write_bytes(base64.b64decode(c.data))
                    saved = True
                elif ctype == "text":
                    print(c.text[:500])
            if not saved:
                print("工具没有返回图片，请看上面的文字提示（可能含二维码链接或 base64）。")
                return 1
            print(f"二维码已保存：{QR_PATH}，正在打开，请用小红书 App 扫码（二维码会过期，尽快）")
            if sys.platform == "darwin":
                subprocess.run(["open", str(QR_PATH)], check=False)

            deadline = time.time() + 180
            while time.time() < deadline:
                await asyncio.sleep(4)
                status = await session.call_tool("check_login_status", {})
                text = " ".join(c.text for c in status.content if getattr(c, "type", "") == "text")
                if ("true" in text.lower() and "false" not in text.lower()) or "已登录" in text:
                    print("登录成功 ✅ ", text[:200])
                    return 0
                print("等待扫码…", text[:80])
            print("3 分钟内未检测到登录，请重新运行。")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
