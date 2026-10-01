# ecommerce-demo-v1

这是 EcommerceRAG 的可复现合成电商数据包，用于本地导入、解析、检索、权限和评测原型。

所有内容均为虚构资料：

- `synthetic=true`
- `provisional=true`
- `license=internal-generated`

资料不代表任何真实商家政策、价格、库存、订单或身份系统。`manifest.yaml` 是目录和版本入口，`acl.yaml` 只用于合成测试，不能授予真实权限。

使用方式：

```bash
python -m app.ingest validate --manifest data/synthetic/ecommerce-demo-v1/manifest.yaml
python -m app.ingest import --manifest data/synthetic/ecommerce-demo-v1/manifest.yaml
python -m app.ingest status --dataset ecommerce-demo-v1
```

导入 CLI 使用本地文件和 `.local/ingest-state` 状态目录，不写 PostgreSQL 或 OSS。任一校验错误会拒绝整个数据包，不产生部分索引。ACL YAML 只作为内部测试记录导入，不会授予服务端权限。

本地检索入口：

```bash
PYTHONPATH=python python3 -m app.ingest index-search \
  --manifest data/synthetic/ecommerce-demo-v1/manifest.yaml \
  --query '保温杯 容量' --tenant demo-tenant-a --shop demo-shop-east
```

索引使用确定性本地关键词匹配，返回 document/version/chunk、来源、评分、引用序号和披露级别。ACL YAML 不会进入 customer evidence；内部资料只对合成 admin/owner 检索可见。DOCX 解析支持标准 WordprocessingML，未安装可选 `python-docx` 时使用受限内置解析器。

隔离工作台运行时使用显式 `RAG_PROFILE=synthetic_import_mock` 和 `SYNTHETIC_MANIFEST`，由 Go 的可信资源登记和当前合成身份授权后调用 Python Search/Generate。会话、回答、证据、引用和 SSE 状态存入隔离 PostgreSQL；检索索引仍在 Python 本地。真实身份和在线 Provider 尚未接入，所有回答保持 `is_mock=true`、复制禁用。

`public-facts.csv` 是独立的合成 `external_allowed` 事实文档，有效区间为 `[2026-10-02, 2026-10-03)`。价格和库存仅表示该日虚构快照，不提供实时库存、订单状态或履约保证；原内部事实文档的权限和日期保持不变。
