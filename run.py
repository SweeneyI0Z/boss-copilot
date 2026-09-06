"""boss-copilot 统一入口：服务模式 + 打包态采集引擎再入模式。

- 服务模式（默认）：python run.py [端口] [--no-browser]
  等价于 start.bat 的 python -m uvicorn backend.main:app --host 127.0.0.1 --port 端口；
  PyInstaller 打包后 exe 双击即走本模式，控制台打印访问地址并自动打开浏览器。
- 引擎再入模式：run.py --run-engine <引擎参数...>
  仅供打包态使用：冻结后 sys.executable 是 exe 自身，collector 无法再以
  "解释器 + 脚本路径" 起采集引擎子进程，故由 exe 自再入、以 runpy 把引擎
  脚本当 __main__ 执行（开发态 collector 仍直跑脚本，行为不变）。
"""
import os
import runpy
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
BROWSER_OPEN_DELAY_SEC = 1.5


def _run_engine(engine_args: list) -> None:
    """把 vendored 采集引擎脚本当 __main__ 执行（runpy 不缓存、退出码原样透传）。

    argv[0] 置为脚本路径，使引擎内 dirname(__file__) 相对定位 data/city_codes.json
    与直接运行脚本时完全一致；SystemExit 不捕获，collector 依赖退出码判成败。
    """
    from backend import config
    sys.argv = [str(config.SCRAPER_SCRIPT)] + list(engine_args)
    runpy.run_path(str(config.SCRAPER_SCRIPT), run_name="__main__")


def parse_service_args(argv: list) -> tuple:
    """解析服务模式参数：可选端口（位置参数，默认 8787）与 --no-browser。"""
    port = DEFAULT_PORT
    open_browser = True
    for arg in argv:
        if arg == "--no-browser":
            open_browser = False
        elif arg in ("-h", "--help"):
            print("用法：run.py [端口] [--no-browser]\n"
                  f"  端口          服务监听端口（默认 {DEFAULT_PORT}）\n"
                  "  --no-browser  启动后不自动打开浏览器")
            raise SystemExit(0)
        elif arg.isdigit() and 1 <= int(arg) <= 65535:
            port = int(arg)
        else:
            raise SystemExit(f"[错误] 无法识别的参数：{arg}"
                             "（用法：run.py [端口] [--no-browser]）")
    return port, open_browser


def run_service(port: int, open_browser: bool) -> None:
    """启动 FastAPI 服务：打印用户友好横幅，可选延时自动打开浏览器。"""
    # 与 start.bat 对齐：给子进程一个确定性的 UTF-8 运行环境。
    os.environ.setdefault("PYTHONUTF8", "1")
    from backend import config
    from backend.main import app
    import uvicorn

    url = f"http://{DEFAULT_HOST}:{port}"
    print("=" * 60)
    print("  求职作战室 已启动")
    print()
    print(f"  请用浏览器打开：{url}")
    print(f"  数据目录：{config.DATA_DIR}")
    print("  停止服务：按 Ctrl+C 或直接关闭本窗口")
    print("=" * 60)
    if open_browser:
        # 延时打开，给 uvicorn 留出监听时间；失败不影响服务。
        threading.Timer(BROWSER_OPEN_DELAY_SEC, webbrowser.open,
                        args=(url,)).start()
    uvicorn.run(app, host=DEFAULT_HOST, port=port, log_level="info")


def main(argv: list = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    from backend import config
    if argv and argv[0] == config.ENGINE_REENTRY_FLAG:
        _run_engine(argv[1:])
        return
    port, open_browser = parse_service_args(argv)
    run_service(port, open_browser)


if __name__ == "__main__":
    main()
