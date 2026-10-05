# 受控网页来源注册

`source-registry-v1.json` 是当前唯一的网页来源策略入口。它只登记来源、域名/path、许可证与条款审阅、robots、刷新预算、保留策略和责任角色，不包含网页正文。`responsibility_contract` 及每条来源的 `responsibility` 记录是 P7 的可审阅责任门禁，当前保持 `pending_review`。

当前三个官方文档来源均为 `pending_review`，active 数量为 0；来源激活门禁仍为 `PENDING_REVIEW`。2026-10-05 的 test-only crawler smoke 对 Shopify Developer 和 WooCommerce 固定入口通过，对 Raspberry Pi 固定入口收到 `http_403`，因此总体为 2/3、`FAIL`；该结果只验证受控失败路径，不代表来源获批或可进入业务链路。首轮来源为 Raspberry Pi、Shopify Developer 和 WooCommerce 官方文档。Tavily 的独立脱敏 smoke 不能绕过来源注册，也不能把搜索结果升级为商品、政策、价格、库存、权限或客服真值。启用来源前必须完成条款/许可证、robots、域名与路径、预算、刷新、保留和责任人审阅，并通过：

```bash
python3 scripts/validate_source_registry.py
python3 scripts/audit_crawler_sources.py
```

`validate_source_registry.py` 校验来源结构和 active 门禁；`audit_crawler_sources.py` 进一步校验官方来源的页数/字节预算、允许内容类型和待审保留策略。后者为只读离线审计，输出 `network_requests=0`、官方来源数量和 `PENDING_REVIEW` 发布门禁，不请求 robots、DNS、HTTP 或 Tavily。两个检查通过都不等于来源许可已批准。

原始网页快照只能存放在仓外受限目录，不能提交 Git、写入公开构建产物或进入 M1 客服链路。当前公开状态以本注册表和 `eval/README.md` 为准；私有运行证据由服务器文档维护。

test-only smoke 报告位于私有运行目录 `.local/smoke/crawler-p3-current/report.json`，不保存网页正文；公网来源仍不可用于价格、库存、订单、商家政策、租户权限或客服真值。Raspberry Pi 的 403 保留为可复现失败，不通过更换未审 URL 或绕过站点策略解除。
