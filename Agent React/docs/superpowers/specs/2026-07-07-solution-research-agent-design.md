# 解决方案咨询 Agent — 设计文档

- **日期**: 2026-07-07
- **状态**: 待用户最终确认
- **作者**: 编排者（brainstorming skill）

---

## 1. 目标与边界

构建一个**客户直接使用**的解决方案咨询 Agent（Demo 范围，本地运行）。
客户输入项目需求，Agent 自动完成：需求澄清 → 调研 → 总结 → 初版方案 → 反馈迭代 → 终稿报告。

**业务领域：通用**。本项目与所在工作区的医保热线业务**无关**。

### 1.1 决策日志（已确认）

| 维度 | 决策 |
|---|---|
| 服务对象 | 客户（C 端对话） |
| 业务领域 | 通用，与医保无关 |
| 数据源 | 联网搜索 + 内部产品/方案 KB + 本会上传资料 |
| 交互形态 | 单聊天框自由对话，无显式阶段 UI |
| 方案修订 | 纯自然语言反馈，agent 语义定位 |
| 交付形态 | 本地原型 / Demo |
| 技术栈 | Python + OpenAI 兼容 API（DeepSeek / Qwen / 硅基流动等可换） |
| 架构 | 状态机 + 阶段内 ReAct 循环 |

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│  UI 层 (Streamlit / Gradio，单聊天框)                        │
│   - 消息流 + 上传组件 + 进度条 (current stage)                 │
└──────────────────┬──────────────────────────────────────────┘
                   │  HTTP/SSE (token 流)
┌──────────────────▼──────────────────────────────────────────┐
│  Orchestrator (Python 状态机)                                  │
│   - Session 管理 (创建/恢复/列表)                               │
│   - Stage 路由 (5+1 个 stage + 路由判断)                       │
│   - 消息分发 (用户消息 → 路由 → 对应 stage handler)              │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐      ┌──────────────┐
│ Stage Handler│ ×5   │   Tools      │
│  - prompt    │      │  - web_search│
│  - guardrail │      │  - kb_search │
│  - tool 调用 │      │  - parse_doc │
│  - 产物落盘  │      │  - save_section│
└──────┬───────┘      │  - revise_section│
       │              │  - export_report│
       ▼              └──────┬───────┘
┌──────────────┐             │
│ LLM (兼容    │◄────────────┘
│  OpenAI API) │
└──────────────┘
       │
       ▼
┌──────────────────────────────────┐
│  Storage (本地文件系统)             │
│   sessions/<id>/                  │
│     ├─ meta.json (阶段+产物)      │
│     ├─ messages.jsonl (对话历史)   │
│     ├─ uploads/ (客户上传资料)    │
│     └─ artifacts/ (调研/方案/终稿)│
│   kb/ (内部产品 KB，FAISS/Chroma) │
└──────────────────────────────────┘
```

---

## 3. 阶段（Stages）

共 **5 个正式 stage + 1 个 finalize 出口**，统称"5+1"。`iterate` 不是独立 stage，而是 `design` 的**子状态**（设计上更自然：方案被持续修改，直到客户确认才出 final）。

| Stage | 输入 | 工具 | 产物 | 说明 |
|---|---|---|---|---|
| `clarify` | 客户首条需求 | 追问 LLM | `requirements.json` | 至少问出：行业、痛点、现状、约束、期望产出。**最多 5 轮**追问 |
| `research` | 结构化需求 | `web_search`, `kb_search` | `research.md` | ReAct 循环。**强制覆盖**：KB ≥ 1 条 + 联网 ≥ 3 条来源 |
| `summarize` | 调研要点 | — | `summary.md` | 单次 LLM 调用，200-400 字小结，**禁止新增内容** |
| `design` | 调研小结 + 需求 | `kb_search`, `revise_section` | `design_vN.md` | ReAct：找同类方案模板 → 填充。**每章节带 `<!-- anchor:slug -->`**。`iterate` 是子状态：收到客户反馈 → 解析定位章节 → `revise_section` 局部修改，直到客户确认 |
| `finalize` | 客户确认 | `export_report` | `final.md`, `final.pdf` | 导出 PDF + Markdown |

### 3.1 阶段路由

```python
async def route(user_msg: str, current_stage: str, session: Session) -> RouteDecision:
    """返回: stay | go_to_<stage_name>"""
