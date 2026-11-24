# 免费节点家宽订阅池 — 只输出 residential.txt

精简版：只保留「家宽 / 移动网络」节点的筛选与导出。

```
抓取订阅源 → 解析全协议 → 测前凭据去重 → 端口预检
→ sing-box 真实测活 → 出口 IP 分类 → 家宽筛选 → residential.txt
```

## 文件

| 文件 | 说明 |
|---|---|
| `residential_pool.py` | 唯一脚本，标准库 only，不用装 pip 包 |
| `sources.txt` | 订阅源，一行一个 URL |
| `residential.txt` | 运行后生成，唯一输出，一行一个节点链接 |

## 用法

```bash
# 1. 把订阅链接写进 sources.txt
# 2. 装 sing-box（测活必需）
#    Linux: curl -fsSL https://sing-box.app/install.sh | sh
#    macOS: brew install sing-box
# 3. 运行
python3 residential_pool.py
```

常用参数：

```bash
python3 residential_pool.py --max-nodes 300 --test-workers 3 --timeout 12
python3 residential_pool.py --only-country JP    # 只要日本出口
python3 residential_pool.py --no-mobile          # 只要家宽，不要移动网络
python3 residential_pool.py --parse-only         # 不测活，只看解析/去重结果
SUB_URLS="https://...,https://..." python3 residential_pool.py  # 不用 sources.txt
```

## 支持的协议

`vmess` `vless` `trojan` `ss` `hysteria2` `tuic` `socks` `http`

订阅形态支持明文多行和整段 base64 两种。

## 家宽是怎么判的

测活时经节点本身去查 `ip-api.com`，拿到出口 IP 的 `mobile / hosting / proxy / isp / org / as` 再分类：

- `mobile=True` → 移动网络，保留
- `hosting=True` 或 `proxy=True` → 机房，丢掉
- ASN / 组织命中机房关键词（Amazon、Google、阿里云、Vultr 等，见脚本里的 `DATACENTER_KEYWORDS`）→ 机房，丢掉
- 其余 → 家宽，保留

这是启发式判断，不是 100% 准确：伪住宅 IP（机房 IP 冒充 ISP）拦不住。给 Claude、银行这类风控严的用之前，还要单独再验 ASN、欺诈分和反向解析。

## GitHub Actions 定时跑

可以，不用自己电脑挂着。工作流在 `.github/workflows/residential.yml`：

- 每天 **08:00 北京时间**跑一次（cron `0 0 * * *`，UTC 0 点），也支持在 Actions 页面手动点 Run workflow
- 跑 `ubuntu-latest`，装 sing-box 1.14.4（2026-11-24 核实过的最新版），跑脚本，筛完把 `residential.txt` 提交回仓库，没变化就不提交
- 不监听 push，所以回提交不会自己触发自己；`timeout-minutes: 60`，500 个节点最坏约半小时

订阅源放哪：

- 公开免费订阅 → 直接写 `sources.txt`，一行一个
- 私有订阅 → 别写进仓库，写进仓库 Settings → Secrets → Actions，新建 `SUB_URLS`，多个用英文逗号隔开，脚本会把它和 `sources.txt` 合并着跑

免费额度：公开仓库 Actions 不限分钟；私有仓库每月免费 2000 分钟，这个任务每天约 10–30 分钟，一个月约 300–900 分钟，够用。定时任务不保证准点，晚几分钟到几十分钟是正常的。

注意：`.github/workflows/` 这个目录我这边接口传不上去（GitHub 要求单独的 workflows 权限，之前 chat.yml 就是卡在这），所以工作流文件得用浏览器在网页上新建 / 上传，或者你自己 push 上去，脚本和 README 我这边能直接传。

## 输出格式

`residential.txt` 里只有节点链接，没有注释和统计，导入客户端可直接用。节点按延迟从低到高排，名字改写成 `国家-运营商-延迟ms#原名`，例如 `JP-SoftBank-320ms#原节点名`。

统计只打印在终端，不写进文件。
