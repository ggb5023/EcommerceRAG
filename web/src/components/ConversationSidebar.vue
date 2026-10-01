<script setup lang="ts">
import type { Conversation } from '../api'
defineProps<{ items: Conversation[]; selectedId?: string; shopName?: string; role?: string }>()
const emit = defineEmits<{ (event: 'new'): void; (event: 'select', item: Conversation): void }>()
</script>
<template>
  <aside class="sidebar panel">
    <div class="side-heading"><div><p class="kicker">CONVERSATIONS</p><h2>近期会话</h2></div><button class="icon-btn" title="新建会话" @click="emit('new')">+</button></div>
    <button class="new-chat" @click="emit('new')">＋ 新建会话</button>
    <div class="conversation-list"><button v-for="item in items" :key="item.id" class="conversation" :class="{ selected: item.id === selectedId }" @click="emit('select', item)"><span class="conversation-title">{{ item.title }}</span><span class="conversation-time">{{ item.messages.length ? `${item.messages.length} 条消息` : '空会话' }}</span></button></div>
    <div class="scope"><p class="kicker">ACCESS SCOPE</p><strong>{{ shopName ?? '演示店铺' }}</strong><span>{{ role ?? '客服运营' }} · 只读知识</span></div>
  </aside>
</template>
