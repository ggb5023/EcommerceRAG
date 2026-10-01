<script setup lang="ts">
import type { Evidence } from '../api'
defineProps<{ evidence: Evidence[]; draft: string }>()
</script>
<template>
  <aside class="evidence panel"><div class="evidence-heading"><div><p class="kicker">TRACEABILITY</p><h2>证据与引用</h2></div><span class="count">{{ evidence.length }}</span></div><div v-if="!evidence.length" class="evidence-empty"><div class="doc-icon">▤</div><p>发送问题后，这里会显示经过权限过滤的来源。</p></div><article v-for="item in evidence" :key="item.id" class="evidence-card"><div class="evidence-card-top"><span>{{ item.sourceType }}</span><span class="score">{{ Math.round(item.score * 100) }}%</span></div><h3>{{ item.title }}</h3><p>{{ item.snippet }}</p><footer>{{ item.documentId }} · {{ item.versionId }}</footer></article><div v-if="draft" class="draft-panel"><div class="draft-head"><h3>回复草稿</h3><span class="mock-tag">模拟数据</span></div><p>{{ draft }}</p><button class="copy-btn" disabled title="模拟结果不可复制">复制草稿 <span>仅真实生成可用</span></button></div></aside>
</template>
