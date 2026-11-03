# State 重构计划（交接文档）

> 这份文档是重构的**唯一上下文来源**。新会话从第 1 节顺序读，就能接上进度。
> **最后更新：2026-10-29 晚。阶段 1 已完成，测试基线已修复，阶段 2 尚未开始写代码。**

## 当前状态速查

| 项目 | 值 |
|---|---|
| 分支 | `dev`，HEAD `0ea02cf`（工作区只应有本文档未跟踪） |
| State 字段数 | **20**（阶段 2 完成，字段数未变） |
| 测试 | 59 个用例，`OK (skipped=4)`，约 5 秒，不出网 |
| 下一步 | **阶段 2.5 DAG / 粒度评审（Gate，不写代码）**，见 11.5 |
| 判断标准 | **失败集合不扩大**；当前失败集合为空 |

---

## 1. 背景与目标

`src/graph/state_models.py` 里的 `State` 是整张 LangGraph 论文工作流共享的状态表。重构前它声明了 33 个字段，问题集中在四类：

1. **字段只写不读**：节点写了，但全仓没有任何消费者；
2. **重复数据**：同一份论文/抽取结果在状态里存了 2~3 份；
3. **依赖注入走数据通道**：`SessionRepository`、`ProviderSnapshot`、`PaperSearchService` 等**对象**被塞进状态，导致状态无法序列化；
4. **缺少约束**：`TypedDict` 在运行时就是普通 dict，节点返回未声明字段不会报错。

目标：让状态变成**可序列化、可校验、职责清晰**的数据结构，为接入官方 Checkpointer 铺路。

### 长期目标与判断原则（用户明确，2026-10-30 记录）

**这不是一次单纯的性能/整洁重构。长期目标是把项目里「框架已有一等公民能力、却手搓了一套」的实现，逐步换成官方机制；借这个真实项目深入理解 LangGraph 的设计哲学，而不是只会调 API。**

判断原则（三条，缺一不可）：

1. **框架有一等公民等价物能力的地方，不手搓。**
   本项目里典型的是：持久化/恢复（Checkpointer）、依赖注入（`context_schema` + `Runtime`）、人在环暂停（`interrupt()`）、并发编排（`Send` / 子图）、状态合并（reducer）。
   手搓的版本往往「happy path 能跑、边界条件下有洞」——例如现有 `read_resume_checkpoint` 在正常续跑时没问题，但会出现「成功跑完仍残留 `{}`、下次误判从阅读开始」这类隐患。
2. **框架不管的地方，自己写是正常的，不算债。**
   例如：SSE 事件推送与前端协议、`SessionRepository` 的存储抽象、Agent 的提示词与工具编排、产物文件布局。LangGraph 不提供这些，自研不是"不规范"。
3. **引入某个框架能力必须有需求触发，不能"有就用"。**
   `Store`（跨会话记忆）、`Send`/子图（逐篇粒度）、reducer（并行合并）都明确暂缓，触发条件写在第 11 节。**为了"更官方"而引入用不上的抽象，是另一种过度工程。**

**随之而来的自我约束（学习目标的一部分）**：

- 每次采用/替换一个官方机制，在本文档记录「官方怎么设计的、我们原来的做法差在哪、边界在哪」；
- 不追求"框架功能全部用上"，追求"需要的能力都用官方原语表达"；
- 判断一个手搓实现是不是债，只问一句：**框架有没有一等公民等价物？有 → 是债；没有 → 不是。**

### 手搓实现清单（用于判断"该不该换"）

| 现有实现 | 官方等价物 | 判定 | 计划 |
|---|---|---|---|
| `read_resume_checkpoint` + `halt_with_checkpoint` | Checkpointer + `interrupt()` | **是债** | 阶段 4 / 5 替换 |
| 依赖（repo/通道/资源）塞进 state | `context_schema` + `Runtime` | **是债** | ✅ 2a 已替换 |
| dataclass 直接进 state | 纯 dict + serde | **是债** | 2b / 2c |
| 节点内 `asyncio.gather` 处理 N 篇论文 | `Send` / 子图 | **待评估** | 2.5 评审决定 |
| 手工累加 artifact refs、手工合并 diagnostics | reducer（`Annotated[...]`） | **部分是**（当前串行图还能用） | 阶段 5 引入并行分支时一起做 |
| 事件总线 + SSE 推送 | 无（框架不管 UI 事件） | 不是债 | 保留 |
| `SessionRepository` 存储抽象 | `Store` 是长期记忆，不是替代品 | 不是债 | 保留（见 11.2） |
| 自研 LLM provider 适配层 | LangChain integrations | 设计选择 | 保留（多协议/鉴权自定义） |

### 硬约束（已确认，不是假设）

后续**确定要接入 LangGraph 官方 Checkpointer**，现有手写恢复机制（`read_resume_checkpoint` + `halt_with_checkpoint`）会被 `interrupt()` 模式取代。

这条约束决定了三件事：

- 阶段 2（让 state 可序列化）是**前置条件**，不是可选的整洁工作；
- `read_resume_checkpoint` 相关代码属于**冻结区**，不投入整改；
- 字段门禁必须卡在**节点返回值边界**，不能卡在 `graph.ainvoke()` 的返回值上（否则将来会被 `__interrupt__` 等内部键误伤）。

### 为什么「依赖必须出 state」（结论，勿重复讨论）

接上 Checkpointer 后，State 不再是内存 dict，而是**磁盘快照**：每个节点执行完都会序列化写盘，重启后反序列化恢复。因此 state 的成员资格不是「谁需要共享」，而是：

1. 能不能被序列化成字节；
2. 恢复时应该**复用**它，还是按当前环境**重建**。

`session_repo` / `sync_port` / `Semaphore` 这两条都不满足（不可序列化 + 应该重建），所以必须迁出。

实测证据：

- `json.dumps(ReviewRequest)` / `json.dumps(PaperDocument)` / `json.dumps(WorkflowRuntimeContext)` 全部 `TypeError`；
- 普通对象进 state + `InMemorySaver` → `Type is not msgpack serializable: Obj`；
- dataclass 进 state 能过 checkpointer，但会打印 `Deserializing unregistered type ... will be blocked in a future version`（未来会拦），这也是 `request` / `search_results` 要转纯 dict 的原因。

---

## 2. 仓库与分支

| 项目 | 值 |
|---|---|
| 仓库路径 | `C:\Users\AdminZeng\Documents\coding_project\Paper-Agent` |
| 基线提交 | `af2fc63`（`main` 的最新提交） |
| 开发分支 | 本地 `dev` |
| 当前 HEAD | `c1803c1 feat(observability): 给每次 run 挂上追踪元数据` |
| 阶段 1 提交 | `80b7b18 refactor(state): 加字段门禁并删除未使用字段` |
| 基线修复提交 | `423284e test(graph): 修复 e2e 基线并把真实模型测试改回手动开启` |
| 阶段 2a 提交 | `7fdf201`（取消测试）、`590d953`（依赖迁出 state）、`c1803c1`（追踪元数据） |
| 阶段 2b 提交 | `3431165`（`request` / `search_results` 转纯 dict） |
| 阶段 2c 提交 | `0ea02cf`（最小增量返回 + 恢复现场清除语义 + 3 条验收测试） |
| Python | 3.12.13（uv 管理） |
| 依赖 | `uv.lock` 已锁定，`uv sync` 可复现 |

