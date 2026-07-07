# Custom Agent 平台 — 设计文档 (PR 8)

> 让用户定义自己的 Agent，无需改主代码。

## 1. 目标

降低"加新 Agent"的成本从"改源码 + 重启服务"到"写一个 YAML / Python 配置"。

适合场景：
- 业务方希望快速验证一个 Agent idea
- 团队内不同部门需要专属 Agent（如 "policy-checker"、"compliance-auditor"）
- 不希望改 `agents/*.py` 主代码（避免 review 摩擦 + 部署耦合）

不适合场景：
- Agent 需要自定义 LLM 调用逻辑（仍要写 Python）
- Agent 需要新工具（仍要先在 `tools/` 注册）
- Agent 是核心架构改进（应走 PR review）

## 2. 架构

```
agents/
└── custom/
    ├── __init__.py          # 导出 CustomAgent, CustomAgentConfig, ...
    ├── base.py              # CustomAgent (继承 SmartAgent) + Config + Safety
    ├── registry.py          # CustomAgentRegistry (持久化) + get_custom_agent_registry()
    └── loader.py            # YAML 加载器 (PR F5, deferred — 测试目前不需要)
```

依赖：
- `agent_system.core.agent.SmartAgent` — 继承所有基础设施（4 路 resolver, checkpoint, event bus）
- `agent_system.core.registry.agent_registry` — Custom Agent 自动注册到全局（让 `_discover_peers` 能找到）
- `agent_system.config.settings.get_settings` — 工具配置

## 3. 数据模型

### `CustomAgentConfig` (Pydantic BaseModel)

```python
class CustomAgentSafety(str, Enum):
    STRICT = "strict"           # 输入校验严格，所有工具调用需人审
    NORMAL = "normal"           # 默认：与 built-in agent 相同
    AUTONOMOUS = "autonomous"   # 无人工干预，自主决策

class CustomAgentConfig(BaseModel):
    id: str                          # "code-reviewer"
    name: str                        # "Code Reviewer" (人类可读)
    description: str                 # 一句话说明
    system_prompt: str               # 给 LLM 的 system prompt
    tools: List[str] = []            # 此 agent 可用的工具子集（必须 ⊆ 全局 enabled）
    safety: CustomAgentSafety = NORMAL
    llm_config: Dict[str, Any] = {}  # 覆盖 LLM Router 的配置（model, temperature...）
    tenant_id: Optional[str] = None  # 多租户隔离
    model_config = ConfigDict(extra="allow")  # 允许自定义字段
```

### `CustomAgent` (继承 SmartAgent)

```python
class CustomAgent(SmartAgent):
    def __init__(self, config: CustomAgentConfig):
        # 1. 解析 tools (filter against global enabled)
        # 2. 设置 agent_name = f"custom_{config.id}"
        # 3. 设置 agent_capabilities = [config.description]
        # 4. 注册到 agent_registry (PR 5 基础设施)
```

关键方法：
- `agent_spec` 属性 → 返回 CustomAgentConfig
- `get_system_prompt()` → base_prompt + safety 注记 + tools 列表
- `execute(task)` → 走 SmartAgent.execute 全部基础设施（重试/事件/4 路 resolver）

## 4. Registry

### `CustomAgentRegistry`

```python
class CustomAgentRegistry:
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        self._configs: Dict[str, CustomAgentConfig] = {}  # (tenant_id, id) -> config
    
    def register(self, config: CustomAgentConfig) -> None:
        """Add or replace a config. Persists to disk."""
    
    def get(self, agent_id: str, tenant_id: str) -> Optional[CustomAgentConfig]:
        """Load by (tenant_id, id). Returns None if not found."""
    
    def list(self, tenant_id: str) -> List[CustomAgentConfig]:
        """List all configs for a tenant."""
    
    def delete(self, agent_id: str, tenant_id: str) -> bool:
        """Remove + persist. Returns True if existed."""
    
    def instantiate(self, agent_id: str, tenant_id: str) -> Optional[CustomAgent]:
        """Get config + create CustomAgent runtime instance."""
```

### 单例

```python
_custom_registry: Optional[CustomAgentRegistry] = None

def get_custom_agent_registry() -> CustomAgentRegistry:
    global _custom_registry
    if _custom_registry is None:
        from agent_system.config.settings import get_settings
        # 默认 storage path 走 settings 或 fallback
        _custom_registry = CustomAgentRegistry(storage_path="./data/custom_agents")
    return _custom_registry
```

## 5. 持久化

每个 config 存为 `<storage_path>/<tenant_id>/<id>.json`：

```json
{
  "id": "code-reviewer",
  "name": "Code Reviewer",
  "description": "Reviews code for style",
  "system_prompt": "...",
  "tools": ["read_file", "code_search"],
  "safety": "normal",
  "llm_config": {},
  "tenant_id": "acme"
}
```

启动时扫描目录加载所有 configs 到内存 cache。

## 6. 与现有基础设施的整合

| 设施 | 整合方式 |
|---|---|
| `agent_registry` (PR 5) | CustomAgent 在 `__init__` 时自动注册，让 `_discover_peers` 能找到 |
| `SmartAgent.execute()` (PR 2) | 继承所有：retry, checkpoint, event, 4 路 resolver |
| `MetricsCalculator` (PR 1) | 自动纳入 9 个核心指标（failure_rate_by_agent 等）|
| `AccessControl` | tenant_id 字段接入 `Resource` 模型 |
| `AuditLogger` | 自动记录 register/delete 操作 |

## 7. 安全考量

- **工具过滤**：`tools` 列表必须 ⊆ 全局 `settings.tools.enabled`（交集为空则 agent 没有任何工具）
- **system_prompt sanitization**：复用 `core/security.sanitizer.validate()`（注入防御）
- **配置 schema 校验**：Pydantic 严格类型 + extra="allow" 但保留基础字段必填
- **持久化路径**：不允许 `..`（防 path traversal）

## 8. 不做什么 (PR 8 范围)

- ❌ YAML 加载器（deferred 到 PR F5，测试不需要）
- ❌ API endpoint (`POST /api/custom-agents`)（deferred）
- ❌ Web UI（deferred）
- ❌ Custom tool 动态注册（仍要改主代码）

## 9. 验收标准

- [ ] `tests/test_custom_agent.py` 全过（11 个测试）
- [ ] `pytest tests/test_custom_agent.py` 覆盖：
  - Config 基本字段 + safety 默认值 + extra 字段
  - CustomAgent execute 返回正确 type + metadata
  - safety 注记出现在 system_prompt
  - tool 过滤（全局 enabled ∩ config.tools）
  - Registry: register / get / list / delete / tenant isolation
  - 持久化：构造新 registry 能读出旧 config
  - instantiate: 返回 agent 实例 + agent_spec.id 正确
  - 单例：get_custom_agent_registry 返回同一对象

## 10. 实现路径

| 步骤 | 文件 | 工作量 |
|---|---|---|
| docs/CUSTOM_AGENT.md | NEW | 本文档 (done) |
| base.py | NEW | 半天 |
| registry.py | NEW | 半天 |
| __init__.py | NEW | 10 行 |
| tests | 跑 + 补漏 | 半天 |
| **总计** | | **1.5 天** |

PR 8 完成后再考虑：
- F5 (loader.py) + examples/custom_agents/*.yaml
- API endpoint (REST 创建/查询)
- Web UI（培训场景示例）