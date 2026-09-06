# 内置采集引擎（vendored）

本目录打包了采集引擎，`backend/collector.py` 以本仓库 venv 解释器直接运行
`scripts/boss_cdp_raw.py`，不再依赖外部仓库或独立虚拟环境。

## 来源与许可

- 上游：[boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper) v2.2.0（commit `2bc40f5`）
- 许可：MIT（见本目录 [LICENSE](LICENSE)，版权归上游作者所有）；本项目亦以 MIT 发布
- 上游文件：`scripts/boss_cdp_raw.py`、`data/city_codes.json`，未改动其余上游代码路径

## 相对上游 v2.2.0 的补丁

| 补丁 | 说明 |
|------|------|
| `SCRAPER_*` 节奏环境变量 | `SCRAPER_LIST_PAGE_GAP_MIN/MAX`（翻页等待，默认 12-22s）、`SCRAPER_DETAIL_DWELL_MIN/MAX`（详情页加载停留，默认 5-10s）、`SCRAPER_DETAIL_GAP_MIN/MAX`（详情间隔，默认 10-25s）、`SCRAPER_READ_PAUSE_SCALE`（详情阅读等待整体缩放，默认 1.0）。未注入时行为与上游完全一致；boss-copilot 采集节奏三档（稳妥/均衡/快速）通过这些变量注入 |
| `--company <brandId\\|URL>` | 公司定向采集：brandId 走 `/web/geek/job?brandId=&page=` 参数化翻页；完整 URL 首页直接导航，query 带 `brandId` 时继续翻页，否则单页结束 |
| `--dup-stop-ratio <0-1>` | 翻页重复早停：某页新增岗位占比 ≤ 阈值时提前结束（第 1 页不触发） |

## 升级上游的方法

用上游新版本覆盖 `scripts/boss_cdp_raw.py` 与 `data/city_codes.json`，然后按本表重新应用三处补丁（补丁点集中在：全局常量区的节奏助手函数、`scrape_list` 签名与翻页循环、`build_search_url` 旁、`main()` 的 argparse 与调用处）。
