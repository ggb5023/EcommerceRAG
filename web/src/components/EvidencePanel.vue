<script setup lang="ts">
import type { Evidence, LastTurn } from '../api'
defineProps<{ evidence: Evidence[]; draft: string; citations: LastTurn['citations']; turn: LastTurn | null }>()
</script>
<template>
  <aside class="evidence panel"><div class="evidence-heading"><div><p class="kicker">TRACEABILITY</p><h2>证据与引用</h2></div><span class="count">{{ evidence.length }}</span></div><p v-if="turn" class="result-binding">{{ turn.status }} · 第 {{ turn.execution_no }} 次执行<br>Turn {{ turn.turn_id }} · {{ turn.request_id }}</p><div v-if="!evidence.length" class="evidence-empty"><p>本轮没有可展示的授权证据。澄清和拒答不会伪造引用。</p></div><article v-for="item in evidence" :key="item.id" class="evidence-card" :data-evidence-id="item.id"><div class="evidence-card-top"><span>{{ item.sourceType }}</span><span class="mock-tag">合成资料</span></div><h3>{{ item.title }}</h3><p>{{ item.snippet }}</p><footer>{{ item.documentId }} · {{ item.versionId }}<br>{{ item.sourceRef }}</footer><p class="citation" v-if="citations.some(c => c.evidence_id === item.id)">引用 [{{ citations.find(c => c.evidence_id === item.id)?.citation_index }}]</p></article><div v-if="draft" class="draft-panel"><div class="draft-head"><h3>回复草稿</h3><span class="mock-tag">模拟生成</span></div><p>{{ draft }}</p><button class="copy-btn" disabled title="模拟结果不可复制">复制草稿 · 模拟结果不可复制</button></div></aside>
</template>
