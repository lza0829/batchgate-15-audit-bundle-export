<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJSON, postJSON, downloadFile } from '../api'
const history = ref([])
const last = ref(null)
const exportsList = ref([])
const busy = ref(false)
const busyExportId = ref(null)
const err = ref('')
const manifestExportId = ref(null)
const manifestData = ref(null)

const exportsByOrch = computed(() => {
  const map = new Map()
  for (const e of exportsList.value) {
    const arr = map.get(e.orchestration_id) || []
    arr.push(e)
    map.set(e.orchestration_id, arr)
  }
  return map
})

async function refresh() {
  history.value = await getJSON('/api/orchestrations')
  exportsList.value = await getJSON('/api/exports')
}
async function runOnce() {
  err.value = ''; busy.value = true
  try {
    last.value = await postJSON('/api/orchestrations')
    await refresh()
  } catch (e) { err.value = String(e.message || e) }
  finally { busy.value = false }
}
async function exportBundle(o) {
  err.value = ''; busyExportId.value = o.id
  try {
    await postJSON(`/api/orchestrations/${o.id}/exports`)
    await refresh()
  } catch (e) { err.value = String(e.message || e) }
  finally { busyExportId.value = null }
}
async function openManifest(rec) {
  err.value = ''
  try {
    manifestExportId.value = rec.id
    manifestData.value = await getJSON(`/api/exports/${rec.id}/manifest`)
  } catch (e) { err.value = String(e.message || e) }
}
function closeManifest() {
  manifestExportId.value = null
  manifestData.value = null
}
async function download(rec) {
  err.value = ''
  try {
    await downloadFile(`/api/exports/${rec.id}/download`)
  } catch (e) { err.value = String(e.message || e) }
}
onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>编排台</h1>
    <p class="muted">触发一次全图编排：按依赖层执行；上游未成功则下游记 skipped_dep。完成后可导出审计包（含报告摘要、尝试证据、作业依赖快照与内容哈希清单）。</p>
    <div class="panel">
      <button :disabled="busy" @click="runOnce">{{ busy ? '编排中…' : '执行编排' }}</button>
    </div>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel" v-if="last">
      <h3>最近一次报告</h3>
      <p>
        #{{ last.id }} · <span class="tag ok">{{ last.status }}</span> ·
        succeeded {{ last.report?.succeeded }} /
        failed {{ last.report?.failed }} /
        skipped {{ last.report?.skipped }} /
        attempts {{ last.report?.attempt_count }}
      </p>
      <p class="mono muted">watermarked_jobs={{ last.report?.watermarked_jobs }} layers={{ last.report?.layer_count }}</p>
      <p>
        <router-link :to="{ path: '/attempts', query: { orchestration_id: last.id } }">查看本次尝试 →</router-link>
        ·
        <router-link to="/watermarks">产出水位 →</router-link>
      </p>
    </div>
    <div class="panel">
      <h3>历史编排与审计包</h3>
      <table>
        <thead><tr><th>ID</th><th>状态</th><th>成功</th><th>失败</th><th>跳过</th><th>开始</th><th>审计包</th></tr></thead>
        <tbody>
          <tr v-for="o in history" :key="o.id">
            <td>{{ o.id }}</td>
            <td>{{ o.status }}</td>
            <td>{{ o.report?.succeeded }}</td>
            <td>{{ o.report?.failed }}</td>
            <td>{{ o.report?.skipped }}</td>
            <td class="mono">{{ o.started_at }}</td>
            <td>
              <button
                class="secondary"
                :disabled="busyExportId === o.id || o.status === 'running'"
                @click="exportBundle(o)"
              >{{ busyExportId === o.id ? '导出中…' : '导出审计包' }}</button>
              <div v-if="exportsByOrch.get(o.id)?.length" class="export-list">
                <div v-for="rec in exportsByOrch.get(o.id)" :key="rec.id" class="export-row">
                  <span class="tag">v{{ rec.version }}</span>
                  <a href="#" @click.prevent="download(rec)">下载</a>
                  <a href="#" @click.prevent="openManifest(rec)">清单</a>
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel manifest-panel" v-if="manifestData">
      <div class="manifest-head">
        <h3>审计包清单 · 编排 #{{ manifestData.orchestration_id }} · v{{ manifestData.export_version }}</h3>
        <button class="secondary" @click="closeManifest">关闭</button>
      </div>
      <p class="mono muted">
        导出时间 {{ manifestData.created_at }} · 哈希算法 {{ manifestData.hash_alg }} ·
        文件 {{ manifestData.counts.file_count }} 个 · 尝试证据 {{ manifestData.counts.evidence_count }} 条
      </p>
      <p class="mono muted">
        报告口径：成功 {{ manifestData.report_totals.succeeded }} /
        失败 {{ manifestData.report_totals.failed }} /
        跳过 {{ manifestData.report_totals.skipped }} /
        尝试 {{ manifestData.report_totals.attempt_count }}；
        证据口径：<span v-for="(n, s) in manifestData.counts.status_counts" :key="s">{{ s }}={{ n }} </span>
      </p>
      <table>
        <thead><tr><th>包内文件</th><th>字节数</th><th>SHA-256 内容哈希</th></tr></thead>
        <tbody>
          <tr v-for="f in manifestData.files" :key="f.path">
            <td class="mono">{{ f.path }}</td>
            <td class="mono">{{ f.bytes }}</td>
            <td class="mono hash-cell">{{ f.sha256 }}</td>
          </tr>
          <tr>
            <td class="mono">{{ manifestData.package.filename }}（整包）</td>
            <td class="mono hash-cell" colspan="2">
              整包 SHA-256 已随导出记录落库，下载后可本地核对 package_sha256
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.export-list { margin-top: 0.4rem; display: flex; flex-direction: column; gap: 0.25rem; }
.export-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; }
.export-row a { color: var(--accent); }
.manifest-panel { border-color: var(--accent); }
.manifest-head { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.hash-cell { word-break: break-all; font-size: 0.8rem; }
</style>
