<script setup lang="ts">
import { RouterLink } from 'vue-router'

import EmptyState from '@/components/EmptyState.vue'
</script>

<template>
  <div class="stack">
    <EmptyState
      icon="◌"
      title="这个地址没有对应的页面"
      body="可能是链接打错了，或者这个页面在升级时被挪走了。"
    >
      <RouterLink class="btn btn--primary" to="/">回到总览</RouterLink>
    </EmptyState>
  </div>
</template>
