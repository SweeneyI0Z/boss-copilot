# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 目录版 + 控制台窗口，最大化降低杀软误报。

形态决策（详见 README「打包发布」）：
- onedir（非 onefile）：不自我解压、不从 %TEMP% 执行、无 SFX 打包器特征，
  是免费手段中误报最低的形态；引擎子进程从应用目录秒起。
- console=True：双击运行保留控制台窗口，显示访问地址与运行日志。
- upx=False：压缩壳是杀软启发式的重点特征，显式全局禁用。
- 图标 assets/boss-copilot.ico 由 assets/make_icon.py 生成（"Bs"
  字标；Pillow 仅生成图标时使用，见该脚本头部说明）。
- 配合 version_info.txt 版本信息资源与（可选的）自编译 bootloader
  （build_bootloader.bat）进一步降低误报。
"""
import os

project_root = SPECPATH
ICON = os.path.join(project_root, "assets", "boss-copilot.ico")

# 随包分发的静态资源：保持与源码树相同的相对布局，
# 运行期由 config.APP_ROOT（即 _MEIPASS）按同名相对路径定位。
datas = [
    # 前端整树（含 vendored vue/marked/purify），StaticFiles 运行期需要真实目录
    (os.path.join(project_root, "frontend"), "frontend"),
    # 内置采集引擎：脚本与城市码表（主进程 cities.py 与引擎子进程双读取），
    # README/LICENSE 一并入包（MIT 合规）
    (os.path.join(project_root, "vendor", "boss-zhipin-scraper", "scripts"),
     os.path.join("vendor", "boss-zhipin-scraper", "scripts")),
    (os.path.join(project_root, "vendor", "boss-zhipin-scraper", "data"),
     os.path.join("vendor", "boss-zhipin-scraper", "data")),
    (os.path.join(project_root, "vendor", "boss-zhipin-scraper", "README.md"),
     os.path.join("vendor", "boss-zhipin-scraper")),
    (os.path.join(project_root, "vendor", "boss-zhipin-scraper", "LICENSE"),
     os.path.join("vendor", "boss-zhipin-scraper")),
]

hiddenimports = [
    # 采集引擎以数据文件打包、不经 PyInstaller 静态分析，其运行时依赖必须显式声明
    "requests",
    "websocket",
    # backend 内函数级延迟导入（兜底显式声明）
    "openai",
    "openpyxl",
    # uvicorn 按字符串动态加载的子模块
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [os.path.join(project_root, "run.py")],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="boss-copilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
    version=os.path.join(project_root, "version_info.txt"),
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="boss-copilot",
)
