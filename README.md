# AMB2API

AMB2API 是一个 AssemblyAI LLM Gateway 的 OpenAI 兼容代理服务，提供统一的 API 接口来访问多个大语言模型（包括 GPT、Claude、Gemini 等）。

## 项目概述

AMB2API 作为中间代理层，将 OpenAI 格式的 API 请求转换并路由到 AssemblyAI LLM Gateway，支持多种主流大语言模型的统一访问。项目提供了完整的 Web 管理面板，支持配置管理、使用统计、日志监控等功能。

### 核心特性

- **OpenAI 兼容接口**：完全兼容 OpenAI API 格式，无缝对接现有应用
- **多模型支持**：通过 AssemblyAI Gateway 访问 GPT、Claude、Gemini 等多个模型
- **灵活的存储后端**：支持 Redis、PostgreSQL、MongoDB 和本地文件存储
- **Web 管理面板**：直观的配置管理、使用统计和日志监控界面
- **使用统计**：详细的 API 调用统计和配额管理
- **速率限制追踪**：实时监控每个 API Key 的速率限制状态和配额使用情况
- **智能 Key 轮换**：基于速率限制自动切换 API Key，优化请求成功率
- **假流式响应**：支持假流式模式，提供更好的用户体验
- **重试机制**：自动处理 429/400 速率限制错误和其他临时故障
- **Docker 支持**：提供完整的 Docker 部署方案

## 架构设计

### 系统架构

```
┌─────────────────┐
│   客户端应用     │
│ (OpenAI格式)    │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│         AMB2API 代理服务             │
│  ┌──────────────────────────────┐  │
│  │   OpenAI Router              │  │
│  │   - 请求验证                  │  │
│  │   - 格式转换                  │  │
│  │   - 流式处理                  │  │
│  └──────────┬───────────────────┘  │
│             │                       │
│  ┌──────────▼───────────────────┐  │
│  │   Assembly Client            │  │
│  │   - API Key 轮换             │  │
│  │   - 重试机制                  │  │
│  │   - 错误处理                  │  │
│  └──────────┬───────────────────┘  │
│             │                       │
│  ┌──────────▼───────────────────┐  │
│  │   Storage Adapter            │  │
│  │   - 配置管理                  │  │
│  │   - 使用统计                  │  │
│  │   - 状态存储                  │  │
│  └──────────────────────────────┘  │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│    AssemblyAI LLM Gateway           │
│  (GPT, Claude, Gemini, etc.)        │
└─────────────────────────────────────┘
```

### 核心模块

#### 1. OpenAI Router (`src/openai_router.py`)
- 处理 OpenAI 格式的 API 请求
- 提供 `/v1/models` 和 `/v1/chat/completions` 端点
- 支持流式和非流式响应
- 实现假流式模式（定期心跳 + 最终内容）

#### 2. Assembly Client (`src/assembly_client.py`)
- 与 AssemblyAI LLM Gateway 通信
- 智能 API Key 轮换机制（基于速率限制状态）
- 速率限制实时追踪（每个 Key 独立监控）
- 429/400 速率限制错误自动重试和切换
- 模型列表查询和缓存
- 支持不同规格的 API Keys（如 30次/分钟、60次/分钟）

#### 3. Storage Adapter (`src/storage_adapter.py`)
- 统一的存储接口抽象
- 支持多种存储后端：
  - **Redis**：高性能分布式缓存（优先级最高）
  - **PostgreSQL**：关系型数据库存储
  - **MongoDB**：文档型数据库存储
  - **File**：本地文件存储（默认）
- 自动选择可用的存储后端

#### 4. Admin Routes (`src/admin_routes.py`)
- Web 管理面板接口
- 配置管理（读取/保存）
- 使用统计查询
- 日志流式传输和下载
- 模型列表管理

#### 5. Usage Stats (`src/usage_stats.py`)
- API 调用统计
- 每日配额管理（UTC 7:00 重置）
- 按模型分类统计
- 支持自定义限额

## 快速开始

### 使用 Docker Compose（推荐）

1. 克隆项目并进入目录：
```bash
git clone <repository-url>
cd amb2api
```

2. 复制环境变量配置文件：
```bash
cp .env.example .env
```

