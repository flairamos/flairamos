#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_stats.py — 使用 GitHub 官方 API 做个人数据分析，并生成 README 用的 SVG 卡片。

数据来源（全部为 GitHub 官方接口，任何第三方统计服务挂掉都不影响这里）：
  · REST   GET /users/{login}                      关注数 / 公开仓库数 / 注册时间
  · GraphQL user(login:).repositories(...)         各仓库代码字节 → 语言占比
  · GraphQL user(login:).contributionsCollection   贡献日历 → 近一年 commits / PR / Issue / 连续贡献

输出（默认写到 assets/ 目录，README 用仓库内相对路径引用 —— 能打开 github.com 页面的网络就一定能加载）：
  · assets/stats-light.svg / stats-dark.svg        综合数据卡
  · assets/languages-light.svg / languages-dark.svg 语言占比卡
  · assets/streak-light.svg / streak-dark.svg      连续贡献（Streak）卡
  · assets/stats.json                              本轮分析的原始指标（JSON）

用法：
  python3 scripts/generate_stats.py
      —— 自动读取环境变量 STATS_TOKEN / PAT_TOKEN / GITHUB_TOKEN，取不到就自动退回种子数据
  python3 scripts/generate_stats.py --token ghp_xxxx
      —— 显式指定 PAT（classic，勾 read:user 即可；要统计私有仓库再加 repo 权限）
  python3 scripts/generate_stats.py --seed scripts/seed_stats.json
      —— 强制使用种子数据渲染占位卡片（不联网）

在 GitHub Actions 里使用：见 .github/workflows/update-stats.yml（每天自动跑一次）。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_REST = "https://api.github.com"
API_GQL = "https://api.github.com/graphql"
UA = "profile-stats-generator (README cards powered by official GitHub API)"

FONT = "Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif"

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "fg": "#c9d1d9",
        "muted": "#8b949e",
        "line": "rgba(240,246,252,0.10)",
        "track": "rgba(240,246,252,0.06)",
        "accent": "#00ADD8",
        "gold": "#e3b341",
        "border": "rgba(240,246,252,0.12)",
    },
    "light": {
        "bg": "#ffffff",
        "fg": "#24292f",
        "muted": "#57606a",
        "line": "rgba(31,35,40,0.15)",
        "track": "rgba(31,35,40,0.05)",
        "accent": "#0f8fb8",
        "gold": "#bf8700",
        "border": "rgba(31,35,40,0.15)",
    },
}

LANG_FALLBACK_COLORS = {
    "Go": "#00ADD8",
    "Java": "#b07219",
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "CSS": "#563d7c",
    "HTML": "#e34c26",
    "Shell": "#89e051",
    "C": "#555555",
    "C++": "#f34b7d",
    "Rust": "#dea584",
    "SQL": "#e38c00",
    "Vue": "#41b883",
    "Lua": "#000080",
}

# 贡献日历色阶（对齐 GitHub 官方配色）
CAL_LEVEL_COLORS = {
    "light": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    "dark": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
}


# ────────────────────────────── 请求封装 ──────────────────────────────────

def _headers(token: str) -> dict:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def http_json(url: str, token: str = "", retries: int = 3):
    req = urllib.request.Request(url, headers=_headers(token))
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time()) + 1) if reset else 20 * (i + 1)
                print(f"  ⚠ REST 限流，等待 {min(wait, 90)}s 后重试 ...", file=sys.stderr)
                time.sleep(min(wait, 90))
                last = e
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(4 * (i + 1))
    raise last


def graphql(query: str, variables: dict, token: str):
    if not token:
        raise RuntimeError("GraphQL 需要认证 token（REST 公开数据可以匿名，GraphQL 不行）")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        API_GQL,
        data=body,
        headers={**_headers(token), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in data["errors"])
        raise RuntimeError(f"GraphQL 错误: {msgs[:400]}")
    return data["data"]


# ────────────────────────────── 数据采集 ──────────────────────────────────

