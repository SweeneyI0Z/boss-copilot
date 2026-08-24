"""M13 只读 spike：验证「感兴趣」推荐页在真实登录态下的卡片结构与解析映射。

用法（需对应账号已登录）：
    .venv/bin/python tests/spike_m13_favorites.py             # 双账号各读第 1 页
    .venv/bin/python tests/spike_m13_favorites.py collect 2   # 指定账号与页数

只做 Page.navigate + DOM 读取，零点击零写操作；结果只打印不入库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import favorites  # noqa: E402
from backend.boss import cdp   # noqa: E402


def probe(account: str, pages: int) -> None:
    label = cdp.config.ACCOUNTS[account]["label"]
    launched = cdp.launch(account)
    print(f"[{label}] Chrome: {'已启动' if launched.get('ok') else launched.get('error')}")
    if not launched.get("ok"):
        return
    reader = favorites.FavoritePageSession(account)
    try:
        for page in range(1, pages + 1):
            data = reader.read_page(page)
            print(f"[{label}] 第{page}页 url={data.get('url', '')[:90]} "
                  f"login={data.get('login')} risk={data.get('risk')}")
            raws = favorites.parse_favorite_page(data)
            print(f"[{label}] 解析出 {len(raws)} 个岗位（原始卡片 {len(data.get('cards') or [])} 张）")
            for raw in raws[:3]:
                print(f"    - {raw['title']} | {raw['boss_name']} | {raw['salary']} "
                      f"| {raw['tags']} | {raw['location']} | {raw['job_link'][:70]}")
            if page == 1 and not raws and data.get("cards"):
                print(f"[{label}] ⚠️ 有卡片但解析为空，原始卡片示例：")
                print("   ", data["cards"][0])
    finally:
        reader.close()


if __name__ == "__main__":
    accounts = sys.argv[1:2] or None
    page_count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    for account in (accounts or favorites.sync_accounts()):
        probe(account, page_count)