3. 编辑 `.env` 文件，配置必要的参数：
```bash
# 基础配置
API_PASSWORD=your_secure_password
PANEL_PASSWORD=your_panel_password

# AssemblyAI 配置
USE_ASSEMBLY=true
ASSEMBLY_API_KEYS=your_assembly_api_key_1,your_assembly_api_key_2

# 可选：Redis 配置（用于分布式部署）
REDIS_URI=redis://:password@host:6379/0
```

4. 启动服务：
```bash
docker-compose up -d
```

5. 访问服务：
- API 端点：`http://localhost:7861/v1`
- 管理面板：`http://localhost:7861/ui`

### 本地开发部署

#### 环境要求
- Python 3.12+
- pip 或 uv（推荐）

#### 安装步骤

1. 安装依赖：
```bash
# 使用 pip
pip install -r requirements.txt

# 或使用 uv（更快）
uv pip install -r requirements.txt
```

2. 配置环境变量：
```bash
cp .env.example .env
# 编辑 .env 文件
```

3. 启动服务：
```bash
python web.py
```

## 配置说明

### 环境变量

#### 服务器配置
- `HOST`：监听地址（默认：`0.0.0.0`）
- `PORT`：监听端口（默认：`7861`）
- `API_PASSWORD`：API 访问密码
- `PANEL_PASSWORD`：管理面板密码
- `PASSWORD`：通用密码（会覆盖上述两个密码）

#### AssemblyAI 配置
- `USE_ASSEMBLY`：是否使用 AssemblyAI（默认：`true`）
- `ASSEMBLY_ENDPOINT`：AssemblyAI 端点 URL
- `ASSEMBLY_API_KEYS`：API Keys（多个用逗号分隔）

#### 存储配置
- `REDIS_URI`：Redis 连接 URI（可选）
- `REDIS_PREFIX`：Redis 键前缀（默认：`AMB2API`）
- `POSTGRES_DSN`：PostgreSQL 连接字符串（可选）
- `MONGODB_URI`：MongoDB 连接 URI（可选）

#### 性能配置
- `CALLS_PER_ROTATION`：API Key 轮换周期（默认：`100`）
- `RETRY_429_ENABLED`：是否启用 429 重试（默认：`true`）
- `RETRY_429_MAX_RETRIES`：最大重试次数（默认：`5`）
- `RETRY_429_INTERVAL`：重试间隔秒数（默认：`1`）

#### 自动封禁配置
- `AUTO_BAN`：是否启用自动封禁（默认：`false`）
- `AUTO_BAN_ERROR_CODES`：触发封禁的错误码（默认：`401,403`）

#### 日志配置
- `LOG_LEVEL`：日志级别
  - `debug` - 详细日志，包含完整请求/响应报文（开发调试）
  - `info` - 简要日志，只记录关键信息（默认，生产推荐）
  - `warning` - 只记录警告和错误
  - `error` - 只记录错误
- `LOG_FILE`：日志文件路径（默认：`log.txt`）

**日志级别对比：**
```bash
# DEBUG - 详细日志（开发调试）
LOG_LEVEL=debug  # 包含完整请求/响应报文、详细处理流程

# INFO - 简要日志（生产环境）
LOG_LEVEL=info   # 只记录请求/响应状态、关键操作

# WARNING - 只看警告
LOG_LEVEL=warning  # 只记录警告和错误
```

详见 [日志使用指南](LOGGING_GUIDE.md)

#### 其他配置
- `PROXY`：HTTP 代理地址（可选）
- `CONFIG_OVERRIDE_ENV`：环境变量是否覆盖存储配置（默认：`true`）

### 配置优先级

配置读取优先级（从高到低）：
1. 环境变量（当 `CONFIG_OVERRIDE_ENV=true` 时）
2. 存储后端中的配置
3. 代码中的默认值

## API 使用

### 获取模型列表

```bash
curl -H "Authorization: Bearer your_api_password" \
  http://localhost:7861/v1/models
```

### 聊天完成（非流式）

```bash
curl -X POST http://localhost:7861/v1/chat/completions \
  -H "Authorization: Bearer your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

### 聊天完成（流式）

```bash
curl -X POST http://localhost:7861/v1/chat/completions \
  -H "Authorization: Bearer your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "stream": true
  }'
```

## 管理面板

访问 `http://localhost:7861/ui` 进入 Web 管理面板。

### 功能特性

1. **配置管理**
   - 在线修改 API Keys
   - 调整性能参数
   - 配置重试策略