```

- LLM + few-shot 判断（prompt 含当前阶段、最近 3 条消息）
- 置信度 < 0.7 → **默认 stay**
- 高频场景走规则快路径：
  - "重新调研" → 强制 `go_to_research`
  - "出报告" / "可以了" → 强制 `go_to_finalize`
  - "回到方案" → 强制 `go_to_design`
  - 客户在 `design` 阶段提"换 / 改 / 调整"类反馈 → 规则识别为 `iterate` 子状态，**不切 stage**

### 3.2 客户插话处理

- 任意 stage 收到新消息，先调 `route()` 决定去留
- `stay` → 当前 stage handler 决定如何吸收该消息（追加上下文 / 触发新动作）
- `go_to_X` → 切到目标 stage，原 stage 产物保留
- 路由误判防护：低置信度时 **显式向客户确认**（"您是想开始新的需求澄清吗？Y/N"）
- `design` 阶段收到修订类反馈时，handler 内部直接走 `iterate` 子状态分支（局部 `revise_section`），不重新生成全文

---

## 4. 组件

### 4.1 Stage Handler 统一接口

```python
class StageHandler(Protocol):
    name: str
    system_prompt: str
    required_tools: list[str]
    guardrail: Callable[[Session], bool]  # 返回 True 才允许切下一阶段

    async def run(self, session: Session, user_msg: str | None) -> StageResult: ...
```

### 4.2 工具集（6 个）

| 工具 | 输入 | 输出 | 实现 |
|---|---|---|---|
| `web_search` | query | `[{title, url, snippet}]` | Tavily / SerpAPI / 自建 |
| `kb_search` | query, top_k | `[{chunk, score, source}]` | Chroma / FAISS 检索 |
| `parse_doc` | file_path | 文本内容 | PyMuPDF / python-docx / openpyxl |
| `save_section` | stage, section_id, content | ack | 写 `artifacts/<stage>/<section>.md` |
| `revise_section` | stage, section_id, new_content | diff | 读旧 → diff → 写新 |
| `export_report` | session_id, format | 文件路径 | markdown → HTML → weasyprint PDF |

### 4.3 存储布局

```
sessions/
  <session_id>/
    meta.json          # {created_at, customer, current_stage, status}
    messages.jsonl     # 每行 {role, content, tool_calls, ts}
    artifacts/
      requirements.json
      research.md
      summary.md
      design_v1.md
      design_v2.md
      ...
      final.md
      final.pdf
    uploads/           # 客户上传文件原样保留

kb/
  index.chroma         # 向量库
  sources/             # 原始文档
    产品手册.pdf
    方案模板.md
    FAQ.md
```

### 4.4 知识库管理

- 离线工具：`scripts/ingest_kb.py` 把 `kb/sources/` 文档 chunk → embed → 入 Chroma
- 启动时懒加载 KB 到内存
- Demo 阶段 KB 小（10-30 个文档）即可

---

## 5. 数据流（典型对话 trace）

**场景**：客户咨询"100 人内部知识库，预算 30 万，3 个月内上线" → 反馈"第三部分部署太复杂换 SaaS" → "可以了出报告"。

```
[1] UI 收首条消息
    → POST /sessions (current_stage=clarify)

[2] clarify.run() — 4 轮问答后输出 requirements.json
    → current_stage → research

[3] research.run(requirements) — ReAct 循环
    LLM: web_search("...")
    Tool: 8 条结果
    LLM: kb_search("...")
    Tool: 2 条
    LLM: 评估达标 (KB≥1, web≥3) → 退出
    → research.md 落盘
    → current_stage → summarize

[4] summarize.run() — 单次 LLM
    → summary.md 落盘
    → current_stage → design

[5] design.run() — ReAct
    LLM: kb_search("方案模板")
    套模板填充, 输出 5 章节带 anchor 的 markdown
    → design_v1.md 落盘

[6] 客户: "第三部分部署太复杂换 SaaS"
    → router: stay, 命中"换/改"快路径 → design handler 走 iterate 子状态
    → LLM 解析: "第三部分" → anchor:deploy, "换 SaaS" → 修订指令
    → revise_section(section=deploy, ...)
    → design_v2.md 落盘, 客户 UI 看 diff 高亮

[7] 客户: "可以了出报告"
    → router: go_to_finalize
    → finalize.run() → export_report()
    → final.md, final.pdf 落盘
    → status=completed
