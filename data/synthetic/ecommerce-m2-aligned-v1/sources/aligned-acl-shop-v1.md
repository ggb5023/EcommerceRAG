---
synthetic: true
provisional: true
license: internal-generated
---

# 店铺范围隔离规则

当前客服身份只允许访问授权店铺。另一个店铺的内部退货规则、同名商品和订单资料不得被合并到当前店铺答案；即使名称相同，也必须按 shop_id 分开检索并拒绝越权请求。
