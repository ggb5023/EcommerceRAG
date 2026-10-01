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

首版导入器使用本地文件和 `.local/ingest-state` 状态目录，不接入 OSS、PostgreSQL、真实身份、在线 Provider 或客服在线链路。任一校验错误会拒绝整个数据包，不产生部分索引。