**注意：不要把这个本地 `dev` 推送到 `origin/dev`。** 上游的 `dev` 是另一代代码（用 `web/`，而本仓库用 `front/`，两者相差 215 个文件、3.6 万行），与 `main` 完全分叉。要推送请推到自己的 fork，并使用不同分支名（例如 `refactor/state-cleanup`）。

**本文档 `REFACTOR_PLAN.md` 不进版本库**（用户明确要求）。它在工作区里是未跟踪状态，属于本地交接资料。

---

## 3. 路线图

| 阶段 | 目标 | 为什么在这个位置 | 卡住 Checkpointer | 状态 |
|---|---|---|---|---|
| **1** | 立字段门禁 + 删死字段 | 后续每一步靠它证明"没写错键" | 否 | ✅ 已完成 |
| **1.5** | 修测试基线（hermetic e2e + 集成测试隔离开关） | 阶段 2 的验收网 | 否 | ✅ 已完成 |
| **2** | state 变纯数据 + 依赖走 context + 只返回变化键 | Checkpointer 序列化的硬前提 | **是** | **✅ 全部完成**（2a / 2b / 2c，见第 8 节） |
| **2.5（评审，不写代码）** | **DAG / 粒度评审**：read 是否拆子图、暂停点在哪、失败语义 | 它决定阶段 3 的 state 形状；接 Checkpointer 后再改图代价高 | 否 | **待做（阶段 3 开工前必须完成，见 11.5）** |
| **3** | state 瘦身：去重 + 收敛 artifact 字段 | 快照体积与写入量 | 否 | 待做 |
| **4** | 接入官方 Checkpointer（SQLite saver + `thread_id`） | 前两步完成后才有意义 | — | 待做 |
| **5** | 用 `interrupt()` 重写暂停/续跑，删掉手写恢复机制 | 需要 Checkpointer 才能做 | — | 待做 |
| **6**（可选） | 按阶段拆子状态 / 分图 | 接完 Checkpointer 后 schema 本就要调整 | 否 | 暂缓 |
| **7** | 可下载运行轨迹（本地 trace bundle + 一键下载） | 依赖阶段 4 的 state 历史与 LLM 埋点；面向非技术用户的排错入口 | 否 | 暂缓（阶段 5 之后，见 11.6） |

阶段 5 的真实难点（提前记下，见第 11 节）：现在逐篇论文的处理是**节点内的 `asyncio.gather`**，与 `interrupt()` 的语义不兼容，需要在阶段 5 前做一次 spike 决策（子图/Send vs 幂等重跑）。

---

## 4. 冻结区（现在不要动）

这些代码注定会在阶段 5 被删除或重写，现在花时间整理它们是浪费：

- `src/graph/checkpoint_halt.py`（整个文件）
- `src/graph/graph.py` 中的 `_entrypoint` / `_merge_read_checkpoint` / `_papers_from_checkpoint`
- `State` 中的 `read_resume_checkpoint`、`read_artifact_refs`
- `read_node.py` 中的 halt / resume 分支

**例外（被迫改动）**：阶段 2b 把 `request` 改成 dict 时，`_merge_read_checkpoint` 里构造 `ReviewRequest` 的那三行必须跟着改成构造 dict。**只改类型，不改逻辑**。

---

## 5. 阶段 1 完成记录

### 5.1 字段门禁

实现位置：`src/graph/graph.py` 的 `_check_declared_state_keys`，挂在 `_with_cancellation_boundary` 里，所有节点返回值都会过一遍；`_build_initial_state` 对 `state_overrides` 也做同样校验。

三条设计原则：

1. 卡在**节点返回边界**，不检查图输出（Checkpointer 兼容）；
2. 白名单从 `State.__annotations__` **派生**，不手写常量（schema 变化时不漂移）；
3. **只查多出来的键**，缺键合法（`total=False` 的语义）。

已知边界：只看节点**返回**的字典，看不见就地写 `state[...]` 的三处（`checkpoint_halt.py`、`read_node.py` 的 halt 分支）——它们都在冻结区。

### 5.2 删除的字段

| 字段 | 删除理由 |
|---|---|
| `search_output` | 检索节点已直接把它交给 sink，state 里这份没人读 |
| `assistant_message` | 消息走事件发给前端，字段无人读 |
| `assistant_message_metadata` | 同上 |
| `final_artifact_refs` | 拼文件与发事件都用局部变量 |
| `search_node_sink` | `_resolve_search_sink` 根本不看它，另有两份重复的 Protocol 定义 |
| `read_paper_statuses` | **从未声明**却写进了状态，且无人读 |

`State` 字段数：**33 → 28**。

保留项：`reply_node` 的 `assistant_metadata` 大字典仍在，因为它要作为 `message` 事件的 metadata 发给前端——只是不再塞回 state。这是阶段 3 要处理的"事件该带多少数据"的问题。

### 5.3 新增测试

`test/test_state_contract.py`，7 个用例：

- 合法更新通过 / 未声明字段报错 / 非字典忽略；
- **门禁确实挂在节点包装器上**（不是只存在于工具函数里）；
- 删掉的 6 个字段不允许被重新加回；
- 直接调用 `compose_reply` 节点，确认它只返回已声明键。

（原文这里写着"该节点在当前测试里跑不到"，现已过时：阶段 1.5 之后 4 条 e2e 会真的跑到 `compose_reply`。）

---

