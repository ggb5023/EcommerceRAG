<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { Message } from '../api'
const props = defineProps<{ messages: Message[]; busy: boolean; turnStatus: string; draft: string; selectedRequestId: string }>()
const emit = defineEmits<{ select: [requestId: string] }>()
const timeline = ref<HTMLElement>()
watch(() => [props.messages.length, props.draft], async () => { await nextTick(); if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight })
</script>
<template>
  <div ref="timeline" class="messages" aria-label="消息时间线" tabindex="0">
    <div v-if="!messages.length && !busy" class="welcome"><div class="welcome-icon">✦</div><h2>从一个客户问题开始</h2><p>可以查询已导入的合成商品、政策和 FAQ。答案和证据仅供演示。</p></div>
    <div v-for="(message, index) in messages" :key="`${message.request_id}-${message.role}-${index}`" class="message" :class="message.role"><div class="avatar">{{ message.role === 'user' ? '你' : 'AI' }}</div><div><div class="message-meta">{{ message.role === 'user' ? '客服' : '助手' }}<span v-if="message.mock" class="mock-tag">模拟数据</span></div><div class="bubble">{{ message.content }}</div><button v-if="message.role === 'assistant' && message.request_id" class="reference-btn" :disabled="busy" :aria-pressed="message.request_id === selectedRequestId" @click="emit('select', message.request_id)">查看本轮证据</button></div></div>
    <div v-if="busy" class="message assistant"><div class="avatar">AI</div><div><p class="message-meta" role="status">{{ turnStatus || '提交中' }} · {{ turnStatus === 'CANCEL_REQUESTED' ? '等待服务端取消' : '正在处理' }}</p><div class="bubble">{{ draft || '正在检索证据…' }}</div></div></div>
  </div>
</template>
