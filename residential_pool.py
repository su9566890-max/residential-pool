#!/usr/bin/env python3
"""
免费节点家宽订阅池 — 只输出 residential.txt
====================================================
精简版: 只保留"家宽/移动网络"节点的筛选与导出。
脚本位于仓库根目录, 输出 residential.txt 到根目录。

流程:
  抓取订阅源 → 解析全协议 → 测前凭据去重 → 端口预检
  → sing-box 真实测活 → 出口 IP 分类 → 家宽筛选 → residential.txt

用法:
  # 1. 先把订阅链接写进 sources.txt, 一行一个 (以 # 开头的是注释)
  # 2. 装 sing-box (测活必需, 只用标准库的脚本本身不依赖任何 pip 包):
  #      Linux:  curl -fsSL https://sing-box.app/install.sh | sh
  #      macOS:  brew install sing-box
  #      Windows: 去 https://github.com/SagerNet/sing-box/releases 下对应版本
  # 3. 运行:
  #      python3 residential_pool.py
  #      python3 residential_pool.py --sources sources.txt --output residential.txt
  #      python3 residential_pool.py --max-nodes 300 --test-workers 3 --timeout 12
  #      python3 residential_pool.py --parse-only        # 不测活, 只看解析/去重结果
  #      python3 residential_pool.py --only-country JP   # 只要日本出口
  #      python3 residential_pool.py --no-mobile         # 只要家宽, 不要移动网络

输出:
  只写 residential.txt 一个文件, 一行一个节点链接, 按延迟从低到高排。
  节点名会被改写成 "国家-运营商-延迟ms#原名" 的形式, 方便在客户端里一眼识别。
  统计只打印到终端, 不写进文件, 保证 residential.txt 能直接当订阅导入。

分类说明 (启发式, 不是 100% 准确):
  出口 IP 信息来自 ip-api.com (经节点本身出去查, 不是直连查)。
  - mobile=True                         → 移动网络, 保留
  - hosting=True / proxy=True           → 机房 / 代理, 丢掉
  - ASN/组织命中机房关键词 (见 DATACENTER_KEYWORDS)
                                        → 机房, 丢掉
  - 其余 (mobile=False, hosting=False, 不在机房名单里)
                                        → 家宽, 保留
  注意: 伪住宅 IP (机房 IP 冒充 ISP) 靠这套规则拦不住,
  只能拦掉公开的机房 ASN。真要给 Claude / 银行这类风控严的用,
  还是得单独再验 ASN、欺诈分和反向解析, 别只信这一个文件。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_OUTPUT = ROOT / "residential.txt"

# ---------------------------------------------------------------------------
# 分类: 机房 ASN / 组织关键词 (小写匹配, 命中即判机房)
# ---------------------------------------------------------------------------
DATACENTER_KEYWORDS = [
    "amazon", "aws", "google", "microsoft", "azure", "alibaba", "aliyun",
    "tencent", "huawei", "oracle", "digitalocean", "vultr", "linode",
    "akamai", "cloudflare", "ovh", "hetzner", "contabo", "choopa",
    "psychz", "quadranet", "cogent", "datacamp", "leaseweb", "ionos",
    "1&1", "kamatera", "scaleway", "upcloud", "hostinger", "hosthatch",
    "bandwagon", "gcorelabs", "m247", "datapacket", "velia", "frantech",
    "clouvider", "servermania", "colocrossing", "dedipath",
]

# ip-api 一次返回的字段: 只拿分类要用的, 不多要隐私字段
IP_API_FIELDS = "status,query,country,countryCode,isp,org,as,mobile,hosting,proxy"
IP_API_URL = f"http://ip-api.com/json/?fields={IP_API_FIELDS}"

# 协议白名单: 解析不到 / 不在名单里的直接丢掉, 不进测活
SUPPORTED = {"vmess", "vless", "trojan", "ss", "shadowsocks",
             "hysteria2", "hy2", "tuic", "socks", "socks5", "http"}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def b64decode_padded(s: str) -> bytes | None:
    """容错 base64 解码: 补齐 =, 接受 urlsafe, 失败返回 None。"""
    s = re.sub(r"\s+", "", s.strip())
    if not s:
        return None
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s)
    except (binascii.Error, ValueError):
        return None


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "residential-pool/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    # 订阅常见两种形态: 明文链接 / 整段 base64, 后者先解一次再说
    text = raw.decode("utf-8", errors="ignore").strip()
    if "://" not in text:
        decoded = b64decode_padded(text)
        if decoded:
            text = decoded.decode("utf-8", errors="ignore")
    return text


def load_sources(path: Path) -> list[str]:
    urls: list[str] = []
    env = os.environ.get("SUB_URLS", "").strip()
    if env:
        urls += [u.strip() for u in env.split(",") if u.strip()]
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    # 去重, 保序
    return list(dict.fromkeys(urls))


def extract_links(text: str) -> list[str]:
    """从订阅文本里抠出节点链接。支持明文多行, 也支持整段 base64。"""
    links: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 整行是 base64 块 (订阅常见形态): 解开后再抠
        if "://" not in line:
            decoded = b64decode_padded(line)
            if decoded:
                line = decoded.decode("utf-8", errors="ignore").strip()
                if "://" not in line:
                    continue
            else:
                continue
        # 一行可能含多个链接 / yaml 行里夹着链接
        for m in re.finditer(r"(vmess|vless|trojan|ss|hysteria2|hy2|tuic|socks5?|http)://[^\s\"'<>]+", line):
            links.append(m.group(0).rstrip(","))
    return links


# ---------------------------------------------------------------------------
# 解析: 全协议 → 统一的 node dict
# ---------------------------------------------------------------------------
def _url_parts(link: str):
    return urllib.parse.urlparse(link)


def parse_vmess(link: str) -> dict | None:
    raw = b64decode_padded(link[len("vmess://"):])
    if not raw:
        return None
    try:
        j = json.loads(raw.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    server, port = str(j.get("add", "")), int(j.get("port", 0) or 0)
    if not server or not port:
        return None
    return {
        "protocol": "vmess", "server": server, "port": port,
        "credential": str(j.get("id", "")),
        "name": str(j.get("ps", "") or f"{server}:{port}"),
        "raw": link, "detail": j,
    }


def parse_ss(link: str) -> dict | None:
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    # ss://base64(method:password@host:port) 或 ss://base64(method:password)@host:port
    if "@" not in body:
        decoded = b64decode_padded(body)
        if not decoded:
            return None
        body = decoded.decode("utf-8", errors="ignore")
    else:
        head, tail = body.rsplit("@", 1)
        decoded = b64decode_padded(head)
        if decoded and ":" in decoded.decode("utf-8", errors="ignore"):
            body = decoded.decode("utf-8", errors="ignore") + "@" + tail
    m = re.match(r"^(?P<method>[^:]+):(?P<pw>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)", body)
    if not m:
        return None
    return {
        "protocol": "ss", "server": m.group("host"), "port": int(m.group("port")),
        "credential": f"{m.group('method')}:{m.group('pw')}",
        "name": name or f"{m.group('host')}:{m.group('port')}",
        "raw": link, "detail": {"method": m.group("method"), "password": m.group("pw")},
    }


def parse_url_node(link: str) -> dict | None:
    """vless / trojan / hysteria2 / tuic / socks / http 的通用解析。"""
    u = _url_parts(link)
    proto = u.scheme.lower()
    if proto == "hy2":
        proto = "hysteria2"
    if not u.hostname or not u.port:
        return None
    # 凭据: vless/trojan/hy2/tuic 是 userinfo, socks/http 是 user:pass
    credential = urllib.parse.unquote(u.username or "")
    if u.password:
        credential += f":{urllib.parse.unquote(u.password)}"
    return {
        "protocol": proto, "server": u.hostname, "port": u.port,
        "credential": credential,
        "name": urllib.parse.unquote(u.fragment) or f"{u.hostname}:{u.port}",
        "raw": link,
        "detail": {"query": dict(urllib.parse.parse_qsl(u.query)),
                   "username": u.username, "password": u.password},
    }


def parse_node(link: str) -> dict | None:
    scheme = link.split("://", 1)[0].lower()
    if scheme not in SUPPORTED:
        return None
    try:
        if scheme == "vmess":
            return parse_vmess(link)
        if scheme == "ss":
            return parse_ss(link)
        return parse_url_node(link)
    except (ValueError, TypeError):
        return None


def dedup_key(node: dict) -> str:
    """测前凭据去重: 同协议 + 同服务器 + 同端口 + 同凭据 = 同一个节点。"""
    return "|".join([
        node["protocol"], node["server"].lower(), str(node["port"]),
        node["credential"],
    ])


def dedup(nodes: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for n in nodes:
        k = dedup_key(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# 端口预检: TCP 能连上才值得上 sing-box, 2 秒超时, 50 并发
# ---------------------------------------------------------------------------
def port_open(node: dict, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((node["server"], node["port"]), timeout=timeout):
            return True
    except OSError:
        return False


def port_precheck(nodes: list[dict], workers: int = 50) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        alive = [n for n, ok in zip(nodes, ex.map(port_open, nodes)) if ok]
    return alive


# ---------------------------------------------------------------------------
# sing-box: 节点 → outbound 配置
# ---------------------------------------------------------------------------
def _tls_from_query(q: dict) -> dict | None:
    if q.get("security", "") not in ("tls", "reality") and q.get("tls", "") != "1":
        return None
    tls: dict = {"enabled": True, "server_name": q.get("sni") or q.get("serverName") or "",
                 "insecure": q.get("allowInsecure", "0") in ("1", "true")}
    if q.get("security") == "reality":
        tls["reality"] = {"enabled": True, "public_key": q.get("pbk", ""),
                          "short_id": q.get("sid", "")}
    return tls


def _transport_from_query(q: dict) -> dict | None:
    net = q.get("type", "") or q.get("network", "") or "tcp"
    if net == "tcp":
        return None
    if net == "ws":
        return {"type": "ws", "path": q.get("path", "/"),
                "headers": {"Host": q["host"]} if q.get("host") else {}}
    if net == "grpc":
        return {"type": "grpc", "service_name": q.get("serviceName", "")}
    if net in ("http", "h2"):
        return {"type": "http", "host": [q["host"]] if q.get("host") else [],
                "path": q.get("path", "/")}
    return None


def to_singbox_outbound(node: dict) -> dict | None:
    proto, q = node["protocol"], node.get("detail", {}).get("query", {})
    base = {"tag": "proxy", "server": node["server"], "server_port": node["port"]}
    if proto == "vmess":
        j = node["detail"]
        ob = {**base, "type": "vmess", "uuid": j.get("id", ""),
              "security": j.get("scy", "auto"), "alter_id": int(j.get("aid", 0) or 0)}
        if j.get("tls") in ("tls", "reality"):
            ob["tls"] = {"enabled": True, "server_name": j.get("sni", ""),
                         "insecure": False}
            if j.get("tls") == "reality":
                ob["tls"]["reality"] = {"enabled": True,
                                        "public_key": j.get("pbk", ""),
                                        "short_id": j.get("sid", "")}
        net = j.get("net", "tcp")
        if net == "ws":
            ob["transport"] = {"type": "ws", "path": j.get("path", "/"),
                               "headers": {"Host": j["host"]} if j.get("host") else {}}
        elif net == "grpc":
            ob["transport"] = {"type": "grpc", "service_name": j.get("path", "")}
        return ob
    if proto == "vless":
        ob = {**base, "type": "vless", "uuid": node["credential"],
              "flow": q.get("flow", "")}
        tls = _tls_from_query(q)
        if tls:
            ob["tls"] = tls
        tr = _transport_from_query(q)
        if tr:
            ob["transport"] = tr
        return ob
    if proto == "trojan":
        ob = {**base, "type": "trojan", "password": node["credential"]}
        tls = _tls_from_query(q) or {"enabled": True,
                                     "server_name": q.get("sni", node["server"])}
        ob["tls"] = tls
        tr = _transport_from_query(q)
        if tr:
            ob["transport"] = tr
        return ob
    if proto == "ss":
        return {**base, "type": "shadowsocks",
                "method": node["detail"]["method"],
                "password": node["detail"]["password"]}
    if proto == "hysteria2":
        ob = {**base, "type": "hysteria2", "password": node["credential"],
              "tls": {"enabled": True, "server_name": q.get("sni", node["server"]),
                      "insecure": q.get("insecure", "0") in ("1", "true")}}
        if q.get("obfs"):
            ob["obfs"] = {"type": q["obfs"], "password": q.get("obfs-password", "")}
        return ob
    if proto == "tuic":
        return {**base, "type": "tuic", "uuid": node["credential"],
                "password": node["detail"].get("password") or "",
                "tls": {"enabled": True, "server_name": q.get("sni", node["server"])}}
    if proto in ("socks", "socks5"):
        ob = {**base, "type": "socks", "version": "5"}
        if node["detail"].get("username"):
            ob["username"] = node["detail"]["username"]
            ob["password"] = node["detail"].get("password") or ""
        return ob
    if proto == "http":
        return {**base, "type": "http"}
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_node(node: dict, singbox: str, timeout: int = 12) -> dict | None:
    """sing-box 真实测活: 起一个 mixed 入站, 经节点 curl ip-api, 拿出口信息 + 延迟。
    成功返回 node + exit/latency, 失败返回 None。"""
    outbound = to_singbox_outbound(node)
    if outbound is None:
        return None
    port = free_port()
    config = {
        "log": {"level": "error"},
        "inbounds": [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": "proxy"},
    }
    with tempfile.TemporaryDirectory(prefix="sb-test-") as td:
        cfg = os.path.join(td, "config.json")
        Path(cfg).write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.Popen(
            [singbox, "run", "-c", cfg], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            # 等 mixed 入站起来 (最多 5 秒)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                return None
            start = time.monotonic()
            r = subprocess.run(
                ["curl", "-s", "-m", str(timeout),
                 "--socks5-hostname", f"127.0.0.1:{port}", IP_API_URL],
                capture_output=True, text=True, timeout=timeout + 5)
            latency_ms = int((time.monotonic() - start) * 1000)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            info = json.loads(r.stdout)
            if info.get("status") != "success" or not info.get("query"):
                return None
            return {**node, "exit": info, "latency_ms": latency_ms}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# 出口 IP 分类
# ---------------------------------------------------------------------------
def classify_exit(info: dict) -> tuple[str, str]:
    """返回 (kind, reason)。kind: residential / mobile / datacenter。"""
    if info.get("mobile"):
        return "mobile", "mobile=True"
    if info.get("hosting"):
        return "datacenter", "hosting=True"
    if info.get("proxy"):
        return "datacenter", "proxy=True"
    hay = f"{info.get('isp', '')} {info.get('org', '')} {info.get('as', '')}".lower()
    for kw in DATACENTER_KEYWORDS:
        if kw in hay:
            return "datacenter", f"ASN/组织命中机房关键词: {kw}"
    return "residential", "mobile/hosting 均为 False, 且不在机房名单里"


# ---------------------------------------------------------------------------
# 改名 + 输出
# ---------------------------------------------------------------------------
def rename_link(node: dict, new_name: str) -> str:
    """只改节点名, 不动服务器/凭据。vmess 重编 base64, 其余改 fragment。"""
    if node["protocol"] == "vmess":
        j = dict(node["detail"])
        j["ps"] = new_name
        payload = base64.b64encode(
            json.dumps(j, ensure_ascii=False).encode()).decode()
        return f"vmess://{payload}"
    link = node["raw"]
    base = link.split("#", 1)[0]
    return f"{base}#{urllib.parse.quote(new_name, safe='')}"


def short_isp(info: dict) -> str:
    isp = (info.get("isp") or info.get("org") or "unknown").strip()
    # 去掉常见后缀, 太长截断, 避免节点名臃肿
    isp = re.sub(r"\s+(Co\.?,?|Ltd\.?|Inc\.?|Corporation|Company)$", "", isp)
    return isp[:18] if len(isp) > 18 else isp


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="免费节点家宽订阅池 — 只输出 residential.txt")
    ap.add_argument("--sources", default=str(DEFAULT_SOURCES), help="订阅源文件, 一行一个 URL")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出文件 (默认 residential.txt)")
    ap.add_argument("--max-nodes", type=int, default=500, help="测活上限, 默认 500")
    ap.add_argument("--test-workers", type=int, default=3, help="sing-box 并发测活数, 默认 3")
    ap.add_argument("--timeout", type=int, default=12, help="单个节点测活超时(秒), 默认 12")
    ap.add_argument("--parse-only", action="store_true", help="只解析/去重/预检, 不跑 sing-box")
    ap.add_argument("--only-country", default="", help="只保留指定国家代码, 如 JP")
    ap.add_argument("--no-mobile", action="store_true", help="不要移动网络, 只要家宽")
    args = ap.parse_args()

    urls = load_sources(Path(args.sources))
    if not urls:
        print(f"订阅源为空: {args.sources} 里一行一个 URL, 或用 SUB_URLS 环境变量传入。", file=sys.stderr)
        return 2
    log(f"订阅源 {len(urls)} 个")

    # 1. 抓取 + 解析
    parsed: list[dict] = []
    for url in urls:
        try:
            text = fetch_text(url)
        except Exception as e:  # noqa: BLE001 - 订阅源挂了就跳过, 不中断全程
            log(f"抓取失败, 跳过: {url} ({e})")
            continue
        for link in extract_links(text):
            node = parse_node(link)
            if node:
                parsed.append(node)
    log(f"解析出 {len(parsed)} 个节点")

    # 2. 测前凭据去重
    nodes = dedup(parsed)
    log(f"去重后 {len(nodes)} 个 (去掉 {len(parsed) - len(nodes)} 个重复凭据)")

    # 3. 端口预检
    nodes = port_precheck(nodes)
    log(f"端口预检通过 {len(nodes)} 个")
    if not nodes:
        print("没有可测的节点, 不写文件。", file=sys.stderr)
        return 1

    nodes = nodes[:args.max_nodes]

    # 4. sing-box 真实测活
    if args.parse_only:
        log("--parse-only: 跳过 sing-box 测活, 不输出 residential.txt")
        for n in nodes[:20]:
            print(f"  {n['protocol']}://{n['server']}:{n['port']}  {n['name']}")
        return 0

    singbox = shutil.which("sing-box")
    if not singbox:
        print("没找到 sing-box, 测活做不了。装一个再跑:\n"
              "  Linux: curl -fsSL https://sing-box.app/install.sh | sh\n"
              "  macOS: brew install sing-box", file=sys.stderr)
        return 2

    log(f"sing-box 测活开始: {len(nodes)} 个, 并发 {args.test_workers}, 超时 {args.timeout}s")
    tested: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.test_workers) as ex:
        futs = {ex.submit(test_node, n, singbox, args.timeout): n for n in nodes}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            if done % 20 == 0 or done == len(nodes):
                log(f"测活进度 {done}/{len(nodes)}")
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001 - 单个节点异常不中断全程
                r = None
            if r:
                tested.append(r)
    log(f"测活通过 {len(tested)} 个 (其余超时 / 连不上 / 出口查询失败)")

    # 5. 出口 IP 分类 + 6. 家宽筛选
    kept: list[dict] = []
    stats: dict[str, int] = {"residential": 0, "mobile": 0, "datacenter": 0}
    for n in tested:
        kind, reason = classify_exit(n["exit"])
        stats[kind] += 1
        n["kind"] = kind
        n["reason"] = reason
        if args.only_country and n["exit"].get("countryCode", "").upper() != args.only_country.upper():
            continue
        if kind == "residential" or (kind == "mobile" and not args.no_mobile):
            kept.append(n)
    log(f"分类: 家宽 {stats['residential']} / 移动 {stats['mobile']} / 机房 {stats['datacenter']}")

    # 7. 输出 residential.txt (只写这一个文件)
    kept.sort(key=lambda n: n["latency_ms"])
    lines = []
    for n in kept:
        cc = n["exit"].get("countryCode", "??")
        name = f"{cc}-{short_isp(n['exit'])}-{n['latency_ms']}ms#{n['name']}"
        lines.append(rename_link(n, name))
    Path(args.output).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    log(f"已写 {args.output}: {len(lines)} 个家宽/移动节点")
    if not lines:
        print("提醒: 筛完一个都没剩, residential.txt 是空的。可能订阅源里本来就没家宽, "
              "或 ip-api 把出口都判成了机房, 可先用 --parse-only 看看解析量。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