## 6. 测试基线（重要）

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test -v
```

**当前基线：54 个用例，`OK (skipped=4)`，约 4~5 秒，默认套件不出网、不依赖本地密钥。**

4 条 skip 是真实模型集成测试，只有显式开启才执行（本地需要可用的 `config/model.json`）：

| 用例 | 开启方式 |
|---|---|
| `test_live_model_config` ×2 | `$env:RUN_LIVE_MODEL_TEST="1"` |
| `test_model_connectivity` ×2 | `$env:RUN_MODEL_CONNECTIVITY_TEST="1"` |

```powershell
$env:RUN_LIVE_MODEL_TEST="1"; $env:RUN_MODEL_CONNECTIVITY_TEST="1"
.\.venv\Scripts\python.exe -m unittest test.test_live_model_config test.test_model_connectivity -v
```

注意：`skipped` 是「没测」，不是「通过」。这 4 条没跑，不等于本地模型配置没问题。

### 6.1 图测试的 hermetic 约束

`test/test_search_graph.py` 通过 `test/graph_test_support.py` 的 `hermetic_graph_llms()`
把分析节点换成替身，并把三个 `load_*_agent_llm` 钉成 `None`。这样这套 e2e 无论本地
有没有 `config/model.json` 都只测管道，不会真调模型（已验证：放一个 `model.json` 进去，
6 条仍在 2.6 秒内全绿）。

**判断标准是"失败集合不扩大"。当前失败集合为空，出现任何 failure/error 都视为本次改动引入的回归。**

覆盖边界（不要误读成"全链路已覆盖"）：

- 分析/大纲/写作节点在 e2e 里走的是**服务端兜底分支**，真实模型输出解析没有覆盖；
- 全文下载、Markdown 转换、向量入库没有覆盖（`ENABLE_FULL_TEXT_EMBEDDING=False`）；
- `compose_reply` 的 Markdown 拼装只被 e2e 间接覆盖。

### 6.2 测试分层（54 条的构成）

| 层 | 文件 | 数量 | 依赖 |
|---|---|---|---|
| 纯逻辑单测 | `test_extraction_validation`(12)、`test_paper_retrieval_service`(5)、`test_llm_adapters`(4)、`test_settings_router`(4)、`test_search_agent_tool_integration`(3)、`test_session_run_recovery`(4) | 32 | 假对象 / 临时 SQLite，不出网 |
| 图 e2e | `test_search_graph` | 6 | 假检索服务 + 假 LLM + 临时 SQLite |
| 字段契约 | `test_state_contract` | 7 | 无 |
| 前后端联调 | `test_front_to_back_gateway` | 5 | TestClient + 假 message handler（不跑真图） |
| 真实模型集成 | `test_live_model_config`(2)、`test_model_connectivity`(2) | 4 | 真密钥 + 网络，默认 skip |

辅助文件：`test/frontend_api_client.py`（模拟前端 HTTP 调用）、`test/graph_test_support.py`（图测试替身）。

### 6.3 历史记录（阶段 1 之后的测试修复，提交 `423284e`）

阶段 1 完成时的基线是「54 个用例，4 failures + 5 errors」：4 个缺 `config/model.json`；
4 个 `test_search_graph` 在执行到分析节点时抛 `OverallAnalysisError`；1 个 gateway 断言了
已被删除的 `websocket_stream`。已修复后两类：

- 图测试改用 `hermetic_graph_llms()` 固定模型环境，不再依赖 `state_overrides` 注入
  `analysis_node_llm`（该字段本来也从未被真正注入过）；
- 2 处 `limit` 断言从 `max_results` 旧语义改为候选池语义（5 → 25，`min(60, max(20, 5×max_results))`）；
- gateway 改断言 `sse_streaming`（实时推送一直是 SSE，`websocket_stream` 从未实现过，值是恒为 `False` 的占位字段）；
- 两条集成测试恢复 env 门禁，默认跳过。

---

## 7. 阶段 2 的核心决定（先读这一节再动手）

阶段 2 的目标：**State 里只剩可 JSON 序列化的业务数据**。

### 7.1 8 个字段分三类，处理方式不同

| 字段 | 谁写 | 性质 | 处理 |
|---|---|---|---|
| `runtime_context` | `graph.py:214`（每次 run） | 真运行时对象 | 字段消失，内容成为 graph context |
| `session_repo` | `graph.py:208`（每次 run） | 真业务依赖 | **迁进 context**（唯一新增字段） |
| `search_node_service` | 只有测试（`state_overrides`） | 测试后门 | 删；测试改 mock |
| `search_node_llm` | 只有测试 | 测试后门 | 删；测试改 mock |
| `read_node_llm` | 只有测试 | 测试后门 | 删；测试改 mock |
| `analysis_node_llm` | **无人写** | 死字段 | 删 |
| `writing_outline_node_llm` | **无人写** | 死字段 | 删 |
| `writing_node_llm` | **无人写** | 死字段 | 删 |

判别标准：**生产代码会不会写这个字段？** 不会写的，就是测试后门或死字段，不该迁移。

**不要**把 3 个测试后门原样搬进 context——那只是把同样的味道换个对象。测试改用
`mock.patch`（与 `hermetic_graph_llms` 一致）：

- `mock.patch("src.graph.search_node.PaperSearchService", lambda: stub)`
- `mock.patch("src.graph.search_node.load_search_agent_llm", lambda: fake_snapshot())`
- `mock.patch("src.graph.read_node.load_read_agent_llm", lambda *a, **k: snapshot)`

### 7.2 依赖注入的目标形态（已实测可行）

```python
# src/graph/runtime.py
@dataclass(slots=True)
class WorkflowRuntimeContext:
    session_key: str
    turn_id: str
    run_id: str | None = None
    workflow_name: str = "paper_graph"
    sync_port: WorkflowSyncPort | None = None
    cancellation: WorkflowCancellation | None = None
    resources: WorkflowRuntimeResources | None = None
    session_repo: SessionRepository | None = None      # 阶段 2a 唯一新增字段
```

```python
# src/graph/graph.py
workflow = StateGraph(State, context_schema=WorkflowRuntimeContext)
...
final_state = await graph.ainvoke(initial_state, context=runtime)
```

```python
# 每个节点（runtime 为必填参数：漏传会立刻 TypeError，而不是静默降级）
async def _node(state, runtime: Runtime[WorkflowRuntimeContext] | None):
    repo = runtime.context.session_repo      # 环境：从 context
    topic = state["request"]["topic"]        # 数据：从 state（2b 后 request 才是 dict）
    return {"analysis_report": report}       # 只写自己负责的键