2. **模型管理**
   - 查询可用模型列表
   - 选择启用的模型
   - 查看模型元数据

3. **使用统计**
   - 查看 API 调用统计
   - 按模型分类统计
   - 按 API Key 统计
   - 配额管理

4. **速率限制追踪** ⭐ 新功能
   - 实时监控每个 API Key 的速率限制状态
   - 显示限额、已用次数、剩余次数
   - 重置倒计时
   - 支持不同规格的 Keys（30次/分钟、60次/分钟等）
   - 自动重置机制
   - 详见 [RATE_LIMIT_TRACKING.md](./RATE_LIMIT_TRACKING.md)

5. **日志监控**
   - 实时日志流
   - 日志下载
   - 日志清空

6. **存储信息**
   - 查看当前存储后端
   - 存储状态监控

## 存储后端

### Redis（推荐用于生产环境）

```bash
# 环境变量配置
REDIS_URI=redis://:password@host:6379/0
REDIS_PREFIX=AMB2API
```

优点：
- 高性能
- 支持分布式部署
- 自动过期管理

### PostgreSQL

```bash
# 环境变量配置
POSTGRES_DSN=postgresql://user:password@host:5432/database
```

优点：
- 关系型数据结构
- 强一致性
- 支持复杂查询

### MongoDB

```bash
# 环境变量配置
MONGODB_URI=mongodb://user:password@host:27017/database
```

优点：
- 灵活的文档结构
- 易于扩展
- 适合大规模数据

### 本地文件（默认）

无需额外配置，自动使用 `./creds` 目录存储数据。

优点：
- 零配置
- 适合单机部署
- 易于备份

## 项目结构

```
amb2api/
├── src/                          # 源代码目录
│   ├── admin_routes.py          # 管理面板路由
│   ├── assembly_client.py       # AssemblyAI 客户端
│   ├── openai_router.py         # OpenAI 兼容路由
│   ├── models.py                # 数据模型定义
│   ├── storage_adapter.py       # 存储适配器
│   ├── usage_stats.py           # 使用统计
│   ├── state_manager.py         # 状态管理
│   ├── task_manager.py          # 任务管理
│   ├── httpx_client.py          # HTTP 客户端
│   ├── openai_transfer.py       # 格式转换
│   ├── anti_truncation.py       # 抗截断处理
│   ├── format_detector.py       # 格式检测
│   ├── utils.py                 # 工具函数
│   └── storage/                 # 存储后端实现
│       ├── file_storage_manager.py
│       ├── redis_manager.py
│       ├── postgres_manager.py
│       ├── mongodb_manager.py
│       └── cache_manager.py
├── front/                        # 前端文件
│   └── control_panel.html       # 管理面板
├── creds/                        # 凭证目录（本地存储）
├── config.py                     # 配置管理
├── web.py                        # 主入口文件
├── log.py                        # 日志模块
├── docker-compose.yml            # Docker Compose 配置
├── Dockerfile                    # Docker 镜像构建
├── requirements.txt              # Python 依赖
├── pyproject.toml               # 项目元数据
└── .env.example                 # 环境变量示例

```

## 开发指南

### 添加新的存储后端

1. 在 `src/storage/` 目录创建新的管理器类
2. 实现 `StorageBackend` 协议中的所有方法
3. 在 `storage_adapter.py` 中添加初始化逻辑

### 添加新的路由

1. 在 `src/` 目录创建新的路由文件
2. 定义 FastAPI 路由器
3. 在 `web.py` 中注册路由器

### 运行测试

```bash
# 安装开发依赖
pip install -r requirements.txt

# 运行测试（如果有）
pytest
```

## 故障排查

### 常见问题

1. **无法连接到 AssemblyAI**
   - 检查 `ASSEMBLY_API_KEYS` 是否正确配置
   - 验证网络连接
   - 检查代理设置（如果使用）

2. **Redis 连接失败**
   - 验证 `REDIS_URI` 格式
   - 检查 Redis 服务是否运行
   - 确认防火墙规则

3. **API 返回 403 错误**
   - 检查 `API_PASSWORD` 是否正确
   - 确认 Authorization header 格式

4. **模型列表为空**
   - 访问管理面板的"模型管理"
   - 点击"查询模型"按钮
   - 选择需要的模型并保存

### 日志查看

```bash
# Docker 部署
docker-compose logs -f amb2api

# 本地部署
tail -f log.txt
```

