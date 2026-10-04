---
synthetic: true
provisional: true
license: internal-generated
---

# 撤销版本与撤权会话规则

版本撤销或店铺授权撤销后，旧会话缓存不能继续作为检索依据。系统必须重新检查当前版本、tenant/shop scope 和撤权时间；已撤销资料不能用于新的客户回复，即使上一轮曾经引用过它。
