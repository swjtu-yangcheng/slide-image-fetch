---
name: slide-image-fetch
description: PPT/文档网络配图。按英文主题关键词从 Google Images（浏览器）/ Bing / Openverse / Wikimedia Commons 抓取真实场景图，候选模式 + 逐张核对。适用：做 PPTX 配图、收集工程/行业实景照片。关键词：抓图、配图、google image、图片搜索、爬图片、slide 配图。
---

# slide-image-fetch — Slide 网络配图技能

杨老师的 PPTX **全部课堂授课用，不出版**：配图许可不构成约束，以内容贴合为唯一标准（来源仍记录备查）。

## 环境（已备妥，勿重装）
- 隔离 Python：`C:\Users\yangc\.workbuddy\binaries\python\envs\imgfetch\Scripts\python.exe`（playwright + httpx[http2] + Pillow）
- 隔离 Edge profile：`%USERPROFILE%\.workbuddy\tmp\edge-imgfetch-profile2`（playwright launch_persistent_context 自动拉起，走代理 127.0.0.1:7890）
- 主程序：`scripts/fetch.py`

## 用法
```bash
C:/Users/yangc/.workbuddy/binaries/python/envs/imgfetch/Scripts/python.exe \
  "C:/Users/yangc/.workbuddy/skills/slide-image-fetch/scripts/fetch.py" \
  --out <项目>/assets --cand 3 \
  --task traffic_smart:"smart bridge health monitoring sensors" \
  --task water_pv:"floating solar reservoir aerial"
# 调试/不启浏览器：加 --no-google（仅 Bing+Openverse+Commons）
```
产物：assets/name_cN.jpg 候选 + manifest.json + image_sources.md。
**Agent 职责**：逐张 Read 候选图核对内容 → mv 选定图为正式名（如 traffic_smart.jpg）→ 删除其余候选。

## 策略链与实测结论（2026-10-16 全部亲测）
| 通道 | 状态 | 要点 |
| :- | :- | :- |
| Google Images（首选） | 需过一次人机验证 | 数据中心出口 IP 触发 /sorry/ reCAPTCHA；脚本会自动暂停，用户在弹出的 Edge 窗口点一次即可，cookie 持久化后复用；若换 VPN 住宅节点可不触发 |
| Bing HTTP | 可用，质量不稳 | 免浏览器、秒级；长尾专业词会跑偏（电商/新闻/农场图），**必须候选核对** |
| Openverse API | 可用 | 公共许可聚合，质量中等 |
| Wikimedia Commons | 可用 | 公共版权兜底；**必须过滤 MIME**（否则抓回 PDF） |

## 实测教训（勿再犯）
1. **proxy 环境变量会污染 playwright 对 127.0.0.1 的 CDP/浏览器连接**（502）——fetch.py 启动时已自动清除并设 NO_PROXY。
2. `connect_over_cdp(http://127.0.0.1:9222)` 在 Edge 上端口绑定不稳定（主进程复用导致监听失败）→ 改用 `launch_persistent_context(channel="msedge")`。
3. **绝不按图片尺寸降序选图**：新闻大图/内容农场图分辨率最高但内容跑偏。保持引擎相关性排序，靠候选+肉眼核对兜底。
4. 黑名单域名（图库水印 dreamstime/alamy、新闻 CDN ismcdn.jp、内容农场、电商 media-amazon）持续维护在脚本 BAD_HOSTS。
5. Google 的 /sorry/ 风控是 IP 级：换域名无效；reCAPTCHA 不绕过（合法性与稳定性都要求人工过一次）。
6. pip 装包走阿里云镜像快（清华源曾缺 playwright 元数据）；官方源经透明代理会卡死。

## 英文关键词构造
中文提纲 → 英文专业术语 + 场景词；宽图加 aerial/panoramic；规避商品图可加 site:gov / site:edu；工程实景类词汇：retrofit / monitoring / aerial view / construction site / interior。
