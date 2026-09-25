# 「慢数据」情报 Agent —— 完整工作流程手册

> 面向大模型数据团队：每天自动感知 Benchmark / 竞品模型 / 人才 / 数据供应商动态，
> 产出带证据链的中文情报日报，并把全部线索沉淀为可检索、可追溯的"慢数据"资产。
> 架构设计详见 [`docs/01-architecture.md`](docs/01-architecture.md)。

---

## 目录

1. [30 秒了解这个系统](#1-30-秒了解这个系统)
2. [架构与模型分层](#2-架构与模型分层)
3. [安装（首次一次性）](#3-安装首次一次性)
4. [一次完整运行的内部流程](#4-一次完整运行的内部流程)
5. [输出产物详解](#5-输出产物详解)
6. [日常操作手册](#6-日常操作手册)
7. [配置参考](#7-配置参考)
8. [质量保障机制](#8-质量保障机制)
9. [试验期验证指南（接下来几天怎么测）](#9-试验期验证指南接下来几天怎么测)
10. [常见问题 FAQ](#10-常见问题-faq)
11. [路线图](#11-路线图)

---

## 1. 30 秒了解这个系统

**解决的问题**：数据团队过去被动等算法提需求才找数据（滞后 2~3 周）。本系统把情报工作改成**主动积累**：每天自动全网扫描 → 结构化沉淀 → 算法提需时直接在储备库检索，命中即有。

**每天的产出**：
1. 一份中文 Markdown 日报（`reports/YYYY-MM-DD.md`）——情报正文 + 数据方针 + 证据核验 + Critic 评分；
2. 增量写入 SQLite 沉淀库（`data/slowdata.db`）——条目、断言核验、实体、事件时间线。

**成本**：单次完整运行 LLM 约 **$0.13**、Tavily 检索约 **40 credits**（免费档 1000/月 ≈ 25 次）、耗时 5~8 分钟、全程本地无云部署。

---

## 2. 架构与模型分层

```
LangGraph 日度流水线（13 个节点，Critic 带条件回路）
generate_queries → collect → triage → fetch_content → dedup → summarize → persist
     → verify → events → synthesize → policy → critic ──未达标──▶ research ─┐
                                            └──────通过/达上限──▶ render_report ◀─┘
```

**模型分层**（你要求的核心设计，所有模型走 DeepSeek 官方 API）：

| 层级 | 模型 | 承担节点 | 依据 |
|------|------|----------|------|
| L0 无 LLM | 确定性代码 + 本地 Qwen3-Embedding-0.6B-Q | 收集、时间窗过滤、网页抓取、语义去重、聚类、持久化、报告渲染 | 零成本 |
| L1 轻量 | `deepseek-chat`（JSON 模式） | 初筛分类、日期线索提取、簇摘要、实体抽取、断言拆解、断言判定、事件提取 | 大吞吐、模式固定 |
| L2 推理 | `deepseek-reasoner` | 热点探测、矛盾裁决、趋势综合、数据方针、Critic 评审 | 需要多步推理，只用 5~6 个节点 |

> 经验法则：判断/抽取/筛选 → L1；综合/裁决/评审 → L2；能不用 LLM 就不用。
> 日预算 $0.5，超 60% 后综合/方针节点自动 L2→L1 降级（报告会标注）。

**信源体系**：

| 信源 | 方式 | 说明 |
|------|------|------|
| Tavily | 搜索 API（days=7 时间窗） | 全网时效信息（含 `site:` 定向抓 LinkedIn/公众号公开内容） |
| OpenAlex | 论文 API | 覆盖含 arXiv 的全学科论文（arXiv 官方 API 拦截 Python 客户端，故用 OpenAlex） |
| GitHub | Search API | 评测/数据集相关新仓库 |
| HuggingFace | daily_papers + trending | 每日论文与趋势数据集 |
| RSS | feedparser | 量子位 / HackerNews / TechCrunch（清单在 `config/sources.yaml`） |
| LinkedIn 人工通道 | CSV 导入 | `slowdata import-csv`（合规红线：不爬私有接口） |

---

## 3. 安装（首次一次性）

```bash
cd /Users/xty116/Documents/Deepseek_Project/数据agent

# 1) 密钥已写入 .env（DEEPSEEK_API_KEY / TAVILY_API_KEY），无需重复配置
# 2) 依赖与嵌入模型均已装好（.venv / .fastembed-cache），无需重复安装
# 3) 直接验证：
./run.sh stats          # 应能看到历史运行记录
```

> 换机器重装时：`cp .env.example .env` 填 key → 按 `docs/01-architecture.md` §9 或本 README 末尾的安装备注执行 uv 安装 → 首次运行会自动下载嵌入模型（约 1.1GB）。

---

## 4. 一次完整运行的内部流程

执行命令：`./run.sh run`（约 5~8 分钟）。内部按序经过 13 个节点：

| # | 节点 | 模型 | 做什么 |
|---|------|------|--------|
| 1 | generate_queries | L0+L2 | Watchlist 词表展开（零成本）+ reasoner 生成 ≤4 条"今日热点探测"query（失败自动跳过） |
| 2 | collect | L0 | 5 个信源并行拉取；**时间窗第一层**：Tavily 传 days=7，OpenAlex/GitHub 按日期过滤，带日期的超窗条目直接丢弃 |
| 3 | triage | L1 | 相关性过滤 + 四维度打标（benchmark/competitor/talent/supplier）+ 重要度 1~5 + **日期线索提取**（时间窗第二层：从标题/正文提取日期，超窗丢弃、窗内补全） |
| 4 | fetch_content | L0 | 重要度≥3（榜单类 URL 优先，上限 25 条）抓网页正文：正文文本+表格转文本+内嵌 JSON；内容过少自动走 r.jina.ai 渲染代理；**时间窗第三层**：页面日期复核（超窗丢弃、窗内补全） |
| 5 | dedup | L0 | 本地嵌入语义聚类（余弦≥0.92）+ **跨 7 天比对**：与存量条目向量比对，命中则标"复现"而非"新" |
| 6 | summarize | L1 | 簇代表摘要（2~3 句）+ 类型化实体抽取（company/model/benchmark/dataset/person/supplier）；榜单类优先，配额 16 簇 |
| 7 | persist | L0 | 条目 upsert 入库（同 URL 再出现时合并更丰富信息，不会越存越旧） |
| 8 | verify | L1+L2 | 重要度≥4 簇：拆 2~3 条原子断言 → 每条 Tavily 定向搜证 → 判 ✅证实/❌冲突/❓未证实 → 冲突时 L2 矛盾裁决（采信版本+置信度） |
| 9 | events | L1 | 实体入库并链接条目；从摘要提取归一化事件（发布/榜单动作/招聘/采买/供应商动作/融资）入事件表 |
| 10 | synthesize | L2 | 趋势综合成四维度情报；**核验纪律**：✅直接引用、❓标注未证实、❌必须转述裁决、禁止传播被否结论 |
| 11 | policy | L2 | 数据方针：可执行动作 + P0/P1/P2 优先级 + 依据编号；Watchlist 增删建议 |
| 12 | critic | L2 | 五维打分（事实性/完整性/引用可靠性/**时效性**/可操作性，各 0~10）；任一 <7 → 生成补充 query 回搜（≤2 轮，带预算上限）→ 仍不达标 → 降级接受并标注 ⚠️ |
| 13 | render_report | L0 | 渲染日报 + 写入运行统计 |

**Critic 回路示意**：critic 未达标 → research（定向回搜→初筛）→ dedup → summarize → persist → verify → events → synthesize → policy → critic（再次评审）。

---

## 5. 输出产物详解

### 5.1 日报（`reports/YYYY-MM-DD.md`）逐段说明

| 段落 | 内容 | 怎么看 |
|------|------|--------|
| 头部 | 采集规模（query/条目/簇/新事件数） | 快速判断今日信息量 |
| 情报正文 | 今日头条 Top5 + Benchmark/竞品/人才/供应商四维度 + 风险与机会 | 每条结论带 `(依据 #N)`；推断标 `(推断)`；未证实标"未证实" |
| 数据方针 | 建议表格（方向/对应 benchmark/优先级/动作/依据）+ Watchlist 建议 + 风险提示 | **直接可执行**：P0 本周、P1 本月、P2 观察 |
| 证据核验与矛盾裁决 | 每条断言 ✅❌❓ + 证据链接 + ⚖️裁决（置信度） | 点开链接复核原文；被 ❌ 的结论不应再出现在正文 |
| 实体事件 | 今日提取的事件表（主体/类型/事件） | 慢数据时间线的最小单元 |
| Critic 评审 | 五维分数 + 每轮问题清单 + 降级接受标注 | 低分维度 = 今天报告最该人工复核的地方 |
| 附录 | 全部线索表（维度/重要度/状态/核验/信源/日期） | "🆕新"vs"复现"验证跨日去重；"⏱️未知"是时间不可考的条目 |
| 运行统计 | 每节点 LLM 调用数/成本 + 运行告警 | 成本透明、路由策略可审计 |

### 5.2 沉淀库（`data/slowdata.db`，6 张表）

| 表 | 内容 | 查看命令 |
|----|------|----------|
| `items` | 全部条目（含嵌入向量、页面正文） | `./run.sh db items --limit 20` |
| `claims` | 断言级核验记录（verdict + 证据） | `./run.sh db claims` |
| `entities` | 实体字典（公司/模型/benchmark/数据集/人物） | `./run.sh db entities` |
| `events` | 归一化事件时间线 | `./run.sh db events` |
| `item_entities` | 条目↔实体链接 | （关联表，供看板使用） |
| `runs` | 每次运行审计（状态/成本/token） | `./run.sh stats` |

---

## 6. 日常操作手册

```bash
cd /Users/xty116/Documents/Deepseek_Project/数据agent

# —— 运行 ——
./run.sh run                      # 完整日度流水线（5~8 分钟）
./run.sh run --max-queries 6      # 省钱试验：缩小检索规模
./run.sh run --date 2026-09-26    # 指定报告日期（一般用不到）

# —— 检查 ——
./run.sh stats                    # 历史运行：状态/成本
./run.sh db items --limit 20      # 条目
./run.sh db claims                # 断言核验记录
./run.sh db entities              # 实体
./run.sh db events                # 事件时间线

# —— 人工线索导入（LinkedIn 等合规通道）——
./run.sh import-csv config/linkedin_import.example.csv
# CSV 列：title,url,content,published_date,source,dimensions（source 默认 linkedin_manual）

# —— 定时运行（macOS launchd，可选，M3 前替代方案）——
# 每天 08:30 自动跑一次：
#  crontab -e 添加：
#  30 8 * * * cd /Users/xty116/Documents/Deepseek_Project/数据agent && ./run.sh run >> data/cron.log 2>&1
```

**改关注清单立即生效**：编辑 `config/watchlist.yaml` 的 queries（如加入 `"LiveCodeBench leaderboard top"`、竞品名、供应商名），下次运行生效，无需重启任何服务。

---

## 7. 配置参考

### `config/config.yaml`（关键项）

| 配置 | 默认 | 含义 |
|------|------|------|
| `search.max_queries` | 12 | 单次 Tavily 检索 query 上限 |
| `sources.enabled` | 5 个全开 | 信源开关（tavily/papers/github/huggingface/rss） |
| `sources.time_window_days` | 7 | **时间窗硬约束**：只收提需时间前 N 天资料 |
| `sources.undated_policy` | keep_flagged | 无日期条目：保留并标记（可改 drop 全丢） |
| `sources.fetch_pages` / `fetch_max` | true / 25 | 网页正文抓取开关与上限 |
| `sources.render_proxy` | r.jina.ai | JS 渲染兜底代理（置空禁用） |
| `dedup.cosine_threshold` | 0.92 | 语义去重阈值（调低=更容易合并） |
| `triage.max_clusters_summarized` | 16 | LLM 摘要簇数上限（成本旋钮） |
| `verify.min_importance` / `max_clusters` | 4 / 6 | 核验触发线与上限（成本旋钮） |
| `critic.min_score` / `max_rounds` | 7 / 2 | Critic 达标线与回搜轮数上限 |
| `llm.daily_budget_usd` | 0.5 | 日预算，超 60% 后 L2 节点自动降级 L1 |

### 其他文件

| 文件 | 作用 |
|------|------|
| `config/watchlist.yaml` | 关注主题与检索词（日常最常改） |
| `config/sources.yaml` | RSS 订阅源清单 |
| `config/prompts/*.md` | 9 个版本化 Prompt（triage/summarize/queries/synthesize/policy/verify_claims/verify_verdict/resolve/critic/events） |
| `config/linkedin_import.example.csv` | LinkedIn 人工线索导入模板 |

---

## 8. 质量保障机制

**设计目标不是"每句话都对"，而是：错误可被发现（检出）→ 可被追溯（溯源）→ 可被纠正（闭环）。**

已实现的防线（对应节点）：

| 防线 | 机制 | 节点 |
|------|------|------|
| 信源分级 | 一手（论文/GitHub/HF 官方 API）> 媒体 > 论坛；弱信源被 Critic 点名 | collect / critic |
| 时间窗四层 | 检索参数 → 入库过滤 → 初筛日期线索 → 抓页复核；仍无日期标 ⏱️ 且不得作为唯一依据 | collect/triage/fetch |
| Claim 级核验 | 断言级 ✅❌❓ 判定 + 证据链接，非整条"看起来对就放行" | verify |
| 矛盾裁决 | 冲突时 L2 权衡权威/时效/精确度，采信版本带置信度 | verify |
| Critic 回路 | 五维打分 + 有界回搜（≤2 轮）+ 降级接受标注 | critic |
| 事实/推断分离 | 推断强制标 `(推断)`，综合层禁止传播被否结论 | synthesize |
| 全链路溯源 | 每条结论 → 依据编号 → 附录 → URL + 信源 + 日期 | render_report |

**已知边界（诚实声明）**：① 时效性维度长期低分是常态——全网大量信源不暴露日期，系统把问题摆在明面上而非隐藏；② 榜单口径冲突（如"SWE-bench 榜首是谁"）依赖各榜单站自身数据，系统提供证据链接由人工裁决；③ 每轮核验预算有限（6 簇），重要度 3 的条目不核验。

---

## 9. 试验期验证指南（接下来几天怎么测）

**第 1 天（今天）**：对照 §5.1 逐段读日报
- [ ] 每条结论都有 `(依据 #N)` 且编号可对上附录
- [ ] 点开 3~5 条证据链接，确认原文真实、日期在近一周内
- [ ] 证据核验区判定合理（✅ 的证据确实支撑断言）
- [ ] 数据方针的 P0 建议是否可执行（对象/动作/优先级/依据齐全）

**第 2 天（明天）**：再跑一次，验证"慢数据"积累
- [ ] 附录里昨天见过的条目标"复现"、新条目标"🆕新"（跨日去重生效）
- [ ] `./run.sh db entities` / `db events` 行数在增长
- [ ] `./run.sh stats` 成本在 $0.10~0.15 区间

**第 3~5 天**：连续观察
- [ ] 记录每天 Critic 五维分数，看趋势（尤其引用可靠性/时效性）
- [ ] 记录误报（无关内容混入）与漏报（重要事件没抓到）——把漏报的查询词加进 watchlist 再跑
- [ ] 检查每天成本/耗时稳定，无异常飙升
- [ ] 若有团队真实关注的事件，验证系统是否抓到且证据正确

**试验期记录 badcase 的方式**：直接记在备忘录/评论里，M4 的周复盘 Agent 会消费这些记录迭代 Prompt 与路由（也可以现在发给我，我手动调）。

---

## 10. 常见问题 FAQ

**Q：为什么 Critic"时效性"总是低分？**
A：大量信源（知乎、LinkedIn 帖、部分榜单站）不暴露发布日期，只能标"⏱️时间未知"。Critic 如实扣分并列出具体条目——这是把数据质量摆到明面上，报告里的 ⚠️ 提示就是提醒人工复核。提高分数的方法：调高 `verify` 预算让更多条目走抓页补日期，或收紧 watchlist 到日期规范的源。

**Q：同一天重复跑，报告会被覆盖吗？**
A：会，`reports/2026-09-25.md` 同名重写。同 URL 条目在库里不会重复（upsert 合并），报告会显示"复现"。想看历史日报请跑完另存或等 M3 看板（自动按日期归档）。

**Q：为什么"Muse Spark 1.1 是 SWE-bench 第一"没出现在报告里？**
A：系统抓到的 benchlm.ai 榜单（9/22 更新）显示榜首是 Claude Opus 5（96%），与你的说法矛盾。这是典型的榜单口径差异，系统现在的处理方式是：给出带链接的证据 + 未证实标记，由人裁决。M4 的矛盾裁决增强会让这类对比更自动化。

**Q：某信源报错（如 RSS 302）怎么办？**
A：单个信源失败只告警不中断（`[collect] xxx 失败`）。机器之心 RSS 因 302 跳转暂不可用，量子位/HN/TechCrunch 正常。修法：在 `config/sources.yaml` 换新 feed 地址。

**Q：如何省 Tavily 额度？**
A：`./run.sh run --max-queries 6`；或把 `verify.max_clusters` 调低、`search.search_depth` 改 basic。核验检索已默认用 basic（1 credit/次）。

**Q：能换别的 LLM 供应商吗？**
A：`config/config.yaml` 的 `llm.base_url/tier_l1/tier_l2` 改指向 OpenAI 兼容端点即可（如 SiliconFlow），分层逻辑不变。

---

## 11. 路线图

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | LLM 分层 + 存储 + Tavily + 单条流水线 + 日报 | ✅ 完成 |
| M2 | 多信源 + 网页抓取 + 时间窗 + Claim 核验 + 矛盾裁决 + Critic 回路 + 实体事件 | ✅ 完成 |
| M3 | Web 看板（日报阅读/实体时间线/成本看板/手动触发）+ 定时调度 | 待你试验确认后开工 |
| M4 | 深潜模式（on-demand 调研问答）+ 周复盘半自动迭代 + Watchlist 演化 | 待开工 |

---

## 附：换机器重装备忘

```bash
git clone <repo> && cd <repo>
cp .env.example .env          # 填入 DEEPSEEK_API_KEY / TAVILY_API_KEY
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PYTHON_INSTALL_DIR="$PWD/.uv-python" \
       HF_HOME="$PWD/.hf-cache" FASTEMBED_CACHE_PATH="$PWD/.fastembed-cache"
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
./run.sh run                  # 首次运行自动下载嵌入模型（约 1.1GB）
```