REPO_QUERY = """
query($login:String!,$cursor:String) {
  user(login:$login) {
    createdAt
    followers { totalCount }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      totalPullRequestReviewContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(first: 100, after: $cursor, isFork: false,
                 ownerAffiliations: OWNER,
                 orderBy: {field: STARGAZERS, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        stargazerCount
        forkCount
        isArchived
        primaryLanguage { name color }
        languages(first: 100, orderBy: {field: SIZE, direction: DESC}) {
          totalSize
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def collect(user: str, token: str) -> dict:
    """官方 API 采集 + 计算分析指标（语言占比、连续贡献、周分布）。"""
    print(f"▸ 采集官方数据：REST GET /users/{user}")
    rest_user = http_json(f"{API_REST}/users/{user}", token=token)

    langs: dict[str, dict] = {}
    total_stars = total_forks = 0
    top_repo, top_stars = None, -1
    repo_count = 0
    cursor = None
    created_at = rest_user.get("created_at")

    while True:
        print(f"▸ GraphQL repositories 分页 (cursor={cursor})")
        data = graphql(REPO_QUERY, {"login": user, "cursor": cursor}, token)
        u = data["user"]
        created_at = created_at or u.get("createdAt")
        cc = u["contributionsCollection"]
        cal = cc["contributionCalendar"]
        repos = u["repositories"]
        for node in repos["nodes"]:
            repo_count += 1
            total_stars += node.get("stargazerCount", 0)
            total_forks += node.get("forkCount", 0)
            if node.get("isArchived"):
                continue
            if node.get("stargazerCount", 0) > top_stars:
                top_stars = node["stargazerCount"]
                top_repo = node["name"]
            total = (node.get("languages") or {}).get("totalSize") or 0
            if not total:
                continue
            for edge in node["languages"]["edges"]:
                name = (edge.get("node") or {}).get("name") or "Other"
                color = (edge.get("node") or {}).get("color")
                item = langs.setdefault(
                    name,
                    {"bytes": 0, "color": color, "repos": 0},
                )
                item["bytes"] += edge.get("size", 0)
                item["repos"] += 1
                if not item["color"]:
                    item["color"] = LANG_FALLBACK_COLORS.get(name)

        if repos["pageInfo"]["hasNextPage"]:
            cursor = repos["pageInfo"]["endCursor"]
        else:
            cc_final = cc
            break
    else:  # pragma: no cover
        cc_final = None

    # 语言占比
    lang_bytes = sum(v["bytes"] for v in langs.values())
    languages = sorted(
        (
            {
                "name": name,
                "bytes": v["bytes"],
                "pct": round(v["bytes"] * 100.0 / lang_bytes, 2) if lang_bytes else 0.0,
                "repos": v["repos"],
                "color": v["color"] or LANG_FALLBACK_COLORS.get(name, "#00ADD8"),
            }
            for name, v in langs.items()
        ),
        key=lambda x: -x["bytes"],
    )

    # 贡献日历 → 周分布 / streak
    days = []
    for wk in cc_final["contributionCalendar"]["weeks"]:
        for d in wk["contributionDays"]:
            days.append((d["date"][:10], int(d.get("contributionCount", 0))))
    days.sort()

    weekday = [0] * 7  # 0 = 周一
    for ds, c in days:
        weekday[date.fromisoformat(ds).weekday()] += c

    longest = cur = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    idx = len(days) - 1
    if idx >= 0 and days[idx][1] == 0:  # 今天还没写代码时，允许以"昨天"收尾
        idx -= 1
    while idx >= 0 and days[idx][1] > 0:
        cur += 1
        idx -= 1

    years = None
    if created_at:
        created = date.fromisoformat(created_at[:10])
        years = round(((date.today() - created).days) / 365.25, 1)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_source": "GitHub official API (REST /users/{login} + GraphQL v4)",
        "user": user,
        "followers": rest_user.get("followers", 0),
        "public_repos": rest_user.get("public_repos", 0),
        "created_at": created_at,
        "github_age_years": years,
        "stars": total_stars,
        "forks": total_forks,
        "top_repo": {"name": top_repo, "stars": max(top_stars, 0) if top_repo else 0},
        "contributions_last_year": {
            "commits": cc_final["totalCommitContributions"],
            "pull_requests": cc_final["totalPullRequestContributions"],
            "issues": cc_final["totalIssueContributions"],
            "reviews": cc_final["totalPullRequestReviewContributions"],
            "calendar_total": cc_final["contributionCalendar"]["totalContributions"],
        },
        "streak": {"current": cur, "longest": longest},
        "weekday": weekday,
        "_calendar_days": [(ds, c) for ds, c in days],  # 仅用于渲染，写 stats.json 前会被移除
        "languages": languages,
        "language_bytes_total": lang_bytes,
        "private_tokens_see_public_only": "想统计私有仓库请用带 repo 权限的 PAT（见 docs/PROFILE.md）",
    }


def collect_seed(path: Path) -> dict:
    print(f"▸ 使用种子数据：{path}")
    seed = json.loads(path.read_text(encoding="utf-8"))
    seed.setdefault("generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    seed.setdefault("data_source", f"seed {path.name}（占位渲染，Actions 首次运行后将被官方接口真实数据覆盖）")
    return seed


# ────────────────────────────── SVG 渲染 ──────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt(n) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def show_seed_note(th, x, y, w) -> str:
    return (
        f'<text x="{x}" y="{y}" font-size="11" font-family="{FONT}" fill="{th["muted"]}"'
        f' text-anchor="end">{esc("种子占位 · Actions 首次运行后更新为官方接口真实数据")}</text>'
    )


def stat_cell(th, x, y, label, value) -> str:
    return (
        f'<text x="{x}" y="{y}" font-size="12" font-family="{FONT}" fill="{th["muted"]}">{esc(label)}</text>'
        f'<text x="{x}" y="{y + 22}" font-size="20" font-family="{FONT}" fill="{th["fg"]}" font-weight="700">{esc(value)}</text>'
    )


def card(width, height, th) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
        f' viewBox="0 0 {width} {height}" role="img">'
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{th["bg"]}" stroke="{th["border"]}"/>'
    )


def header(th, title, sub) -> str:
    return (
        f'<circle cx="32" cy="34" r="8" fill="{th["accent"]}"/>'
        f'<text x="48" y="40" font-size="19" font-family="{FONT}" fill="{th["fg"]}" font-weight="700">{esc(title)}</text>'
        f'<text x="418" y="40" font-size="11" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">{esc(sub)}</text>'
    )


def stats_card(d: dict, th: dict, seeded: bool) -> str:
    w, h = 430, 272
    c = cc = d.get("contributions_last_year") or {}
    cells = [
        ("近一年 Commits", fmt(cc.get("commits"))),
        ("近一年 PR", fmt(cc.get("pull_requests"))),
        ("近一年 Issues", fmt(cc.get("issues"))),
        ("近一年 Reviews", fmt(cc.get("reviews"))),
        ("收到的 Stars", fmt(d.get("stars", 0))),
        ("获 Fork", fmt(d.get("forks", 0))),
        ("公开仓库", fmt(d.get("public_repos"))),
        ("Followers", fmt(d.get("followers"))),
    ]
    body = [card(w, h, th), header(th, "GitHub Stats", f"flairamos · 官方 API · 每日更新")]
    for i, (label, val) in enumerate(cells):
        row, col = i // 2, i % 2
        body.append(stat_cell(th, 30 + col * 215, 92 + row * 36, label, val))
    line_y = 224
    body.append(f'<line x1="28" y1="{line_y}" x2="{w - 28}" y2="{line_y}" stroke="{th["line"]}"/>')
    top = d.get("top_repo") or {}
    age = d.get("github_age_years")
    extra = []
    if top.get("name"):
        extra.append(f"最热仓库 {top['name']} ★{fmt(top.get('stars', 0))}")
    if age is not None:
        extra.append(f"GitHub 龄 {age:g} 年")
    if extra:
        body.append(
            f'<text x="30" y="{line_y + 26}" font-size="13" font-family="{FONT}" fill="{th["muted"]}">{esc(" · ".join(extra))}</text>'
        )
    if seeded:
        body.append(show_seed_note(th, w - 30, line_y + 44, w))
    body.append("</svg>")
    return "".join(body)


def languages_card(d: dict, th: dict, seeded: bool, topn: int = 6) -> str:
    w, h = 430, 280
    body = [card(w, h, th), header(th, "语言占比", "口径：自建非 fork 仓库 · 代码字节数")]
    langs = (d.get("languages") or [])[:topn]
    total_bytes = d.get("language_bytes_total") or 0
    if not langs:
        body.append(
            f'<text x="{w / 2}" y="150" font-size="14" font-family="{FONT}" fill="{th["muted"]}" text-anchor="middle">'
            f'{esc("暂无语言数据 — 运行一次 update-stats workflow 后自动生成")}</text>'
        )
    else:
        for i, it in enumerate(langs):
            y = 92 + i * 30
            body.append(f'<text x="30" y="{y}" font-size="12.5" font-family="{FONT}" fill="{th["fg"]}">{esc(it["name"])}</text>')
            body.append(
                f'<text x="{w - 30}" y="{y}" font-size="12" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">'
                f'{it["pct"]:.1f}%{esc(" · " + ("%.1f" % (it["bytes"] / 1024) + " KB") if it["bytes"] < 1024 * 1024 else "%.1f MB" % (it["bytes"] / 1024 / 1024))}</text>'
            )
            body.append(f'<rect x="30" y="{y + 6}" width="370" height="7" rx="3.5" fill="{th["track"]}"/>')
            body.append(f'<rect x="30" y="{y + 6}" width="{max(370 * it["pct"] / 100.0, 2)}" height="7" rx="3.5" fill="{it["color"] or th["accent"]}"/>')
    note = f"共 {sum(L['repos'] for L in langs)} 个仓库参与统计 · 字节数合计 {fmt(total_bytes)}"
    body.append(f'<text x="30" y="{h - 22}" font-size="11" font-family="{FONT}" fill="{th["muted"]}">{esc(note)}</text>')
    if seeded:
        body.append(show_seed_note(th, w - 30, h - 22, w))
    body.append("</svg>")
    return "".join(body)


def streak_card(d: dict, th: dict, seeded: bool) -> str:
    w, h = 430, 232
    body = [card(w, h, th), header(th, "连续贡献 Streak", "近 365 天贡献日历")]
    cal_total = (d.get("contributions_last_year") or {}).get("calendar_total")
    body.append(stat_cell(th, 32, 96, "当前连续 (天)", fmt(d.get("streak", {}).get("current"))))
    body.append(stat_cell(th, 172, 96, "最长连续 (天)", fmt(d.get("streak", {}).get("longest"))))
    body.append(stat_cell(th, 312, 96, "近一年总贡献", fmt(cal_total)))
    body.append(f'<line x1="28" y1="142" x2="{w - 28}" y2="142" stroke="{th["line"]}"/>')

    weekday = d.get("weekday") or [0] * 7
    mx = max(weekday) or 1
    labels = ["一", "二", "三", "四", "五", "六", "日"]
    bottom = 190
    for i, v in enumerate(weekday):
        x = 40 + i * 24
        bh = 6 + (v / mx) * 30
        body.append(f'<rect x="{x}" y="{bottom - bh}" width="14" height="{bh}" rx="3" fill="{th["accent"]}" opacity="0.85"/>')
        body.append(f'<text x="{x + 7}" y="{bottom + 14}" font-size="10" font-family="{FONT}" fill="{th["muted"]}" text-anchor="middle">{labels[i]}</text>')
    body.append(f'<text x="40" y="146" font-size="11" font-family="{FONT}" fill="{th["muted"]}">{esc("按星期分布")}</text>')
    body.append(f'<text x="{w - 30}" y="146" font-size="11" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">{esc("柱高 = 该星期贡献次数占比")}</text>')
    if seeded:
        body.append(show_seed_note(th, w - 30, h - 12, w))
    body.append("</svg>")
    return "".join(body)


def calendar_card(days, th: dict, theme: str, seeded: bool) -> str:
    """贡献日历热力图（GitHub 官方数据）—— 带星期/月份坐标，补齐贪吃蛇没有的坐标轴。"""
    w, h = 650, 154
    body = [card(w, h, th)]
    body.append(
        f'<text x="30" y="28" font-size="17" font-family="{FONT}" fill="{th["fg"]}" font-weight="700">{esc("贡献日历")}</text>'
        f'<text x="{w - 30}" y="28" font-size="11" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">{esc("近 365 天 · GitHub 官方 API")}</text>'
    )
    colors = CAL_LEVEL_COLORS[theme]
    # 图例：少 □□□□□ 多
    for i, c in enumerate(colors):
        x = w - 106 + i * 13
        body.append(f'<rect x="{x}" y="20" width="10" height="10" rx="2" fill="{c}" stroke="{th["line"]}"/>')
    body.append(f'<text x="{w - 112}" y="29" font-size="9" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">{esc("少")}</text>')
    body.append(f'<text x="{w - 38}" y="29" font-size="9" font-family="{FONT}" fill="{th["muted"]}">{esc("多")}</text>')

    if not days:
        msg = "暂无数据 — 运行一次 Update Profile Stats 后由官方贡献日历生成"
        body.append(
            f'<text x="{w / 2}" y="100" font-size="13" font-family="{FONT}" fill="{th["muted"]}" text-anchor="middle">{esc(msg)}</text>'
        )
        body.append("</svg>")
        return "".join(body)

    x0, y0, step, size = 36, 60, 11, 9
    first = date.fromisoformat(days[0][0][:10])
    sunday_start = first - timedelta(days=(first.weekday() + 1) % 7)  # 该列首日所属的周日

    # 星期坐标（GitHub 布局：第 0 行是周日）；只在 一/三/五 行写标签
    row_labels = {1: "一", 3: "三", 5: "五"}
    for r, lbl in row_labels.items():
        body.append(
            f'<text x="{x0 - 6}" y="{y0 + r * step + 7.5}" font-size="8.5" font-family="{FONT}" fill="{th["muted"]}" text-anchor="end">{esc(lbl)}</text>'
        )

    # 月份坐标：某月第 1 天出现在某一列时标注月份
    last_label_col = -10
    for ds, _ in days:
        d = date.fromisoformat(ds[:10])
        if d.day != 1:
            continue
        col = (d - sunday_start).days // 7
        if col - last_label_col < 2:
            continue
        last_label_col = col
        body.append(
            f'<text x="{x0 + col * step}" y="{y0 - 6}" font-size="8.5" font-family="{FONT}" fill="{th["muted"]}">{esc(d.month)}月</text>'
        )

    # 格子
    for ds, c in days:
        d = date.fromisoformat(ds[:10])
        col = (d - sunday_start).days // 7
        row = (d.weekday() + 1) % 7          # 周一=1 ... 周日=0（和 GitHub 一致）
        if c <= 0:
            lv = 0
        else:
            lv = 1 if c < 3 else 2 if c < 6 else 3 if c < 10 else 4
        body.append(
            f'<rect x="{x0 + col * step}" y="{y0 + row * step}" width="{size}" height="{size}" rx="2" fill="{colors[lv]}"/>'
        )
    body.append("</svg>")
    return "".join(body)


# ────────────────────────────── 主流程 ──────────────────────────────────

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="GitHub 官方 API → README 数据卡片")
    ap.add_argument("--user", default=os.environ.get("STATS_USER", "flairamos"))
    ap.add_argument("--token", default=None)
    ap.add_argument("--seed", default="scripts/seed_stats.json")
    ap.add_argument("--out", default="assets")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    token = args.token or os.environ.get("STATS_TOKEN") or os.environ.get("PAT_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    seeded = False
    d = None
    if args.seed:
        seed_path = Path(args.seed)
        use_seed_first = "--seed-first" in sys.argv
        if seed_path.exists() and (use_seed_first or not token):
            d, seeded = collect_seed(seed_path), True
    if d is None:
        try:
            d = collect(args.user, token)
        except Exception as e:  # 采集失败 → 回落种子，保证卡片永远可渲染
            print(f"⚠ 官方接口采集失败：{e}", file=sys.stderr)
            if Path(args.seed).exists():
                d, seeded = collect_seed(Path(args.seed)), True
            else:
                print("✗ 无种子数据可用，退出。", file=sys.stderr)
                return 1

    print(f"▸ 渲染卡片 → {out}/ (data_source={d.get('data_source', '?')[:60]}...)")
    cal_days = d.pop("_calendar_days", []) or []
    for theme in ("light", "dark"):
        th = THEMES[theme]
        (out / f"stats-{theme}.svg").write_text(stats_card(d, th, seeded), encoding="utf-8")
        (out / f"languages-{theme}.svg").write_text(languages_card(d, th, seeded), encoding="utf-8")
        (out / f"streak-{theme}.svg").write_text(streak_card(d, th, seeded), encoding="utf-8")
        (out / f"calendar-{theme}.svg").write_text(calendar_card(cal_days, th, theme, seeded), encoding="utf-8")

    local = Path(out) / "stats.json"
    local.write_text(
        json.dumps({**d, "seeded_placeholder": seeded}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cc = d.get("contributions_last_year") or {}
    print(
        f"✓ 完成：commits={cc.get('commits')} languages={len(d.get('languages') or [])} "
        f"streak={d.get('streak')} seeded={seeded}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
