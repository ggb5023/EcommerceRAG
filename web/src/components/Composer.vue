<script setup lang="ts">
defineProps<{ modelValue: string; busy: boolean }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: string): void; (event: 'send'): void; (event: 'stop'): void }>()
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); emit('send') }
}
</script>
<template>
  <form class="composer" @submit.prevent="emit('send')"><label class="sr-only" for="question">客户问题</label><textarea id="question" :value="modelValue" :disabled="busy" placeholder="输入客户问题…" rows="3" @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" @keydown="keydown"></textarea><div class="composer-actions"><span>Enter 发送 · Shift + Enter 换行</span><button v-if="busy" type="button" class="stop-btn" @click="emit('stop')">停止</button><button v-else type="submit" class="send-btn" :disabled="!modelValue.trim()">发送 ↗</button></div></form>
</template>
