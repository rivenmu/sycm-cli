"""自助分析「取数报表」（个人空间）的列表与下载。

接口族：https://sycm.taobao.com/lyone/fetchData
  getReportList.json    报表列表（space=personal 才是个人空间）
  download.json         触发服务端出数（reportId）
  queryDownloadUrl.json 轮询签名直链（reportId）

下载是服务端异步出数：先触发，再轮询拿 OSS 直链，最后直接从链接取文件。
报表的日期区间由报表自身配置决定，命令不接 --date。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from ...core.errors import TbError

REFERER = "https://sycm.taobao.com/adm/v3/micro/auto_analysis/my_space"
DEFAULT_OUT = Path.home() / "Downloads" / "sycm-exports" / "datafetch"
DEFAULT_CHANNEL = "tmall"
POLL_INTERVAL = 4
POLL_TIMEOUT = 150
MIN_BYTES = 2000

# --tree 用：目录名一律「中文_英文」，渠道/业务域/维度/粒度各一层
CHANNEL_NAME = {"tmall": "天猫", "taobao": "淘宝"}
DOMAIN = {"品类": "category", "客户": "customer", "商品": "item", "店铺": "shop", "无界": "wujie"}
DIMENSION = {
    "整体": "overall",
    "核心指标": "core_metrics",
    "流量来源": "traffic_source",
    "流量来源(新版)": "traffic_source_new",
    "流量来源详情(新版)": "traffic_source_detail_new",
    "流量来源-4品": "traffic_source_top4",
    "流失竞店分布": "lost_shop_distribution",
    "账户": "account",
    "关键词": "keyword",
}
GRANULARITY = {"日": ("按日", "daily"), "周": ("按周", "weekly"), "月": ("按月", "monthly")}


def _deps():
    """延迟导入：cli 注册本模块，本模块又要用 cli 的请求与校验，顶层导入会成环。"""
    from . import cli

    return cli


def _cookie_has(hold: Any, name: str) -> str:
    return hold.get(name, "") or ""


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    cli = _deps()
    cookies = cli.load_taobao_cookies()
    merged: dict[str, Any] = {"_": str(int(time.time() * 1000))}
    token = _cookie_has(cookies, "_tb_token_")
    if token:
        merged["token"] = token
    merged.update(params or {})
    return cli._api_get(path, merged, cookies, referer=REFERER)


def fetch_reports() -> list[dict[str, Any]]:
    """个人空间全部报表（分页拉完）。"""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = _get("/lyone/fetchData/getReportList.json",
                    {"pageSize": 100, "currentPage": page, "space": "personal"})
        data = resp.get("data") or {}
        rows = data.get("reportList") or []
        out.extend(rows)
        total = int(data.get("totalCnt") or 0)
        if not rows or len(out) >= total:
            break
        page += 1
    return out


def _date(value: Any) -> str:
    return str(value or "")[:10]


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "报表"


def _matches(report: dict[str, Any], token: str) -> bool:
    return str(report.get("id")) == token or token in str(report.get("reportName") or "")


def _resolve(reports: list[dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    """按 ID 或名字定位报表；名字做包含匹配，命中多条时报候选并停。"""
    picked: list[dict[str, Any]] = []
    for token in tokens:
        token = token.strip()
        hit = [r for r in reports if _matches(r, token)]
        exact = [r for r in hit if str(r.get("reportName")) == token]
        if exact:
            hit = exact
        if not hit:
            raise RuntimeError(f"个人空间里找不到报表「{token}」")
        if len(hit) > 1:
            names = "、".join(f"{r['reportName']}({r['id']})" for r in hit)
            raise RuntimeError(f"「{token}」匹配到多份报表：{names}")
        if hit[0] not in picked:
            picked.append(hit[0])
    return picked


def _trigger(report_id: int) -> None:
    try:
        _get("/lyone/fetchData/download.json", {"reportId": report_id})
    except TbError as exc:
        # 服务端同一报表只排队一个任务；旧任务还在跑时不算失败，接着轮询即可
        if "已有任务" not in str(exc):
            raise


def _poll_url(report_id: int) -> str:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp = _get("/lyone/fetchData/queryDownloadUrl.json", {"reportId": report_id})
        except TbError as exc:
            if "已有任务" in str(exc):
                continue
            raise
        url = (resp.get("data") or {}).get("url")
        if url:
            return _deps()._validated_https_url(url)
    raise RuntimeError(f"等下载链接超时（{POLL_TIMEOUT}s），报表可能还在生成")


def _download(url: str, dest: Path) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)  # nosec B310 — full URL 已在调用方校验为 HTTPS
    with open(tmp, "rb") as fh:
        magic = fh.read(4)
    if magic != b"PK\x03\x04":
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载到的不是 xlsx（文件头不对），已丢弃")
    tmp.replace(dest)
    return dest.stat().st_size


def _report_name(report: dict[str, Any], channel: str = DEFAULT_CHANNEL) -> str:
    base = f"{_safe_name(report.get('reportName') or '报表')}_{_date(report.get('startDate'))}_{_date(report.get('endDate'))}.xlsx"
    return f"{channel}_{base}"


def cmd_list(_args: argparse.Namespace) -> None:
    reports = fetch_reports()
    print(f"# 取数报表（个人空间）共 {len(reports)} 份")
    print(f"{'ID':<10} {'报表名':<28} {'维度':<12} {'区间':<24} 指标")
    for r in reports:
        try:
            count = len(json.loads(r.get("indicators") or "[]"))
        except ValueError:
            count = 0
        dimension = f"{r.get('dataType') or ''}/{r.get('dataDimension') or ''}"
        span = f"{_date(r.get('startDate'))}~{_date(r.get('endDate'))}"
        print(f"{r.get('id'):<10} {str(r.get('reportName'))[:28]:<28} {dimension:<12} {span:<24} {count}")


def _tree_dir(tree: Path, report: dict[str, Any], channel: str) -> Path:
    """报表名 → <中文_英文渠道>/<中文_英文域>/<中文_英文维度>/<中文_英文粒度>。"""
    name = str(report.get("reportName") or "")
    parts = [p for p in name.split("-") if p]
    granularity = GRANULARITY["日"]
    if parts and parts[-1] in GRANULARITY:
        granularity = GRANULARITY[parts.pop()]
    if len(parts) < 2:
        raise RuntimeError(f"报表「{name}」的名字不是「业务域-维度-粒度」，无法按目录归档")
    domain_cn = parts[0]
    domain = DOMAIN.get(domain_cn)
    if not domain:
        raise RuntimeError(f"报表「{name}」的业务域「{domain_cn}」还没有英文名，先在 datafetch.py 的 DOMAIN 里补一行")
    dimension_cn = "-".join(parts[1:])
    dimension = DIMENSION.get(dimension_cn)
    if not dimension:
        raise RuntimeError(f"报表「{name}」的维度「{dimension_cn}」还没有英文名，先在 datafetch.py 的 DIMENSION 里补一行")
    channel_cn = CHANNEL_NAME.get(channel, channel)
    return (tree / f"{channel_cn}_{channel}"
            / f"{domain_cn}_{domain}"
            / f"{dimension_cn}_{dimension}"
            / f"{granularity[0]}_{granularity[1]}")


def _data_month(path: Path, report: dict[str, Any]) -> str:
    """数据末月：取表里「统计日期」列的最大日期。有的报表行序不是倒序、有的缺这列，
    所以扫全表取最大，读不到再退回报表配置的结束日。"""
    try:
        import re as _re
        import warnings

        import openpyxl

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = ws.iter_rows(values_only=True)
            header = [str(h or "") for h in next(rows, ())]
            index = next((i for i, h in enumerate(header) if "统计日期" in h), None)
            if index is None:
                index = next((i for i, h in enumerate(header) if "日期" in h), None)
            latest = ""
            if index is not None:
                for row in rows:
                    if index >= len(row):
                        continue
                    value = row[index]
                    if hasattr(value, "strftime"):
                        text = value.strftime("%Y-%m-%d")
                    else:
                        text = str(value or "")[:10]
                    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) and text > latest:
                        latest = text
            wb.close()
            if latest:
                year, month = latest[:7].split("-")
                return f"{year}年_{int(month)}月"
    except Exception:  # 打不开 Excel 时用报表配置兜底，不让归档挡住下载
        pass
    end = _date(report.get("endDate"))[:7]
    if "-" in end:
        year, month = end.split("-")
        return f"{year}年_{int(month)}月"
    return "未知月份_unknown"


def _find_existing(tree: Path, filename: str) -> Path | None:
    if not tree.is_dir():
        return None
    for hit in tree.rglob(filename):
        if hit.is_file() and hit.stat().st_size >= MIN_BYTES:
            return hit
    return None


def cmd_download(args: argparse.Namespace) -> None:
    channel = args.channel
    tree = Path(args.tree).expanduser() if args.tree else None
    out_dir = Path(args.out).expanduser() if args.out else DEFAULT_OUT
    staging = tree or out_dir
    staging.mkdir(parents=True, exist_ok=True)
    reports = _resolve(fetch_reports(), args.targets)
    plans: dict[int, Path] = {}
    if tree:
        for report in reports:  # 先把目录映射全部验证一遍，避免下完才报错
            plans[int(report["id"])] = _tree_dir(tree, report, channel)

    done: list[str] = []
    for report in reports:
        name = str(report.get("reportName"))
        filename = _report_name(report, channel)
        print(f"[{len(done) + 1}/{len(reports)}] {name} ({report.get('id')}) "
              f"{_date(report.get('startDate'))}~{_date(report.get('endDate'))}", file=sys.stderr)
        existing = _find_existing(tree, filename) if tree else (out_dir / filename)
        if existing and existing.exists() and existing.stat().st_size >= MIN_BYTES and not args.force:
            print(f"    已存在，跳过：{existing}", file=sys.stderr)
            done.append(f"skip  {existing}")
            continue
        tmp = staging / f".downloading-{filename}"
        try:
            _trigger(int(report["id"]))
            url = _poll_url(int(report["id"]))
            size = _download(url, tmp)
            if tree:
                dest_dir = plans[int(report["id"])] / _data_month(tmp, report)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / filename
                tmp.replace(dest)
            else:
                dest = out_dir / filename
                tmp.replace(dest)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            print(f"✗ {name}: {exc}", file=sys.stderr)
            print(f"\n# 已处理 {len(done)}/{len(reports)}，在「{name}」失败：{exc}")
            for line in done:
                print(line)
            raise SystemExit(1) from None
        print(f"    ok  {size / 1024:.1f} KB → {dest}", file=sys.stderr)
        done.append(f"ok    {dest}  {size / 1024:.1f} KB")

    print(f"# 完成 {len(done)} 份" + (f"，根目录 {tree}" if tree else f"，目录 {out_dir}"))
    for line in done:
        print(line)


def register(sp: argparse._SubParsersAction) -> None:
    df = sp.add_parser("datafetch", help="自助分析取数报表：列表 + 下载（个人空间，只读）")
    df.set_defaults(func=lambda _args: df.print_help())
    sub = df.add_subparsers(dest="datafetch_cmd", metavar="list|download")

    ls = sub.add_parser("list", help="列出个人空间里的取数报表")
    ls.set_defaults(func=cmd_list)

    dl = sub.add_parser("download", help="下载指定报表（名字或 ID，可多个）")
    dl.add_argument("targets", nargs="+", metavar="报表名|ID", help="报表名（可只写一部分）或报表 ID")
    where = dl.add_mutually_exclusive_group()
    where.add_argument("--out", help=f"平铺输出目录（默认 {DEFAULT_OUT}）")
    where.add_argument("--tree", metavar="根目录",
                       help="按 <中文_英文渠道>/<域>/<维度>/<粒度>/<年_月> 分层归档，如 --tree /Volumes/RivenCode/iqair_data_source")
    dl.add_argument("--channel", default=DEFAULT_CHANNEL,
                    help=f"渠道代码，用于目录第一层和文件名前缀（默认 {DEFAULT_CHANNEL}，目录显示为「天猫_tmall」）")
    dl.add_argument("--force", action="store_true", help="已存在也重新下载")
    dl.set_defaults(func=cmd_download)
