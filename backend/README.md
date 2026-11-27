# NovusAI SaaS Backend

依赖以 **`pyproject.toml`** 声明、**`uv.lock`** 锁定版本（须提交到仓库）。

## 安装

```bash
uv sync --extra dev
```

未安装 [uv](https://docs.astral.sh/uv/) 时：

```bash
pip install -e ".[dev]"
```

更多环境变量、迁移与启动命令见仓库根目录 **`README.md`**。

## 仓储租户归属

`TenantRepository.create(data)` 和 `create_many(data_list)` 按仓储的
租户写入归属并保留调用方字典。模型声明 `owner_tenant_id` 时使用该字段。
映射到同一列的 `tenant_id` 别名随之使用同一归属。

`get_next_sort_order(**scope_filters)` 和
`batch_update_sort_order(ordered_ids, **scope_filters)` 都在 SQL 中应用
租户归属、数据权限和模型声明的排序范围。`tenant_id=None` 对应平台
记录。`parent_id=None` 对应根层级。事务由调用方提交或回滚。

| 场景 | 预期 |
| --- | --- |
| 在租户仓储批量创建 | 每条新记录归当前仓储的租户 |
| 为根层级重新排序 | 只更新处于数据权限内的本租户根层级记录 |
| 使用 `tenant_id=None` 的租户仓储 | 归属或查询范围为 SQL NULL |

`tests/integration/test_tenant_repository_bulk_admission_postgresql.py` 和
`tests/integration/test_tenant_repository_sorting_scope_postgresql.py` 使用独立
PostgreSQL 校验实际写入、排序和回滚。测试分别由
`TENANT_BULK_POSTGRES_DSN` 和 `TENANT_SORTING_POSTGRES_DSN` 指定测试连接。

## 登录限流和调试

三个登录端使用 Redis 共享计数，沿用每个客户端 60 秒内 10 次的限制。
由反向代理转发时，在 `TRUSTED_PROXY_IPS` 中配置直接代理的 IP 或网络
CIDR，多个值用逗号分隔。空配置使用连接对端的地址。

`APP_ENV=production` 时使用 `DEBUG=false`。生产环境关闭调试响应及
Swagger、ReDoc 和 OpenAPI 文档入口。

## AI 供应商与工具执行

在供应商记录中设置自定义 OpenAI 兼容地址。地址为空时使用官方 OpenAI
端点。请求使用校验后的连接地址并保留原主机名用于 TLS，关闭环境代理、
自动重定向与 SDK 自动重试。需要内网网关时，在对应供应商配置中显式启用
`allow_private_network`。音频附件下载仍使用独立的公网准入和大小限制。

工具执行仅允许系统管理的 Toolkit。生产及预发布环境使用受信子进程模式。
普通代码工具请求在隔离运行环境接入前返回执行受限错误。
