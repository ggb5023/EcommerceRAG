<script setup lang="ts">
defineProps<{ modelValue: string; busy: boolean }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: string): void; (event: 'send'): void; (event: 'stop'): void }>()
</script>
<template>
  <form class="composer" @submit.prevent="emit('send')"><textarea :value="modelValue" :disabled="busy" placeholder="输入客户问题…" rows="3" @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" @keydown.enter.exact.prevent="emit('send')"></textarea><div class="composer-actions"><span>Enter 发送 · Shift + Enter 换行</span><div><button v-if="busy" type="button" class="stop-btn" @click="emit('stop')">停止</button><button v-else type="submit" class="send-btn" :disabled="!modelValue.trim()">发送 <span>↗</span></button></div></div></form>
</template>
