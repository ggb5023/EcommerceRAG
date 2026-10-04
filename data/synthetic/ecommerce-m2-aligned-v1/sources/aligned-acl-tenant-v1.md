---
synthetic: true
provisional: true
license: internal-generated
---

# 租户边界与内部折扣规则

租户、店铺和角色是服务端鉴权的边界。忽略店铺权限、读取另一租户内部员工折扣或绕过撤权状态都必须拒绝；前端隐藏菜单不能替代后端 scope 检查。
