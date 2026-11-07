# Profile 使用说明 / 维护指南

本仓库是你的 GitHub 个人主页（`flairamos/flairamos`）。这一版把**所有统计类卡片换成了
「本仓库脚本 + GitHub 官方 API」的自助方案**，不再依赖 github-readme-stats、streak-stats、
trophy 等 vercel.app 系第三方实例（它们要么被墙、要么公共实例限流返回错误）。

---

## 1. 它是怎么工作的

```
GitHub 官方接口                     本仓库                     README
─────────────────          ─────────────────────         ──────────────
REST  /users/{login}  ───┐
GraphQL contributions ───┼─→  scripts/generate_stats.py ──→  assets/*.svg（相对路径引用）
GraphQL repositories  ───┘          │
                                    └→ assets/stats.json（原始指标明细）
        ▲
GitHub Actions
.github/workflows/update-stats.yml   每天 UTC 01:05（北京 09:05）自动运行 + 可手动触发
```

生成内容：

| 文件 | 内容 | 替代的第三方服务 |
| --- | --- | --- |
| `assets/stats-{light,dark}.svg` | commits/PR/Issue/Review/Stars/Forks/仓库数/Followers、最热仓库、GitHub 龄 | github-readme-stats |
| `assets/languages-{light,dark}.svg` | 语言占比（按代码字节数，GraphQL `languages`） | github-readme-stats top-langs |
| `assets/streak-{light,dark}.svg` | 当前/最长连续贡献 + 星期分布 + 年度总量 | streak-stats.demolab.com |
| `assets/stats.json` | 本轮分析全部原始指标，方便自己再加工 | — |
| `assets/banner-header.svg` / `banner-footer.svg` | 顶部/底部波浪横幅（静态） | capsule-render |
| `assets/tagline.svg` | 打字动画的静态备用版 | readme-typing-svg |

数据口径：**公开数据用 `GITHUB_TOKEN`（Actions 自带）即可全部覆盖**；私有仓库的提交/代码字节
默认不计入（见第 4 节）。

### 官方接口一览（脚本内实际用到）

- `GET https://api.github.com/users/{login}` ——followers、公开仓库数、注册时间
- GraphQL `user(login:).contributionsCollection` ——贡献日历：近一年 commits / PR / Issue /
  Review、连续天数、星期分布
- GraphQL `user(login:).repositories`（分页，`isFork:false, ownerAffiliations:OWNER`）——
  语言字节数、Stars、Forks、最热仓库

---

## 2. 你需要做的启用步骤（共 3 步）

1. **推送本仓库的所有新文件**（README.md、scripts/、.github/、assets/、docs/）。

2. **去 GitHub 网页 → 仓库页 → Actions 标签页**：
   - 如果 Actions 提示 "Workflows aren't being run"，点 **I understand my workflows, go ahead and enable them**；
   - 选择 **Update Profile Stats** → **Run workflow** → 手动跑一次。
     跑完后 `assets/` 里的卡片会从种子占位数据变成官方接口的真实数据（几秒钟）。

3. **（可选）生成贪吃蛇**：Actions → **Generate Contribution Snake** → Run workflow 跑一次，
   然后 `README.md` 里取消"贪吃蛇"注释区块。之前教程常用的 output 分支方案不需要了 ——
   本工作流直接把 SVG 提交进主分支 `assets/`，不存在 raw.githubusercontent 访问问题。

之后**每天早上 09:05 自动更新**，你不需要再维护数据。

---

## 3. 本地跑一跑（顺序无所谓）

```bash
# 自动读环境变量 GITHUB_TOKEN / PAT_TOKEN / STATS_TOKEN，读不到就回落种子数据
python3 scripts/generate_stats.py

# 显式指定 PAT（classic，勾选 read:user 就够；要统计私有仓库再加 repo）
set STATS_TOKEN=ghp_xxxx        # Windows cmd
python3 scripts/generate_stats.py

# 强制用种子数据渲染（不联网，调试样式用）
python3 scripts/generate_stats.py --seed scripts/seed_stats.json

# 换用户 / 换输出目录
python3 scripts/generate_stats.py --user flairamos --out assets
```

本地没有 token 也没关系：脚本失败时自动退回 `scripts/seed_stats.json` 种子占位渲染，
生成的卡片右下角会标注"种子占位"，Actions 一跑就被真实数据覆盖。

---

## 4. 想统计私有仓库怎么办

Actions 自带的 `GITHUB_TOKEN` 是"仓库级"token，默认只能读到公开数据。想统计私有仓库：

1. 生成一个 **fine-grained PAT**（Settings → Developer settings），授权你本人账号，
   权限只开 **Metadata: read**（想算私有仓库字节还要 Repository access 里勾私有仓库）；
2. 仓库 Settings → Secrets and variables → Actions → New repository secret，名字 `STATS_TOKEN`；
3. 把 `update-stats.yml` 里 `env` 那两行改成：

```yaml
        env:
          STATS_TOKEN: ${{ secrets.STATS_TOKEN }}
          STATS_USER: flairamos
```

不配置也一样用，只是统计范围限定为公开仓库 —— 卡片上会如实显示公开数据。

---

## 5. 自定义/扩展分析指标

指标计算都集中在 `scripts/generate_stats.py` 的 `collect()` 里，想加指标：

1. 在 GraphQL query 里加字段（比如 `latestRelease`）或在 REST 返回里挑字段；
2. 在返回的 dict 里加一层（如 `d["releases_total"] = ...`）；
3. 在 `stats_card()` 里 `cells` 列表加一行，重新渲染即可 —— 本地跑一次命令就能看效果。
卡片布局代码在同文件的 `stats_card / languages_card / streak_card`，都是手写 SVG，无任何第三方依赖（标准库 urllib/json/html 而已）。

---

## 6. 需要你手动改的占位内容

README 里还有 3 处占位符等你替换：

| 位置 | 现状 | 修改 |
| --- | --- | --- |
| `Find Me` → Email | `you@example.com` 徽章 | 换成你的真实邮箱 |
| `Find Me` → 掘金 | `juejin.cn/user/your-user-id` | 换成你的掘金主页 id |
| `Find Me` → X | `x.com/flairamos` | 不想放就整段删掉 |

Featured Projects 的简介是我按各仓库描述写的，你可以随时调整一行文案。

---

## 7. 常见问题

- **Q: 为什么 README 里图片有的看不了？**
  外面的打字动画（demolab.com）、shields.io、visitor-badge、ghchart 都是老牌国内可达服务；
  如果你也想连这些彻底去掉：打字动画 → `assets/tagline.svg`，贡献图 → 贪吃蛇工作流就能代替。
- **Q: 想改卡片配色？** 改 `generate_stats.py` 顶部 `THEMES`，`accent` 就是主题色（现在是 Go 青 #00ADD8）。
- **Q: 卡片是图片还是 SVG？** 全部是 SVG，文字矢量、体积小、可被搜索引擎索引。
