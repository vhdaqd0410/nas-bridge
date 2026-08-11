"""NAS Bridge - 桌面端启动器
用 pywebview 包装 Flask 服务，避免浏览器缓存问题。
启动后在后台线程运行 Flask，前台打开原生窗口。
"""
import threading
import time
import sys
import os
import logging

# 确保工作目录正确
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("nas_bridge.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def start_flask():
    """在后台线程中启动 Flask 服务 + Watcher"""
    try:
        from app import app, watcher
        # 启动 watcher（监听成片目录）
        threading.Thread(target=watcher.start, daemon=True).start()
        # use_reloader=False 避免双进程
        app.run(host="127.0.0.1", port=8089, debug=False, use_reloader=False)
    except Exception as e:
        log.error("Flask 启动失败: %s", e)


def wait_for_flask(timeout=30):
    """等待 Flask 服务就绪"""
    import urllib.request
    for i in range(timeout):
        try:
            urllib.request.urlopen("http://127.0.0.1:8089/api/status", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    log.info("=" * 40)
    log.info("  NAS Bridge Desktop")
    log.info("=" * 40)

    # 尝试挂载 N 盘
    try:
        import subprocess
        result = subprocess.run(["net", "use", "N:"], capture_output=True, text=True)
        if result.returncode != 0:
            log.info("挂载 N 盘...")
            subprocess.run(
                ["net", "use", "N:", "\\\\192.168.8.234\\ai漫剧中转盘", "/persistent:yes"],
                capture_output=True
            )
    except Exception as e:
        log.warning("N 盘挂载检查失败: %s", e)

    # 启动 Flask 后台线程
    log.info("启动后台服务...")
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # 等待 Flask 就绪
    if not wait_for_flask():
        log.error("后台服务启动超时，请检查 nas_bridge.log")
        input("按回车键退出...")
        sys.exit(1)

    log.info("服务已就绪，打开窗口...")

    # 启动 pywebview 窗口
    import webview
    webview.create_window(
        title="NAS Bridge - 项目同步管理",
        url="http://127.0.0.1:8089",
        width=1280,
        height=800,
        min_size=(900, 600),
        text_select=True,
    )
    webview.start()

    # 窗口关闭后退出
    log.info("窗口已关闭，退出程序")
    sys.exit(0)


if __name__ == "__main__":
    main()