## 性能优化

### 建议配置

1. **生产环境**：
   - 使用 Redis 作为存储后端
   - 配置多个 API Keys 实现负载均衡
   - 启用 429 重试机制
   - 适当调整 `CALLS_PER_ROTATION`

2. **开发环境**：
   - 使用本地文件存储
   - 单个 API Key
   - 降低日志级别

### 扩展部署

支持水平扩展：
1. 使用 Redis 或 PostgreSQL 作为共享存储
2. 部署多个 AMB2API 实例
3. 使用负载均衡器（如 Nginx）分发请求

## 安全建议

1. **密码管理**
   - 使用强密码
   - 定期更换密码
   - 不要在代码中硬编码密码

2. **网络安全**
   - 使用 HTTPS（通过反向代理）
   - 限制管理面板访问 IP
   - 配置防火墙规则

3. **API Key 保护**
   - 不要在日志中记录完整 API Key
   - 使用环境变量或安全存储
   - 定期轮换 API Keys

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

如有问题或建议，请通过以下方式联系：
- 提交 GitHub Issue
- 发送邮件至项目维护者

## 更新日志

### v0.2.0 (最新)
- ⭐ **新增速率限制追踪功能**
  - 实时监控每个 API Key 的速率限制状态
  - 独立追踪每个 Key 的限额、已用次数、剩余次数
  - 自动重置机制和倒计时显示
  - 支持不同规格的 API Keys
  - 提供 `/admin/rate-limits` API 端点
- 🔧 **优化 Key 轮换机制**
  - 基于响应头判断速率限制（而非错误消息）
  - 400 错误时自动切换 Key
  - 智能选择可用的 Key
- 📝 **完善文档**
  - 新增 [RATE_LIMIT_TRACKING.md](./RATE_LIMIT_TRACKING.md) - 技术文档
  - 新增 [RATE_LIMIT_API_USAGE.md](./RATE_LIMIT_API_USAGE.md) - API 使用指南
  - 新增 [API_KEY_ROTATION_FIX.md](./API_KEY_ROTATION_FIX.md) - Key 轮换优化说明
- 🧪 **测试工具**
  - 新增 `test_rate_limit.py` - Python 测试脚本
  - 新增 `test_rate_limit.sh` - Bash 测试脚本

### v0.1.0
- 初始版本发布
- 支持 OpenAI 兼容接口
- 集成 AssemblyAI LLM Gateway
- 提供 Web 管理面板
- 支持多种存储后端
- 实现使用统计和配额管理

## 相关文档

### 速率限制追踪功能 ⭐
- **[RATE_LIMIT_INDEX.md](./RATE_LIMIT_INDEX.md)** - 📚 文档索引（推荐从这里开始）
- [RATE_LIMIT_UI_GUIDE.md](./RATE_LIMIT_UI_GUIDE.md) - 🖥️ Web 界面使用指南
- [RATE_LIMIT_QUICKSTART.md](./RATE_LIMIT_QUICKSTART.md) - 🚀 5分钟快速上手
- [RATE_LIMIT_TRACKING.md](./RATE_LIMIT_TRACKING.md) - 📖 完整技术文档
- [RATE_LIMIT_API_USAGE.md](./RATE_LIMIT_API_USAGE.md) - 💻 API 使用指南
- [RATE_LIMIT_FEATURE_SUMMARY.md](./RATE_LIMIT_FEATURE_SUMMARY.md) - 📝 功能总结
- [API_KEY_ROTATION_FIX.md](./API_KEY_ROTATION_FIX.md) - 🔧 Key 轮换机制优化
- [IMPLEMENTATION_COMPLETE.md](./IMPLEMENTATION_COMPLETE.md) - ✅ 实现完成总结

### 问题修复
- [USAGE_STATS_FIX.md](./USAGE_STATS_FIX.md) - 使用统计冗余数据修复
- [TOOL_CALLS_FIX.md](./TOOL_CALLS_FIX.md) - Tool Calls 支持修复
- [LOGGING_IMPROVEMENTS.md](./LOGGING_IMPROVEMENTS.md) - 日志系统优化

### 开发指南
- [DEBUG_GUIDE.md](./DEBUG_GUIDE.md) - 调试指南
- [LOGGING_GUIDE.md](./LOGGING_GUIDE.md) - 日志使用指南
- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - 快速参考
