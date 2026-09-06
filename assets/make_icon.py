"""生成应用图标 assets/boss-copilot.ico（"Bs" 字标）。

视觉规范：深蓝圆角方块底色 #001E36，
描边与字标同色 #31A8FF，圆角比例约 22.7%，粗体双字母（首大写次小写）居中。

用法（Pillow 隔离安装到 build/icon-tools，仅生成图标时使用，不进 requirements）：
    .venv/Scripts/python.exe -m pip install --target build/icon-tools pillow
    PYTHONPATH=build/icon-tools .venv/Scripts/python.exe assets/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024
BG = (0, 30, 54, 255)          # #001E36，Ps 同款底色
ACCENT = (49, 168, 255, 255)   # #31A8FF，Ps 同款描边/字色
BORDER = 48                    # 描边宽度（约 4.7%）
RADIUS = 232                   # 圆角半径（约 22.7%，Adobe CC 系比例）
FONT_SIZE = 480
LETTER = "Bs"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
             (64, 64), (128, 128), (256, 256)]
FONT_CANDIDATES = [r"C:\Windows\Fonts\arialbd.ttf",
                   r"C:\Windows\Fonts\segoeuib.ttf"]


def load_font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit(f"未找到粗体字体，请确认其一存在：{FONT_CANDIDATES}")


def render(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = size / SIZE
    inset = round(BORDER * scale / 2)
    draw.rounded_rectangle([inset, inset, size - inset, size - inset],
                           radius=round(RADIUS * scale),
                           fill=BG, outline=ACCENT,
                           width=max(1, round(BORDER * scale)))
    font = load_font(round(FONT_SIZE * scale))
    # "Bs" 无下伸部，锚点居中即光学居中；整体略上移补偿描边视觉重量
    draw.text((size / 2, size / 2 - 8 * scale), LETTER,
              font=font, fill=ACCENT, anchor="mm")
    return img


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    master = render()
    master.save(out_dir / "boss-copilot.ico", format="ICO", sizes=ICO_SIZES)
    master.resize((256, 256), Image.LANCZOS).save(
        out_dir / "icon-preview-256.png")
    print(f"已生成 {out_dir / 'boss-copilot.ico'} 与 icon-preview-256.png")


if __name__ == "__main__":
    sys.exit(main())