```

**时序**：阶段切换 ~3-8 s，research 最重 5-15 s，全流程 5-15 min（含客户思考）。

---

## 6. 错误处理

| 类别 | 示例 | 处理 |
|---|---|---|
| 工具失败 | web 超时 / KB 0 结果 | 重试 2 次 → 降级（KB 0 用 LLM 自身知识 + 标注"无外部依据"） |
| LLM 输出不合规 | 方案缺章节 / 调研只 1 条 | 重生成 1 次 → agent 自我诊断重试 → 最多 3 轮 |
| 路由误判 | summarize 时客户说"方案不对" 错判为新需求 | 显式确认 Y/N |
| 客户输入极端 | 1 字 / 1GB 文件 | 长度校验 / 文件限 10MB，清晰提示 |
| 网络挂 | web 全不可用 | 继续，所有结论标"⚠️ 联网不可用" |
| session 损坏 | meta.json 非法 | 启动 schema 校验，备份恢复或新建 |

### 6.1 安全/合规

- **敏感信息脱敏**：上传文件中手机号/身份证/银行卡正则识别，**不送 LLM**，本地打码
- **幻觉抑制**：事实论断必须带来源链接或 KB 引用；无来源部分浅色标注
- **拒绝域**：prompt 硬编码"不答与本次调研无关的问题"
- **成本保护**：单 session ≤ 50 次 LLM 调用 / ≤ 100k token，超限自动 finalize

### 6.2 客户可见反馈

- 顶部进度条：`需求澄清 ▰▰▰▱▱ → 调研 → 总结 → 方案 → 修订 → 终稿`
- 每阶段完成后聊天框内显示产物摘要
- 修订用 markdown 渲染 diff（绿/红）
- 错误透明（"我在等 X / 正在尝试 Y"）

---

## 7. 测试与验收

### 7.1 验收标准（Demo "完成"）

| 项 | 标准 |
|---|---|
| 能跑 | `python -m agent.main` → 浏览器 8501 端口能聊 |
| 5+1 阶段全过 | 至少 1 个真实场景从 clarify 走到 finalize 不挂 |
| KB 检索 | 5-10 个产品文档入 `kb/sources/` 后 kb_search 返回合理 |
| 工具降级 | 拔网线跑 research 不崩，标注"联网不可用" |
| 续传 | design 阶段关浏览器重开能继续 |
| 修订定位 | "第三部分换 SaaS" 正确锚到 `anchor:deploy` |
| 报告导出 | final.pdf 5 章节 + 来源引用齐全 |

### 7.2 测试策略

不写传统单元测试（LLM 行为难测），用 **golden case**：

```
tests/
  golden/
    case_01_知识库咨询/
      input.txt
      expected_stages.txt
      expected_artifacts/
    case_02_多轮反馈/
      ...
```

跑法：`python tests/run_golden.py`，比对产物关键字。

**人工 review**：每个 prompt 调优后人工跑 2-3 个真实 case，看方案完整性、来源质量、修订 diff。

### 7.3 演示脚本（交付物）

README 附 3 分钟演示：开浏览器 → 首条需求 → 4 轮 clarify → research 自动跑 → summary + design → "第三部分换 SaaS" 看 v2 diff → "可以了" 下载 PDF。

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 路由判断不稳，频繁误切 | 规则快路径 + 低置信度 stay + 显式确认 |
| 中英混检 KB 召回差 | 检索时双语 query 扩展 + 重新打分 |
| 方案章节定位漂移 | anchor 用可读 slug (`arch`/`deploy`)，解析失败回退全文检索 |
| Demo 时长 5-15 min 不便演示 | 加 `--fast` 模式（少 ReAct 轮 + 短 prompt） |

---

## 9. 范围外（Out of Scope）

- 多租户 / 计费 / 权限体系
- 高可用 / 容灾 / 水平扩展
- 多语言（Demo 阶段只支持中文）
- 移动端原生 App
- 与外部 CRM / 工单系统对接
- 模型微调 / 自训练
- A/B 实验框架

---

## 10. 资源/成本估算（Demo）

| 项目 | 估算 |
|---|---|
| 单 session LLM token | 30k–80k |
| 单 session 工具调用 | 8–20 次 |
| 单 session 端到端 | 5–15 min |
| Demo KB 大小 | 10–30 文档，~5MB |
| 单 session 存储 | 1–5MB |
