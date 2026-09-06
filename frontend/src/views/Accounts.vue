<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { useDataStore } from '../stores/data'
import type { AccountInfo } from '../types'

const data = useDataStore()

const rows = ref<AccountInfo[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const submitting = ref(false)
const form = ref({ username: '', password: '', assigned_site: '' })

// 池子选项来自代理层动态发现的站点（与站点视图一致）
const poolOptions = computed<string[]>(
  () => data.overview?.sites.map((s) => s.name) ?? [],
)

async function load() {
  loading.value = true
  try {
    rows.value = (await api.accounts()).accounts
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载账号失败')
  } finally {
    loading.value = false
  }
}

function openDialog() {
  form.value = { username: '', password: '', assigned_site: poolOptions.value[0] ?? '' }
  dialogVisible.value = true
}

async function submit() {
  const f = form.value
  if (!f.username.trim() || !f.password || !f.assigned_site) {
    ElMessage.warning('用户名、密码、绑定池都不能为空')
    return
  }
  submitting.value = true
  try {
    await api.createAccount({
      username: f.username.trim(),
      password: f.password,
      assigned_site: f.assigned_site,
    })
    ElMessage.success(`账号 ${f.username.trim()} 已创建`)
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '创建失败')
  } finally {
    submitting.value = false
  }
}

async function remove(row: AccountInfo) {
  try {
    await ElMessageBox.confirm(
      `确定删除账号「${row.username}」？删除后其凭据立即失效。`,
      '删除账号',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.deleteAccount(row.username)
    ElMessage.success(`账号 ${row.username} 已删除`)
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '删除失败')
  }
}

function fmtCreated(iso: string) {
  return new Date(iso).toLocaleString()
}

onMounted(load)
</script>

<template>
  <div class="accounts">
    <div class="toolbar">
      <el-button type="primary" @click="openDialog">新增账号</el-button>
      <el-button :loading="loading" @click="load">刷新</el-button>
      <span class="hint">
        凭据用于下游服务调接口领 IP（Basic 认证）或填隧道代理
        http://用户名:密码@主机:9001，绑定哪个池就只能用哪个池的 IP。
      </span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="username" label="用户名" min-width="140" />
      <el-table-column prop="assigned_site" label="绑定池" min-width="120">
        <template #default="{ row }">
          <el-tag>{{ row.assigned_site }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="180">
        <template #default="{ row }">{{ fmtCreated(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button type="danger" text @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新增账号" width="420px">
      <el-form label-width="80px">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" placeholder="下游服务标识，如 sub2api" />
        </el-form-item>
        <el-form-item label="密码" required>
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="服务端加盐哈希存储"
          />
        </el-form-item>
        <el-form-item label="绑定池" required>
          <el-select v-model="form.assigned_site" placeholder="选择二级池" style="width: 100%">
            <el-option
              v-for="name in poolOptions"
              :key="name"
              :label="name"
              :value="name"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.accounts {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-left: 8px;
}
</style>