```

实测确认（langgraph 1.2.6）：

- `context_schema` + `ainvoke(context=...)` 可用，**context 不会进 checkpoint**（放不可序列化对象 + `InMemorySaver` 两次 invoke 正常）；
- LangGraph 按**参数名 `runtime`** 注入，不要求注解（内部 `KWARGS_CONFIG_KEYS` 里 `runtime` 的类型约束是 ANY）；
- `context_schema` **不做运行时类型校验**（`Runtime(context=...)` 不 validate），它只是类型契约 + 给 Studio/Platform 生成 schema 的入口。别指望它当保险。
- 给 `runtime` 参数默认值 `None`，可以让 `test_state_contract` 那种直接调用节点的测试继续工作。

---

## 8. 阶段 2 执行方案（✅ 全部完成：2a / 2b / 2c）

拆成 3 个可独立验证的提交，顺序不能换。

### 8.1 2a：依赖出 state（改签名 + 换通道）—— ✅ 已完成（`590d953`）

**实际做法（与最初草稿的差异）**

1. `WorkflowRuntimeContext` 加 `session_repo`（只此一个）。
2. `State` 删 8 个字段，28 → 20。**没有新增任何字段。**
3. `StateGraph(State, context_schema=WorkflowRuntimeContext)`，`run_graph` 用 `ainvoke(..., config=..., context=runtime)`。
4. 6 个节点签名改 `(state, runtime)`（**必填**，无默认值）；`_resolve_reporter` / `_resolve_sink` / `_resolve_runtime_resources` 从 `runtime.context` 取。
   - `_resolve_llm` **被删除**：LLM 注入本就不进 context，节点直接调用 `load_*_agent_llm()`，测试用 `mock.patch` 替换。
   - 判空模式：`context = runtime.context if runtime is not None else None`。
5. `_with_cancellation_boundary` 的 `_guarded(state, runtime)`：取消检查改 `runtime.context.cancellation`，转发 runtime，并**保留了** `_guarded.__name__`。
6. `_build_initial_state` 不再塞依赖；`state_overrides` 只用于 `read_resume_checkpoint`。
7. 装配点补 `session_repo`：`session_runs.py` 主路径、`paper_workflow_runtime.py` 兜底路径。
8. 新增 `_ensure_runtime()`：把调用方传入的 `session_repo` 装进 context（兼容 `run_graph(session_repo=...)` 的旧调用方式）。
9. 新增 `_build_run_config()`：`config.metadata` 带 session_key / turn_id / run_id，`tags=["paper_graph"]`；不写 `configurable`。

**测试影响（已落地）**

- `test_search_graph`：新增 `_fake_backends()`（patch `PaperSearchService` / `load_search_agent_llm` / `load_read_agent_llm`），删除全部 `state_overrides` 依赖注入项；
- `test_state_contract`：包装器测试传 `(state, None)`；取消测试用 `Runtime(context=WorkflowRuntimeContext(...))`；直接调用 reply 节点处显式传 `None`。

**验收结果**：56 条全绿；实测一次完整 run 产出 11 个产物（search × 5、read × 2、analysis、outline、writing、final），最终 state 恰好 20 个已声明字段。

### 8.2 2b：数据纯化（`request` / `search_results` 转 dict）—— ✅ 已完成（`3431165`）

**实际改动**

- `request: ReviewRequest` → `request: JsonObject`；`graph.py` 新增 `_request_payload()` 在入口转换，`_merge_read_checkpoint`（冻结区例外）也构造 dict。
- `search_results: list[PaperDocument]` → `list[JsonObject]`：search 节点**持久化之后**再 `to_dict()`；read 节点入口用 `_papers_from_payload()` 还原，内部逻辑不动。
- 删除 `_papers_from_checkpoint` / `_optional_int`；`GraphRunResult.papers` 改 `list[dict]`。
- `reply_node` / `searchAgent` / `writingOutlineAgent` 的 4 处 `getattr(request, ...)` 全部改成 dict 取值；`read_node._build_resume_checkpoint` 的 3 处也一并改掉（否则 checkpoint 会丢 topic）。
- `writing_node` 不用改（已有 `isinstance(payload, PaperDocument)` 兼容分支）。

**结果**：56 条全绿；`json.dumps(final_state)` 通过（约 43KB）；完整 run 仍产出 11 个产物；`state["request"]` / `search_results[0]` 类型均为 `dict`。

**新增测试守卫**：

- `test_run_graph_returns_ranked_papers`：断言 `request` / `search_results[0]` 是 dict、`request["topic"]` 与 `search_summary["topic"]` 未丢失；
- `test_search_agent_uses_llm_to_generate_keywords`：断言检索提示词里真的包含用户主题（堵 `getattr` 静默坑）；
- `test_read_node_can_resume_from_checkpoint`：`search_results[0].id` 改为 `["id"]`。

**必须一起改的坑：`getattr` 在 dict 上会静默失败（最危险的回归点，已按此处理）**

`searchAgent.py:221/263/264` 和 `writingOutlineAgent.py:135` 用的是 `getattr(request, "topic", "")`。
迁移到 dict 后，**dict 没有属性，`getattr` 不报错，只返回默认值**：

```python
request = {"topic": "多智能体综述", "constraints": {...}}
getattr(request, "topic", "")   # → ""              ← 静默
request["topic"]                # → "多智能体综述"
request.topic                   # → AttributeError  ← 大声失败
```

对比：普通属性访问 `request.topic` 会立刻抛错（好事），而 `getattr(..., 默认值)` 会把"读不到"伪装成"本来就是空的"。
极端情况：如果键名与 dict 方法同名（如 `items`），`getattr(d, "items")` 返回的是**绑定方法**，同样不报错。

四处的影响：

| 位置 | 丢失的东西 | 后果 |
|---|---|---|
| `searchAgent.py:221`（`_build_llm_messages`） | 提示词里的 topic | 模型只看到"请拆分研究方向"，主题为空 → 关键词泛化（survey/review），检索跑偏 |
| `searchAgent.py:263`（`_build_search_intent`） | 整个 `constraints` | `year_from/year_to/sources/excluded_terms/max_results` 全部回落默认（不限年份、全源、60 篇）→ 用户约束静默失效，候选量暴涨十几倍 |
| `searchAgent.py:264` | `SearchIntent.topic` | 日志/产物元数据里的 topic 为空，排查时被误导成"用户没输入主题" |
| `writingOutlineAgent.py:135`（`_outline_messages`） | 大纲提示词里的用户主题 | 大纲仍能"成功"生成，但脱离主题 → 最终产出结构完整却跑题的综述 |

**最坏情况是一条全程无异常的降级链**：

```
topic 丢失 → 关键词泛化 → 候选论文与主题无关 → 语义过滤全部丢弃
→ 阅读结果只剩 0~1 篇 → 分析走"没有阅读结果"兜底 → 写作拿到空分析
→ 最终回复"未检索到符合条件的论文结果。"
```

而日志里只有一条"检索完成"。

**会被测试抓住的部分**（不要误以为全绿就安全）：

| 丢失项 | 现有测试能否抓到 |
|---|---|
| `constraints` | **能**：`sources == ["openalex","arxiv"]`、`limit == 25` 两条断言会红 |
| 检索提示词里的 topic | **不能**：stub 不检查 prompt 内容 |
| `SearchIntent.topic` | **不能**：无断言 |
| 大纲提示词里的 topic | **不能**：大纲走兜底也合法 |

**修法**：

```python
request = state.get("request") or {}
"topic": request.get("topic", ""),
constraints = dict(request.get("constraints") or {}),
topic = str(request.get("topic") or "").strip(),
```

**验收时必须补的守卫**（否则 2b 等于没测到）：

1. 全仓 grep `getattr(request` 必须清零；
2. e2e 断言 `result.state["request"]["topic"]` 非空，以及检索产物/意图里的 topic 非空；
3. 最好再加一条 Agent 层单测：构造 `_build_llm_messages(state)`，断言 messages 里包含主题字符串。

**测试影响**：`result.state["search_results"][0].id` → `["id"]`；`result.papers` 的断言只用了 `len()`，不受影响。

**验收**：全绿；`json.dumps(final_state)` 里 `request` / `search_results` 已是纯 dict（可以先手工验证，测试在 2c 一次补）。

### 8.3 2c：只返回变化的键 + 修删除语义 —— ✅ 已完成（`0ea02cf`）

**实际改动**

1. 6 个节点全部改为只返回自己改动的键；`reply_node` 只返回 `diagnostics` / `current_step`。
2. **修 `read_resume_checkpoint` 的清除语义**：
   - `_entrypoint` 改为真值判断（`{}` 视为"没有现场"）；
   - `read_node` 成功时写 `read_resume_checkpoint={}`，删掉原来的 `updated.pop(...)`（实测：**返回缺失的键 ≠ 删除**，channel 会保留旧值）；
   - `reply_node` 不再透传该字段。
3. `build_graph(checkpointer=...)` 增加可选参数（当前仅测试用，阶段 4 再接生产）。
4. **发现并修复一个真实的 Checkpointer 阻断点**：`WritingAgent.async_write_section` 把 `progress_callback` **函数**放进内层小节循环的 State。外层接上 saver 后，内层图会继承同一个 saver，直接抛 `Type is not msgpack serializable: function`。改为用 `ContextVar` 传递回调——不进 state、不进 checkpoint。
   - **教训**：这种"节点内部还有一个图"的结构，靠读代码很难发现，是"失败后恢复"测试把它逼出来的。

**为什么 2c 不是为了性能（纠正原计划的说法）**

实测：checkpoint 保存的是**全部 channel 的全量值**，节点返回最小增量还是整包，checkpoint 字节数完全一样（100KB 字段，2 个 checkpoint 共 200KB）。2c 的真实理由是：

- 删除语义（上面的第 2 条）；
- 避免用节点入口时的旧值覆盖别人写的 channel；
- 将来接 reducer 时，只有返回增量才是正确的。

体积问题属于阶段 3。

**新增 3 条验收测试（已落地）**

1. `test_final_state_contains_core_fields`：7 个核心字段必须在最终 state 里；
2. `test_final_state_is_json_serializable`：`json.dumps(final_state)` 成功；
3. `test_state_survives_failure_and_resumes_from_checkpoint`：分析节点第一次抛错 → `ainvoke(None, thread_id)` 从 checkpoint 继续跑完；断言不重跑检索、`search_manifest.json` 只写一次。

**验收结果**：59 条全绿；`json.dumps` 已固化为测试；State 保持 20 个字段。

### 8.4 阶段 2 实际结果（已达成）

| 指标 | 结果 |
|---|---|
| `State` 字段数 | 20（2a 迁移依赖；2b/2c 未增删字段） |
| `json.dumps(final_state)` | 成功，已固化为测试（单轮约 43KB） |
| 节点返回 | 6 个节点都只返回变化键 |
| 依赖注入 | 统一走 graph context（`context_schema` + `Runtime`） |
| 测试 | 59，`OK (skipped=4)` |
| 恢复能力 | `InMemorySaver` 下"分析失败 → 续跑完成"测试通过 |

### 8.5 阶段 2 明确不做

- 不接 Checkpointer（阶段 4），不装 `langgraph-checkpoint-sqlite`；
- 不动 `checkpoint_halt.py`、`read_node.py` 的 halt 分支（冻结区）；
- 不做字段瘦身/去重（`writing_sections` 与 `writing_report["sections"]` 重复、`search_results` 与 `read_results[].paper` 重复）→ 阶段 3；
- 不碰 `interrupt()` 和逐篇论文的恢复粒度 → 阶段 5；
- **不把 `session_key` / `turn_id` 迁出 state**（它们可序列化、不卡 Checkpointer，属阶段 3 的去重范畴）；
- 不做 LangSmith 埋点（见第 10 节：阶段 2 只做两件零风险准备）。

---

## 9. 核心概念约定（避免下次重新推导）

### 9.1 State / Context / Config 三分

| | State | Context | Config |
|---|---|---|---|
| 装什么 | 这次任务**做到哪了** | 这次任务**用什么工具做** | 部署/环境参数 |
| 例子 | `request`、`search_results`、`read_results`、`writing_sections` | `session_repo`、`sync_port`、`cancellation`、`semaphore` | 模型名、超时、并发数 |
| 变化频率 | 每个节点都在改 | 一次 invoke 定好，全程不变 | 用户改设置时 |
| 存哪 | 内存 → checkpoint | 只在内存 | `config/model.json`、`system.yaml` |
| 崩溃重启 | **复用** | **重建** | 重新加载 |

判据（两条都要满足才进 State）：

1. 能 `json.dumps`；
2. 重启后应该复用旧值，而不是按当前环境重建。

一句话：**State 是「做到了哪」，Context 是「用什么做」。**

还有第三类：**节点局部变量**（`agent`、`groups`、中间列表）——下一个节点不需要 → 既不进 state 也不进 context。

### 9.2 LangGraph 概念地图（本项目用没用）

| 类别 | 概念 | 本项目 |
|---|---|---|
| 图结构 | node / edge / 条件边 / `Command(goto)` / 子图 / 递归上限 | 用了前三个 |
| 状态与持久化 | State schema / channel / reducer / `Overwrite` / checkpointer / `thread_id` / `durability` | 只有 State schema |
| 运行时注入 | `context_schema` / `Runtime(context, store, stream_writer)` / RetryPolicy / 超时 | 阶段 2 开始用 context |
| 人机交互 | `interrupt()` / `Command(resume=...)` / 静态断点 | 手写机制，阶段 5 替换 |
| 并发编排 | `Send` / 子图 / map-reduce | 用 `asyncio.gather` 替代（阶段 5 要重新评估） |
| 流式 | `stream_mode` / `StreamWriter` | 没用（自建 SSE + 事件总线） |
| 平台层 | Studio / Platform / threads API / assistants | 本地部署用不到 |
| 生态包 | `create_agent` / `ToolNode` / supervisor / swarm | 没用（自己写 agent） |

真正与本项目设计哲学有落差的只有两条：**持久化（阶段 4）** 和 **并发编排（阶段 5）**。Store / reducer 不是落差，是没有需求。

### 9.3 实测性能数字（避免重复测量）

| state 大小 | 序列化 | SQLite 提交 | 6 节点合计 |
|---|---|---|---|
| 100 KB | 0.01 ms | 4.65 ms | ~28 ms |
| 500 KB | 0.02 ms | 4.05 ms | ~24 ms |
| 2 MB | 0.35 ms | 10.57 ms | ~66 ms |

结论：

- 序列化成本可忽略，SQLite 开销主要是每次 commit 的 fsync（4~10ms，与大小弱相关）；
- **阶段 2 没有性能收益**（依赖对象是"存不进去"，不是"慢"）；
- 性能/体积的收益在阶段 3（每节点一份全量 checkpoint、正文双份、事件 metadata 再存一份）。

### 9.4 类型迁移守则：`getattr` 是静默失败源

任何「对象 → dict」的迁移（2b、以及将来把内部 `PaperDocument` 统一成 dict 时）都必须先清理这几类写法：

| 写法 | 迁移后 | 危险度 |
|---|---|---|
| `x.topic` | `AttributeError` | 低（立刻炸，能发现） |
| `getattr(x, "topic", "")` | **返回 `""`** | **高（静默降级）** |
| `x.get("topic", "")` | 返回 `""`（预期行为） | 正常 |
| `isinstance(x, SomeClass)` | 恒为 `False` | 高（分支悄悄失效） |
| `hasattr(x, "topic")` | 恒为 `False` | 高（分支悄悄失效） |

规则：

1. 迁移前先 `grep` 出所有 `getattr(<数据对象>, ...)`、`isinstance(..., <数据类>)`、`hasattr(<数据对象>, ...)`，逐条处理；
2. 迁移后用「断言数据非空」的测试兜底（例如检索意图里的 topic 必须非空），而不是只断言"流程跑完"；
3. `isinstance` 分支尤其隐蔽：`_collect_paper_metadata` 里 `isinstance(payload, PaperDocument)` 在 dict 下恒为 False，会走 `elif isinstance(payload, dict)`——好在那种地方本来就兼容两种形态，但新写的代码不要依赖这种巧合。
4. **不要把函数 / 回调放进任何图的 State**，包括"节点里再开的图"。LangGraph 会通过 run config 把外层的 Checkpointer 传到内层图，内层 State 一样会被序列化；函数会直接抛 `Type is not msgpack serializable: function`，而报错点在 `ainvoke` 那一行，不指向真正的字段。
   - 正确做法：回调走 `ContextVar`（按 asyncio 任务隔离）或闭包；环境对象走 graph context。
   - 本项目实例：`WritingAgent.async_write_section` 曾把 `progress_callback` 放进 `SectionLoopState`，已改为 `ContextVar`（提交 `0ea02cf`，见 §8.3）。

---

## 10. 可观测性：LangSmith（长期计划，已确认要做）

**现状**：`langsmith 0.9.3` 已随 langgraph 安装；代码里目前**没有任何** tracing 钩子；LangGraph 节点在设了 `LANGSMITH_TRACING=true` 后会自动出现在 trace 里。

**盲区**：自研 provider（`src/llm/openai_compat.py` / `anthropic.py`）不走 LangChain，所以**模型调用不会被自动 trace**——prompt / response / token / 延迟都不在 trace 里。

**好消息（接缝位置）**：所有文本模型调用都汇聚在 `src/llm/base.py:426` 的
`LLMProvider._run_llm_call(operation, call_once)`，两个 provider 的 `chat` / `chat_stream`
都走它，重试也在里面。将来加埋点只需：

1. 给 `_run_llm_call` 加一个 `messages` 参数（共 6 个调用点）；
2. 在里面开一个 LLM span，输出里带 `usage_metadata`（项目已有 token 统计）。

**阶段 2 只做两件零风险准备**：

1. `run_graph` 组装 run 级 config 并传给 `ainvoke`：
   ```python
   config = {"metadata": {"session_key": ..., "turn_id": ..., "run_id": ...},
             "tags": ["paper_graph"]}
   ```
   今天不设 env 就完全不生效；价值是 LangSmith 里能按会话筛选，且阶段 4 的 `thread_id`
   也放同一个地方。**`thread_id` 语义未定，先别写 `configurable`。**
2. **保住** `_guarded.__name__ = f"{node_name}_with_cancellation"`（2a 改签名时最容易顺手删掉）。

**接入时要处理的事**：节点 run 的输入/输出默认是整个 state（含论文摘要、全文、`writing_report`），
会又大又涉及数据外发。用 `traceable(process_inputs=...)` / `hide_inputs / hide_outputs` 裁剪：
节点层只传 key + 计数 + topic，LLM 层传 prompt/response（必要时截断）。

**环境变量**（0.9.3 支持，已确认）：

```powershell
$env:LANGSMITH_TRACING="true"
$env:LANGSMITH_API_KEY="lsv2_..."
$env:LANGSMITH_PROJECT="paper-agent-dev"
# 可选：$env:LANGSMITH_TRACING_SAMPLING_RATE="0.2"；自建用 LANGSMITH_ENDPOINT
```

**两套观测不要合并**：SSE + SQLite 事件是产品/审计（给用户看、刷新回放）；LangSmith 是开发工具（看 prompt、比差异）。

**不要做**：不要给每个函数加 `@traceable`；不要在测试里开（外发 + 变慢）；不要把 key 写进仓库。

---

## 11. 长期演进决策（已讨论，记录结论）

### 11.1 PostgreSQL：现在不用，长期会需要

用户长期规划：最终部署到线上服务器、多用户使用。但**不是现在**。

**短期结论：阶段 4 用 `langgraph-checkpoint-sqlite` + 独立 `data/checkpoints.db`。**

理由：当前是单进程（`main.py` 里 `uvicorn.run(..., host=127.0.0.1, reload=True)`）、单用户、本机；
已有 SQLite（会话）+ 文件（产物）+ Chroma；一次 run 一个写者，SQLite WAL 足够。

**「提前上 Postgres」不等于「准备好线上多用户」**——多用户真正贵的是：

| 需要的能力 | 现状 | Postgres 能解决吗 |
|---|---|---|
| 认证 / 会话归属 | 无认证，`auth_required: False` | ❌ |
| 每用户模型密钥 | 全局 `config/model.json`（明文） | ❌ |
| 产物存储 | 本机磁盘 `data/sessions/...` | ❌ |
| 任务执行模型 | web 进程内 `asyncio` 任务 | ❌ |
| 多实例 SSE | 进程内 broker | ❌ |
| 并发写 / 多副本 | SQLite 单文件 | ✅ |

**现在只做 4 件便宜的准备**

1. checkpoint 用独立文件 + retention 清理策略（每 thread 保留最近 N 个 / TTL）；
2. `build_checkpointer(settings)` 工厂 + `checkpointer_backend` 配置项（节点永远看不到 saver，只在 `compile()` 出现）；
3. `thread_id = f"{session_key}:{turn_id}"`（保持"每轮独立"，天然带会话维度）；
4. **artifact refs 去掉绝对路径**：现在 `read_persistence.py:101`、`search_persistence.py:35` 都带 `"path": "C:\\..."`，会进 checkpoint，换机器/对象存储就失效。阶段 3 收敛 artifact 字段时只留 `artifact_id / artifact_type / name / size`，路径由仓储在读取时解析。

**触发上 Postgres 的条件**（任意一条）：多实例部署 / 多用户并发写 / 远程访问 / 高可用与运维要求 / 需要 SQL 查历史状态。

**将来迁移清单**（按成本排序，最贵的不是数据库）：

1. 产物从本地文件 → 对象存储（最贵，影响 `write_artifact`、下载接口、前端 URL）；
2. run 执行从 web 进程移出（独立 worker + 队列），`recover_interrupted_runs` 的对账逻辑要重写；
3. 每用户配置/密钥 + 认证鉴权（`session` 表已有 `user_id NOT NULL` + 索引，schema 预留了）；
4. `PostgresSessionRepository`（实现现有 ABC，接口不变）；
5. `checkpointer_backend=postgres` + `AsyncPostgresSaver`；
6. 多实例 SSE → broker（Redis pub/sub）；
7. Chroma → pgvector 或独立服务（目前 embedding 还是关的）。

**建议**：把「第一次给外部用户用」当成独立里程碑，届时一次性做多租户改造，不要现在零散预埋。

### 11.2 Store（跨会话记忆）：暂缓

现状：用 SQLite 自存会话与产物；`langgraph.store` 只装了 `InMemoryStore`（无 SQLite/Postgres store 包）。

Store 的定位是「Agent 的长期记忆」（namespace + KV + 语义检索），**不是**会话仓储的替代品。
引入它会多一条并行的事实来源，且和 Chroma 形成两套向量库。

**触发条件**：出现「跨会话记忆」需求，且需要**按语义检索**（记住用户偏好、已读论文、研究兴趣），而不是按 session_key 查。用户已确认有这个需求但优先级低。

### 11.3 子图 / Send：阶段 5 的决策点

现状问题：阅读节点内用 `asyncio.gather` 并发处理 N 篇论文——并行藏在节点里，LangGraph 只看到「一个节点」：

- 只在整批结束时有一个 checkpoint；
- 中断后节点从头重跑（`_restore_read_results` 只从 state 读，不从磁盘产物读）；
- `interrupt()` 在 gather 子任务里不可靠。

**结论**：阶段 5 开始前做一次 1~2 天 spike，在「子图/Send 做 per-paper checkpoint」与「保持整节点重跑 + 产物幂等」之间选一个。它会改状态形状，所以必须在阶段 3 定型前决定。

### 11.4 Reducer：与并行分支一起做

现在没用（串行图，一个 key 一个写者）。但已经有两处**手工 reducer**：artifact refs 的 `list(...) + [new]`、`diagnostics` 的读-改-写。一旦引入 Send/并行分支，就必须上 reducer，否则互相覆盖。

**结论**：不主动引入；等 Send/并行分支落地时一起设计。这也是 2c「只返回增量」的隐藏前提。

### 11.5 阶段 3 前的 DAG / 粒度评审（Gate，只出决策不写代码）

**为什么必须做**：DAG 的破坏性改动（改节点名、删节点、改边）在「还没有 checkpoint」时几乎免费，一旦有了旧存档就很贵。所以要在阶段 3 定型 state 形状之前，把会改变图结构的问题先定下来。

**为什么不能现在做**：2b/2c 正在改数据格式和返回方式，此时再动节点边界会让三个变化叠加，出问题无法归因。**排在 2c 之后、阶段 3 之前，半天到一天。**

**只回答三个问题**：

1. **粒度**：`read_node` 内部的逐篇并发（`asyncio.gather`）是否拆成子图 / `Send`？`writing` 是否按小节拆？
   - 现状：一次 run 只有 6 个 checkpoint，而阅读节点可能跑 15 分钟、处理 20 篇；崩溃后整节点重跑。
   - 备选（更便宜）：不拆节点，但让 `_restore_read_results` 能从磁盘 `note.json` 恢复，做到「节点重跑、每篇幂等」。
   - 优先级：`read` 高（动态 fan-out + 单篇耗时长）；`writing` 低（串行、节间有依赖，整段重跑可接受）。
   - 判断依据：崩溃时能接受重跑多少工作。
2. **暂停点**：`interrupt()` 落在哪？现在的 halt 是「存 checkpoint + 抛异常」，改成 `interrupt()` 后是「挂在节点内 + `Command(resume=...)` 继续」。确认是否需要新增条件边。
3. **失败语义**：单篇失败 / 整阶段失败 / 用户取消，分别应该重跑什么？要与 checkpoint 粒度对齐，避免「恢复后重复下载」或「恢复了但漏一篇」。

**产出**：1~2 页决策记录，写回本文档第 11 节；**不改代码**。它的结论直接决定阶段 3 怎么做，以及阶段 5 是否要 bump `GRAPH_VERSION`。

**评审前的临时纪律**：阶段 2~4 期间**冻结节点名与 State 字段名**，它们已经是持久化格式的一部分（见第 12 节第 8 条）。

**推迟拆分不会形成技术债的前提**（两条，缺一不可）：

1. 节点名 / state 字段名在上一阶段冻结，破坏性改动只发生在阶段 5 并 bump 版本；
2. 2.5 评审在阶段 3 之前完成，避免阶段 3 按「read 是一个大列表」定死形状后又要返工。

**已知约束（拆分 writing 时必须遵守）**：

- `writing` 目前是**两层结构**：外层 `run_writing` 节点 → 内层单节循环图（`SectionLoopState`）。评审要判断是保持"节点内子图"，还是把小节提升为外层图的节点 / `Send` 分支。
- 内层循环的进度回调已改用 `ContextVar` 传递（`0ea02cf`），**不在 State 里**。将来把它提升为正式子图时，应把回调迁到子图 context（`runtime.context`），**不要再放回 State**——否则子图 checkpoint 会再次遇到不可序列化函数（见 §9.4 第 4 条）。
- 这一条不影响阶段 3 的字段形状决策，但会影响阶段 5 的实现方式，评审时一并记录。

### 11.6 可下载运行轨迹（面向用户的排错包，阶段 7，暂缓）

**定位**：这是**产品功能**，不是开发观测平台。目标用户是"不懂技术、说不清问题"的使用者：出错时点一个按钮，下载一个 zip，交给开发者。离线、全量、可审计。

**为什么不是造轮子（结论）**：LangSmith 是开发者的观测平台——远端、需要 key、可采样、数据出本机；而这里需要的是「本机、始终开启、全量、用户可下载」的排错包。两者解决的是不同需求。按 §1 的判据：官方没有「给终端用户的可下载诊断包」这种一等公民能力，所以自研不是重复造轮子。

**与 LangSmith 的分工**：

| 维度 | LangSmith | 本地 trace bundle |
|---|---|---|
| 面向 | 开发者（你） | 终端用户 → 转交开发者 |
| 位置 | 远端 | 本机文件 |
| 前提 | 账号 + key + 联网 | 无 |
| 完整性 | 可采样、保留策略由平台定 | 全量、保留期自定 |
| 形态 | 网页 UI | zip / JSONL |

**明确不做**（否则就真成"再造 LangSmith"）：

- span 树 / 父子调用可视化 UI；
- 采样、检索、跨 run 聚合分析；
- 通用 `@traceable` 埋点框架（不在业务函数里撒装饰器）。

**要做的是三块数据 + 一个导出**：

1. **LLM 调用明细**：在唯一汇聚点 `src/llm/base.py` 的 `_run_llm_call` 记录 model / token / 耗时 / 重试次数 / prompt 与 response 预览（截断）。
2. **节点前后 state 差分**：在 `_with_cancellation_boundary` 记录「本节点改了哪些 key、大小变化」，不存全文。
3. **统一事件信封**：`id / parent_id / run_id / session_key / turn_id / type / node_key / stage / status / started_at / duration_ms / tokens / error / payload / trace_version`。
4. **导出**：`GET /api/sessions/{key}/traces/{turn_id}` → zip；前端加「下载运行日志」按钮。

**Bundle 结构**：

```
data/sessions/<key>/traces/<turn_id>/
├── manifest.json        # app/graph 版本、模型配置指纹（不含 key）、时间、计数、错误摘要
├── events.jsonl         # 事件流（按 seq 排序，含 node/stage/parent_id）
├── state_history.jsonl  # 阶段 4 后：每个 checkpoint 的 state 快照 / 差分
├── diagnostics.json
└── artifacts.json       # 产物索引（id/type/name/size，不含绝对路径）
```

**采集 vs 采样 vs 展示**（容易混，记在这里）：

| 概念 | 一句话 | 本项目 |
|---|---|---|
| 采集 | 决定记什么 | 做，做到字段完整 |
| 采样 | 按比例丢弃记录 | **不做**；用保留期代替（保留最近 N 轮 / 7 天，单条超限截断） |
| 埋点（`@traceable`） | 标记"这里生成一个 span" | 只在三个汇聚点用，不撒到业务函数 |
| 展示 | 渲染树 / UI / 搜索 | 不做；数据齐了以后可写脚本或后补页面 |

**采样为什么不做**：用户下载的恰恰是出问题那一轮，采样会把它随机丢掉；而本机量级很小（一轮几百到几千条事件，几 MB）。

**现在（阶段 2~5）只需要保持的习惯**（不是任务）：

1. 新事件一律带 `run_id` / `node_key` / `stage`；
2. payload 克制：不放全文、不放密钥，只放 id / 计数 / 摘要 / 错误结构；
3. 阶段 4 顺手把 `thread_id` / `checkpoint_id` 加进事件，便于与 checkpoint 对齐。

**触发条件**：阶段 4 完成（有 state 历史）+ LLM 埋点完成（见 §10）。在此之前不写导出代码、不引入采样。

---

## 12. 已知问题 / 待决策

1. **`run_graph` 的返回值在线上被丢弃**（`src/services/paper_workflow_runtime.py:49` 没有接收）。前端全部依赖事件推送，`GraphRunResult.state` 只有测试在读。产品决策问题，不在重构主线。
2. **测试只覆盖管道，不覆盖模型输出解析**（见 6.1）。建议阶段 2 之后补 Agent 层测试（固定模型响应 + 真解析代码），不要用 JSON fixture 硬凑。
3. **上游 `origin/dev` 与 `main` 分叉**，不要混用，见第 2 节。
4. **`thread_id` 粒度待定**（阶段 4 前必须定）：建议 `session_key:turn_id`。
5. **`durability` 待定**（阶段 4）：默认 `async` 可能丢最后一个 checkpoint，要"崩溃后续跑"应设 `sync`。
6. **checkpoint retention 策略待定**（阶段 4）：不清理会随「节点数 × run 数」线性增长。
7. **SSE 澄清**：实时推送只有 SSE 一条（`sessions.py:103-121` + 前端 `EventSource`），没有 WebSocket；`POST /messages` 是遗留同步入口，只有 `test/frontend_api_client.py` 在用。
8. **节点名与 State 字段名是持久化契约**（阶段 4 之后生效）：checkpoint 的待办/版本记录按节点名索引，改类名/删节点会让旧存档失效或静默跳步。阶段 2~4 冻结这两类名字；破坏性改动放在阶段 5，并 bump `GRAPH_VERSION`、换 `thread_id`，让旧存档显式作废而不是误用。
9. **恢复前必须校验图版本**（阶段 4）：版本不匹配时拒绝续跑并提示用户重跑，不能静默恢复。

---

## 13. 环境备忘

- `uv run` 在某些受限终端里会因为无法写 `%LOCALAPPDATA%\uv\cache` 而失败；绕过方式：
  `$env:UV_CACHE_DIR = Join-Path $env:TEMP "uv-cache-paperagent"`，或直接调用 `.\.venv\Scripts\python.exe`。
- 测试用 `.\.venv\Scripts\python.exe -m unittest discover -s test`（不要依赖 `uv run`）。
- `test/` 没有 `__init__.py`，`python -m unittest test.test_xxx` 能跑（命名空间包）；`test/graph_test_support.py` 通过 `try: from .xxx / except ImportError: from xxx` 两种方式导入。
- `.venv/`、`data/*`、`logs/*`、`doc/*`、`model.json` 都在 `.gitignore` 里。
- PowerShell 输出中文可能乱码（控制台编码），看测试结果以 `Ran / OK / FAILED` 为准。
- 仓库有 `core.autocrlf`，新文件提交时会出现 LF→CRLF 警告，属正常。

---

## 14. 下次会话从这里开始

**下一步是 2.5 DAG / 粒度评审（Gate，不写代码）**——阶段 2 已全部完成。按顺序做，每步跑一次全量测试
（`.\.venv\Scripts\python.exe -m unittest discover -s test`），判断标准是
**`OK (skipped=4)`，失败集合为空**。

1. ✅ 取消测试（`7fdf201`）
2. ✅ **2a**：context 加 `session_repo`；State 删 8 个字段；节点签名 `(state, runtime)`（必填）；包装器转发 runtime；测试改 mock 注入；删 `reply_node` 的透传（`590d953`）
3. ✅ run 级 config metadata（`c1803c1`）
4. ✅ **2b**：`request` / `search_results` 转 dict；清理 4 处 `getattr`；`_papers_from_checkpoint` 删除；`GraphRunResult.papers` 改类型（`3431165`）。
   - 遗留观察：`search_results[0]` 的 dict 化已由测试守护；内部 `PaperDocument` 仍保留，按计划留到阶段 5 重写 `read_node` 时成片统一（见 §9.4）。
5. ✅ **2c**：6 个节点最小增量返回；`_entrypoint` 真值判断；read 写 `read_resume_checkpoint={}`；补 3 条验收测试（字段完整性 / `json.dumps` / 失败后恢复）（`0ea02cf`）。
   - **顺带修掉一个真实阻断点**：`WritingAgent` 把 `progress_callback` 函数放进内层小节循环的 State，接 saver 后会抛 `Type is not msgpack serializable: function`；已改为 `ContextVar` 传递。
6. **2.5 DAG / 粒度评审（Gate，不写代码）**：阶段 3 开工前必须完成，回答「read / writing 是否拆子图」「暂停点在哪」「失败语义」三个问题，结论写回第 11.5 节。**没做这个评审，不要开始阶段 3。**（当前唯一待办）
7. **3**：state 瘦身（去重 + artifact 去绝对路径）。
8. **4**：接 SQLite Checkpointer（工厂 / `thread_id` / `durability` / retention / 版本校验）；顺手把 `thread_id`、`checkpoint_id` 加进事件（为阶段 7 铺路）。
9. **5**：按 2.5 结论处理 read / writing 的粒度，用 `interrupt()` 重写暂停恢复，bump `GRAPH_VERSION`。
10. **7**：可下载运行轨迹（见 11.6），前置是阶段 4 + LLM 埋点；不采样、不做展示 UI。
11. **收尾**：更新本文档（阶段打勾、记录实际 diff 与遗留问题）。

**不要在本轮做**：接 Checkpointer、装 sqlite/postgres saver、字段瘦身、`interrupt()`、LangSmith 埋点、轨迹导出、任何 Postgres 相关代码。
