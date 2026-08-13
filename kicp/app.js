/* KICP 机器人视角管理页 */
const { createApp, reactive, ref, computed, onMounted } = Vue;

const TOKEN_KEY = 'kicp_token';

function qs(name) {
  return new URLSearchParams(location.search).get(name) || '';
}

createApp({
  setup() {
    // 支持 ?bot_id=xxx 或 ?botId=xxx
    const botId = ref(qs('bot_id') || qs('botId') || qs('robot_id'));
    const token = ref(localStorage.getItem(TOKEN_KEY) || '');
    const ready = ref(false);
    const needLogin = ref(false);
    const logining = ref(false);
    const loginError = ref('');
    const loginForm = reactive({ username: '', password: '' });

    const user = reactive({ username: '', role: '' });
    const robot = reactive({
      bot_id: '', department: '', department_label: '', platform: '', platform_label: '',
      company: '', enabled: true, prompt_version: -1, configured: false, updated_at: ''
    });
    const scope = reactive({});
    const meta = reactive({ departments: [], platforms: [], scenes: [], kb_types: [] });

    const tab = ref('');
    const modal = ref('');
    const editing = ref(false);
    const current = ref(null);
    const form = reactive({ content: '', description: '' });
    const toast = reactive({ show: false, msg: '', type: '' });

    const prompts = ref([]); const pTotal = ref(0);
    const pQuery = reactive({ keyword: '', page: 1, page_size: 20 });
    const versions = ref([]); const viewVersion = ref(null);
    const kbs = ref([]); const kTotal = ref(0);
    const iQuery = reactive({ keyword: '', type: '' });
    const flows = ref([]); const fTotal = ref(0);
    const flowRecords = ref([]);

    const editRecord = reactive({ id: 0, file_name: '', description: '', bot_id: '' });
    const itemForm = reactive({ _idx: null, text: '', type: '', bot_id: '' });

    function notify(msg, type = 'success') {
      toast.msg = msg; toast.type = type; toast.show = true;
      setTimeout(() => { toast.show = false; }, 2600);
    }

    async function api(path, opts = {}) {
      const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
      if (token.value) headers['Authorization'] = 'Bearer ' + token.value;
      const res = await fetch(path, Object.assign({}, opts, { headers }));
      if (res.status === 401) {
        token.value = ''; localStorage.removeItem(TOKEN_KEY);
        needLogin.value = true; ready.value = false;
        throw new Error('登录已过期，请重新登录');
      }
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
      if (!res.ok) throw new Error((data && data.detail) || ('请求失败 ' + res.status));
      return data;
    }

    const roleText = computed(() => (user.role === 'admin' ? '高级权限' : '普通权限'));

    const modules = computed(() => ([
      { key: 'prompt', name: '提示词管理', icon: '📝', desc: '查看与维护该机器人科室的提示词', allowed: !!scope.can_prompt_view },
      { key: 'knowledge', name: '知识库管理', icon: '📚', desc: '维护科室平台维度的知识条目', allowed: !!scope.can_knowledge_view },
      { key: 'flow', name: '流程树管理', icon: '🌳', desc: '查看与编辑流程树解析记录', allowed: !!scope.can_flow_view }
    ]));

    const currentModule = computed(() => modules.value.find(m => m.key === tab.value) || null);
    const pPages = computed(() => Math.max(1, Math.ceil(pTotal.value / pQuery.page_size)));

    function label(kind, value) {
      const list = meta[kind] || [];
      const hit = list.find(i => i.value === value);
      return hit ? hit.label : (value || '-');
    }

    // 知识库 content 为 JSON 数组：[{text, type, bot_id}] 或纯字符串数组
    function kbParse(content) {
      if (!content) return [];
      let arr = null;
      try { arr = JSON.parse(content); } catch (e) { arr = null; }
      if (!Array.isArray(arr)) {
        arr = String(content).split('\n').map(s => s.trim()).filter(Boolean);
      }
      return arr.map((it, i) => {
        if (typeof it === 'string') return { _idx: i, text: it, type: '', bot_id: '' };
        return {
          _idx: i,
          text: it.text || it.content || '',
          type: it.type || '',
          bot_id: it.bot_id || ''
        };
      });
    }

    // 当前知识库中属于本机器人的记录（bot_id 为空视为通用，全部机器人可见）
    const kbItems = computed(() => {
      if (!current.value || tab.value !== 'knowledge') return [];
      const bid = String(botId.value || '');
      return kbParse(current.value.content).filter(it => !it.bot_id || String(it.bot_id) === bid);
    });

    // 关键词 + 类型筛选后的记录
    const filteredItems = computed(() => {
      const kw = (iQuery.keyword || '').trim().toLowerCase();
      return kbItems.value.filter(it => {
        if (iQuery.type && it.type !== iQuery.type) return false;
        if (kw && !(it.text || '').toLowerCase().includes(kw)) return false;
        return true;
      });
    });

    // 列表页条数：同样只统计本机器人可见的记录
    function kbCount(content) {
      const bid = String(botId.value || '');
      return kbParse(content).filter(it => !it.bot_id || String(it.bot_id) === bid).length;
    }

    function serializeItem(it) {
      const o = { text: it.text };
      if (it.type) o.type = it.type;
      if (it.bot_id) o.bot_id = it.bot_id;
      return o;
    }

    /**
     * 按 _idx 对全量记录做增/改/删后序列化回 content。
     * 关键：页面只展示本机器人的记录，但写回必须保留其它机器人的记录，避免误删。
     * @param {'add'|'update'|'delete'} action
     */
    function buildContent(action, payload, idx) {
      const all = kbParse(current.value.content);
      if (action === 'add') {
        all.push(payload);
      } else if (action === 'update') {
        const t = all.find(i => i._idx === idx);
        if (!t) return null;
        Object.assign(t, payload);
      } else if (action === 'delete') {
        const pos = all.findIndex(i => i._idx === idx);
        if (pos < 0) return null;
        all.splice(pos, 1);
      }
      return JSON.stringify(all.map(serializeItem));
    }

    // ============ 会话与初始化 ============
    function applySession(data) {
      token.value = data.token || token.value;
      if (data.token) localStorage.setItem(TOKEN_KEY, data.token);
      Object.assign(user, data.user || {});
      Object.assign(robot, data.robot || {});
      Object.keys(scope).forEach(k => delete scope[k]);
      Object.assign(scope, data.scope || {});
      needLogin.value = false;
      ready.value = true;
      // 默认落到第一个有权限的模块
      const first = modules.value.find(m => m.allowed);
      tab.value = first ? first.key : (modules.value[0] ? modules.value[0].key : '');
      resetDetail();
      loadMeta().then(loadCurrentTab);
    }

    async function initSession(username, password) {
      const body = { bot_id: botId.value };
      if (username && password) { body.username = username; body.password = password; }
      const data = await api('/api/kicp/session', { method: 'POST', body: JSON.stringify(body) });
      applySession(data);
    }

    async function boot() {
      if (!botId.value) {
        notify('缺少机器人ID参数，请使用 /kicp?bot_id=xxx 访问', 'error');
        needLogin.value = false; ready.value = false;
        return;
      }
      // 已有 token 时优先复用，避免重复建会话
      if (token.value) {
        try {
          const data = await api('/api/kicp/context?bot_id=' + encodeURIComponent(botId.value));
          data.token = token.value;
          applySession(data);
          return;
        } catch (e) { token.value = ''; localStorage.removeItem(TOKEN_KEY); }
      }
      // 默认 admin 免登录
      try {
        await initSession();
      } catch (e) {
        needLogin.value = true;
        loginError.value = e.message;
      }
    }

    async function doLogin() {
      if (!loginForm.username || !loginForm.password) { loginError.value = '请输入用户名和密码'; return; }
      logining.value = true; loginError.value = '';
      try {
        await initSession(loginForm.username, loginForm.password);
        loginForm.password = '';
      } catch (e) { loginError.value = e.message; }
      finally { logining.value = false; }
    }

    async function loadMeta() {
      try {
        const [d, p, s, k] = await Promise.all([
          api('/api/meta/departments'), api('/api/meta/platforms'),
          api('/api/meta/scenes'), api('/api/meta/kb_types')
        ]);
        // 后端返回 {departments:[{key,label}]}，统一成 {value,label}
        const norm = (list) => (list || []).map(i => ({ value: i.key || i.value, label: i.label || i.key || i.value }));
        meta.departments = norm(d.departments);
        meta.platforms = norm(p.platforms);
        meta.scenes = norm(s.scenes);
        meta.kb_types = k.kb_types || [];
      } catch (e) { /* 元数据失败不阻塞主流程 */ }
    }

    function resetDetail() {
      current.value = null; editing.value = false;
      form.content = ''; form.description = '';
      versions.value = []; viewVersion.value = null;
      iQuery.keyword = ''; iQuery.type = '';
    }
    function cancelEdit() {
      editing.value = false;
      if (current.value) {
        form.content = current.value.content || '';
        form.description = current.value.description || '';
      }
    }

    function switchTab(m) {
      if (!m.allowed) { notify('当前账号没有「' + m.name + '」的访问权限', 'error'); return; }
      if (tab.value === m.key) return;
      tab.value = m.key;
      resetDetail();
      loadCurrentTab();
    }

    function loadCurrentTab() {
      if (!currentModule.value || !currentModule.value.allowed) return;
      if (tab.value === 'prompt') return loadPrompts();
      if (tab.value === 'knowledge') return loadKbs();
      if (tab.value === 'flow') return loadFlows();
    }

    // 机器人科室/平台作为默认过滤条件
    function scopeParams() {
      const p = new URLSearchParams();
      if (robot.department) p.set('department', robot.department);
      if (robot.platform) p.set('platform', robot.platform);
      return p;
    }

    // ============ 提示词 ============
    async function loadPrompts() {
      const p = scopeParams();
      if (pQuery.keyword) p.set('keyword', pQuery.keyword);
      p.set('page', pQuery.page); p.set('page_size', pQuery.page_size);
      try {
        const data = await api('/api/prompts?' + p.toString());
        prompts.value = data.items || []; pTotal.value = data.total || 0;
      } catch (e) { notify(e.message, 'error'); }
    }
    function reloadPrompts() { pQuery.page = 1; loadPrompts(); }

    async function selectPrompt(p) {
      editing.value = false; viewVersion.value = null; versions.value = [];
      try {
        const d = await api('/api/prompts/' + p.id);
        current.value = d; form.content = d.content || ''; form.description = d.description || '';
        loadVersions(d.id);
      } catch (e) { notify(e.message, 'error'); }
    }

    // ---- 历史版本 ----
    async function loadVersions(id) {
      try {
        const data = await api('/api/prompts/' + id + '/versions');
        const list = Array.isArray(data) ? data : (data.items || data.versions || []);
        versions.value = list.slice().sort((a, b) => b.version - a.version);
      } catch (e) { versions.value = []; }
    }
    function previewVersion(v) {
      if (editing.value) { notify('请先保存或取消编辑', 'error'); return; }
      if (current.value && v.version === current.value.version) { exitVersionView(); return; }
      viewVersion.value = v;
      form.content = v.content || '';
    }
    function exitVersionView() {
      viewVersion.value = null;
      if (current.value) form.content = current.value.content || '';
    }
    async function doRollback(v) {
      if (!confirm('确认将「' + current.value.name + '」回滚到 v' + v.version + '？将生成一个新版本。')) return;
      try {
        const d = await api('/api/prompts/' + current.value.id + '/rollback', {
          method: 'POST',
          body: JSON.stringify({ target_version: v.version })
        });
        current.value = d; viewVersion.value = null;
        form.content = d.content || ''; form.description = d.description || '';
        notify('已回滚到 v' + v.version);
        loadVersions(d.id); loadPrompts();
      } catch (e) { notify(e.message, 'error'); }
    }

    function startPromptEdit() {
      if (!scope.can_prompt_edit) { notify('无编辑权限', 'error'); return; }
      viewVersion.value = null;
      form.content = current.value.content || '';
      editing.value = true;
    }
    async function savePrompt() {
      try {
        const d = await api('/api/prompts/' + current.value.id, {
          method: 'PUT',
          body: JSON.stringify({ content: form.content, description: form.description, change_log: 'KICP 页面编辑' })
        });
        current.value = d; editing.value = false;
        notify('保存成功'); loadVersions(d.id); loadPrompts();
      } catch (e) { notify(e.message, 'error'); }
    }
    async function removePrompt() {
      if (!confirm('确认删除提示词「' + current.value.name + '」？')) return;
      try {
        await api('/api/prompts/' + current.value.id, { method: 'DELETE' });
        resetDetail(); notify('已删除'); loadPrompts();
      } catch (e) { notify(e.message, 'error'); }
    }

    // ============ 知识库 ============
    async function loadKbs() {
      try {
        const data = await api('/api/knowledge?' + scopeParams().toString());
        kbs.value = data.items || []; kTotal.value = data.total || 0;
      } catch (e) { notify(e.message, 'error'); }
    }
    async function selectKb(k) {
      editing.value = false;
      iQuery.keyword = ''; iQuery.type = '';
      try {
        const d = await api('/api/knowledge/' + k.id);
        current.value = d;
      } catch (e) { notify(e.message, 'error'); }
    }

    // ---- 知识记录：逐条增 / 改 / 删 ----
    function openCreateItem() {
      if (!scope.can_knowledge_edit) { notify('无编辑权限', 'error'); return; }
      itemForm._idx = null; itemForm.text = ''; itemForm.type = '';
      itemForm.bot_id = botId.value || '';
      modal.value = 'item';
    }
    function openEditItem(it) {
      if (!scope.can_knowledge_edit) { notify('无编辑权限', 'error'); return; }
      itemForm._idx = it._idx; itemForm.text = it.text;
      itemForm.type = it.type || ''; itemForm.bot_id = it.bot_id || '';
      modal.value = 'item';
    }
    async function persistContent(content, okMsg) {
      const d = await api('/api/knowledge/' + current.value.id, {
        method: 'PUT', body: JSON.stringify({ content: content })
      });
      current.value = d;
      notify(okMsg); loadKbs();
    }
    async function saveItem() {
      if (!itemForm.text.trim()) { notify('内容不能为空', 'error'); return; }
      const isAdd = itemForm._idx === null;
      const payload = { text: itemForm.text.trim(), type: itemForm.type, bot_id: itemForm.bot_id.trim() };
      const content = buildContent(isAdd ? 'add' : 'update', payload, itemForm._idx);
      if (content === null) { notify('记录不存在，请刷新后重试', 'error'); return; }
      try {
        await persistContent(content, isAdd ? '已新增记录' : '已保存记录');
        modal.value = '';
      } catch (e) { notify(e.message, 'error'); }
    }
    async function removeItem(it) {
      if (!confirm('确认删除该条知识记录？\n\n' + (it.text || '').slice(0, 80))) return;
      const content = buildContent('delete', null, it._idx);
      if (content === null) { notify('记录不存在，请刷新后重试', 'error'); return; }
      try {
        await persistContent(content, '已删除记录');
      } catch (e) { notify(e.message, 'error'); }
    }

    async function removeKb() {
      if (!confirm('确认删除该知识库及其全部记录？')) return;
      try {
        await api('/api/knowledge/' + current.value.id, { method: 'DELETE' });
        resetDetail(); notify('已删除'); loadKbs();
      } catch (e) { notify(e.message, 'error'); }
    }

    // ============ 流程树 ============
    async function loadFlows() {
      const p = scopeParams();
      if (botId.value) p.set('bot_id', botId.value);
      try {
        const data = await api('/api/flow_trees?' + p.toString());
        flows.value = data.items || []; fTotal.value = data.total || 0;
      } catch (e) { notify(e.message, 'error'); }
    }
    async function selectFlow(f) {
      editing.value = false; current.value = f; flowRecords.value = [];
      try {
        const data = await api('/api/flow_trees/' + f.id + '/records?bot_id=' + encodeURIComponent(botId.value));
        flowRecords.value = data.items || [];
      } catch (e) { notify(e.message, 'error'); }
    }
    async function removeFlow() {
      if (!confirm('确认删除该流程树库及其记录？')) return;
      try {
        await api('/api/flow_trees/' + current.value.id, { method: 'DELETE' });
        resetDetail(); flowRecords.value = []; notify('已删除'); loadFlows();
      } catch (e) { notify(e.message, 'error'); }
    }
    function openEditRecord(r) {
      editRecord.id = r.id; editRecord.file_name = r.file_name;
      editRecord.description = r.description || ''; editRecord.bot_id = r.bot_id || botId.value;
      modal.value = 'record';
    }
    async function saveRecord() {
      try {
        await api('/api/flow_records/' + editRecord.id, {
          method: 'PUT',
          body: JSON.stringify({ description: editRecord.description, bot_id: editRecord.bot_id })
        });
        modal.value = ''; notify('保存成功');
        if (current.value) selectFlow(current.value);
      } catch (e) { notify(e.message, 'error'); }
    }

    onMounted(boot);

    return {
      botId, ready, needLogin, logining, loginError, loginForm, doLogin,
      user, robot, scope, meta, roleText, modules, currentModule, tab, switchTab,
      label, modal, editing, current, form, toast, cancelEdit,
      prompts, pTotal, pQuery, pPages, loadPrompts, reloadPrompts, selectPrompt,
      startPromptEdit, savePrompt, removePrompt,
      versions, viewVersion, previewVersion, exitVersionView, doRollback,
      kbs, kTotal, kbItems, filteredItems, iQuery, kbCount, loadKbs, selectKb, removeKb,
      itemForm, openCreateItem, openEditItem, saveItem, removeItem,
      flows, fTotal, flowRecords, loadFlows, selectFlow, removeFlow,
      openEditRecord, saveRecord, editRecord
    };
  }
}).mount('#app');
