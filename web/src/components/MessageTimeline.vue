<script setup lang="ts">
import type { Message } from '../api'
defineProps<{ messages: Message[]; busy: boolean; turnStatus: string }>()
</script>
<template>
  <div class="messages">
    <div v-if="!messages.length && !busy" class="welcome"><div class="welcome-icon">✦</div><h2>从一个客户问题开始</h2><p>答案会附带可追溯的知识证据。当前为隔离环境，生成内容仅供预览。</p></div>
    <div v-for="(message, index) in messages" :key="index" class="message" :class="message.role"><div class="avatar">{{ message.role === 'user' ? '你' : 'AI' }}</div><div><div class="message-meta">{{ message.role === 'user' ? '客服' : '助手' }}<span v-if="message.mock" class="mock-tag">模拟数据</span></div><div class="bubble">{{ message.content }}</div></div></div>
    <div v-if="busy" class="message assistant"><div class="avatar">AI</div><div><div class="message-meta">助手<span class="typing">{{ turnStatus || '处理中' }} · 正在检索证据…</span></div><div class="bubble loading"><i></i><i></i><i></i></div></div></div>
  </div>
</template>
