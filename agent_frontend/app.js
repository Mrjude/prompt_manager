// Agent 前端 - Vue3 应用
const {createApp,ref,reactive,onMounted,nextTick,computed}=Vue;
const API='';
createApp({
  setup(){
    const mode=ref('flow'),llmOk=ref(false),llmModel=ref('');
    const depts=ref([]),plats=ref([]),fDept=ref(''),fPlat=ref('');
    const searchKw=ref(''),searchTimer=ref(null);
    const toast=reactive({show:false,msg:'',type:'info'}),ftPT=ref(false);

    // 图片列表相关
    const recList=ref([]),recTotal=ref(0),selRec=ref(null);
    const recPg=ref(1),recPS=12;

    // 上传相关
    const upShow=ref(false),upFile=ref(null);
    const upFm=reactive({department:'general',platform:'general'});

    // 编辑描述
    const edId=ref(-1),edDesc=ref('');

    // chat
    const cMsgs=ref([]),cIn=ref(''),cLoad=ref(false);

    function ST(m,t='info'){toast.msg=m;toast.type=t;toast.show=true;if(!ftPT.value)setTimeout(()=>toast.show=false,2500);}
    async function api(p,o={}){try{const r=await fetch(API+p,{headers:{'Content-Type':'application/json'},...o});if(!r.ok)throw new Error(await r.text());return await r.json();}catch(e){ST('请求失败: '+e.message,'error');return null;}}
    function dL(k){return depts.value.find(d=>d.key===k)?.label||k;}
    function pL(k){return plats.value.find(p=>p.key===k)?.label||k;}

    async function loadMeta(){const[d,p]=await Promise.all([api('/api/meta/departments'),api('/api/meta/platforms')]);if(d)depts.value=d.departments;if(p)plats.value=p.platforms;}
    async function chkLLM(){const r=await api('/api/llm/status');if(r){llmOk.value=r.configured;llmModel.value=r.model_name||'';}}
    async function testLLM(){ST('测试中...','info');try{const r=await fetch(API+'/api/llm/test',{method:'POST',headers:{'Content-Type':'application/json'}});if(r.ok){ST('成功: '+(await r.json()).response,'success');}else ST('失败: '+await r.text(),'error');}catch(e){ST('失败: '+e.message,'error');}}

    function swM(m){mode.value=m;}

    // ========== 图片列表搜索 ==========
    function doSearch(){
      clearTimeout(searchTimer.value);
      searchTimer.value=setTimeout(async()=>{
        const p=new URLSearchParams();
        if(fDept.value)p.set('department',fDept.value);
        if(fPlat.value)p.set('platform',fPlat.value);
        if(searchKw.value.trim())p.set('keyword',searchKw.value.trim());
        const r=await api('/api/flow_records/search?'+p.toString());
        if(r){recList.value=r.items;recTotal.value=r.total;recPg.value=1;}
      },300);
    }

    function selRecord(r){
      selRec.value=r;
      edId.value=-1;
      edDesc.value='';
    }

    // ========== 上传 ==========
    function upFC(e){upFile.value=e.target.files[0]||null;}
    function openUpload(){upShow.value=true;upFile.value=null;}
    async function doUpload(){
      if(!upFile.value)return;
      // 先查找或创建流程树库
      const p=new URLSearchParams();
      if(upFm.department)p.set('department',upFm.department);
      if(upFm.platform)p.set('platform',upFm.platform);
      let ftRes=await api('/api/flow_trees?'+p.toString());
      let flowId=null;
      if(ftRes&&ftRes.items&&ftRes.items.length>0){
        flowId=ftRes.items[0].id;
      }else{
        const cr=await api('/api/flow_trees',{method:'POST',body:JSON.stringify({department:upFm.department,platform:upFm.platform})});
        if(cr)flowId=cr.id;
      }
      if(!flowId){ST('创建流程树库失败','error');return;}

      const fd=new FormData();
      fd.append('file',upFile.value);
      fd.append('auto_parse','true');
      upShow.value=false;
      upFile.value=null;
      try{
        const r=await fetch(API+'/api/flow_trees/'+flowId+'/records/upload',{method:'POST',body:fd});
        if(!r.ok)throw new Error(await r.text());
        const d=await r.json();
        ftPT.value=true;
        toast.msg='正在解析流程树...';
        toast.type='success';
        toast.show=true;
        poll(d.id);
      }catch(e){ST('上传失败: '+e.message,'error');}
    }

    async function poll(rid){
      const p=async()=>{
        try{
          const r=await fetch(API+'/api/flow_records/'+rid);
          if(!r.ok){ftPT.value=false;toast.show=false;return;}
          const rc=await r.json();
          if(rc.status==='parsing'){
            ftPT.value=true;toast.msg='正在解析流程树...';toast.type='success';toast.show=true;
            setTimeout(p,2000);
          }else{
            ftPT.value=false;
            toast.msg=rc.status==='success'?'解析完成':'解析失败：'+(rc.error||'');
            toast.type=rc.status==='success'?'success':'error';
            toast.show=true;
            setTimeout(()=>toast.show=false,2500);
            // 上传成功后刷新左侧列表
            doSearch();
          }
        }catch(e){ftPT.value=false;toast.show=false;}
      };
      setTimeout(p,2000);
    }

    async function delRecord(r){
      if(!confirm('确定删除记录 '+r.file_name+'？'))return;
      if(await api('/api/flow_records/'+r.id,{method:'DELETE'})){
        ST('已删除','success');
        if(selRec.value&&selRec.value.id===r.id)selRec.value=null;
        doSearch();
      }
    }

    async function reparse(r){
      if(!confirm('重新解析？'))return;
      ftPT.value=true;
      toast.msg='正在重新解析...';toast.type='success';toast.show=true;
      try{
        const s=await fetch(API+'/api/flow_records/'+r.id+'/reparse',{method:'POST'});
        if(!s.ok)throw new Error(await s.text());
        poll(r.id);
      }catch(e){ftPT.value=false;ST('重新解析失败: '+e.message,'error');}
    }

    function startEdit(r){edId.value=r.id;edDesc.value=r.description||'';}
    function cancelEdit(){edId.value=-1;edDesc.value='';}
    async function saveEdit(r){
      if(await api('/api/flow_records/'+r.id,{method:'PUT',body:JSON.stringify({description:edDesc.value})})){
        ST('已保存','success');
        edId.value=-1;edDesc.value='';
        doSearch();
        if(selRec.value&&selRec.value.id===r.id){
          selRec.value.description=edDesc.value;
        }
      }
    }

    const recTP=computed(()=>Math.ceil(recList.value.length/recPS)||1);
    const recPage=computed(()=>recList.value.slice((recPg.value-1)*recPS,(recPg.value-1)*recPS+recPS));

    // chat
    async function cSend(){
      const t=cIn.value.trim();
      if(!t||cLoad.value)return;
      cMsgs.value.push({role:'user',content:t});cIn.value='';cLoad.value=true;
      try{
        const r=await fetch(API+'/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:cMsgs.value.map(m=>({role:m.role,content:m.content}))})});
        if(!r.ok)throw new Error(await r.text());
        cMsgs.value.push({role:'assistant',content:(await r.json()).content});
      }catch(e){cMsgs.value.push({role:'assistant',content:'错误: '+e.message});}
      cLoad.value=false;
      await nextTick();
      const el=document.querySelector('.chat-messages');
      if(el)el.scrollTop=el.scrollHeight;
    }

    function isImage(r){return r.file_type==='image';}
    function isPdf(r){return r.file_type==='pdf';}

    onMounted(()=>{loadMeta();chkLLM();});
    return{
      mode,llmOk,llmModel,depts,plats,fDept,fPlat,searchKw,
      toast,ftPT,recList,recTotal,selRec,recPg,recPS,recTP,recPage,
      upShow,upFile,upFm,edId,edDesc,
      cMsgs,cIn,cLoad,
      dL,pL,swM,testLLM,doSearch,selRecord,openUpload,upFC,doUpload,
      delRecord,reparse,startEdit,cancelEdit,saveEdit,
      cSend,isImage,isPdf
    };
  },
  template:`
<div>
<div class="header"><h1>Agent 服务</h1><div class="header-actions"><div class="llm-status"><span :class="['status-dot',llmOk?'on':'off']"></span><span>{{llmOk?'LLM 已连接':'LLM 未配置'}}</span><span v-if="llmModel" style="opacity:.7;margin-left:4px">({{llmModel}})</span><button class="btn btn-sm" style="background:rgba(255,255,255,.15);color:#fff;border:none" @click="testLLM">测试</button></div><button class="icon-btn" title="返回主服务" onclick="location.href='http://'+location.hostname+':8900'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg></button></div></div>

<div class="container"><div class="toolbar">
  <div class="filter-group">
    <div class="select-wrap"><select v-model="fDept" @change="selRec=null;doSearch()"><option value="">全部科室</option><option v-for="d in depts" :key="d.key" :value="d.key">{{d.label}}</option></select></div>
    <div class="select-wrap"><select v-model="fPlat" @change="selRec=null;doSearch()"><option value="">全部平台</option><option v-for="p in plats" :key="p.key" :value="p.key">{{p.label}}</option></select></div>
  </div>
  <div class="search-box">
    <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input v-model="searchKw" @input="selRec=null;doSearch()" placeholder="搜索流程树..." class="search-input">
  </div>
  <div class="spacer"></div>
  <button class="btn btn-success" style="margin-right:8px" @click="openUpload">+ 上传流程树</button>
  <button class="btn btn-toggle" :class="{active:mode==='flow'}" @click="swM('flow')">流程树解析</button>
  <button class="btn btn-toggle" :class="{active:mode==='chat'}" @click="swM('chat')">LLM 对话</button>
</div>

<!-- 上传弹窗 -->
<div v-if="upShow" class="upload-overlay" @click.self="upShow=false">
  <div class="upload-dialog">
    <div class="upload-dialog-header"><span>上传流程树图片/PDF</span><button class="btn btn-ghost btn-sm" @click="upShow=false">&#10005;</button></div>
    <div class="upload-dialog-body">
      <div class="form-row">
        <div class="form-group"><label>科室</label><select v-model="upFm.department"><option v-for="d in depts" :key="d.key" :value="d.key">{{d.label}}</option></select></div>
        <div class="form-group"><label>平台</label><select v-model="upFm.platform"><option v-for="p in plats" :key="p.key" :value="p.key">{{p.label}}</option></select></div>
      </div>
      <div class="form-group"><label>选择文件（图片/PDF）</label><input type="file" @change="upFC" accept="image/*,.pdf"></div>
      <div class="actions-bar"><button class="btn btn-ghost" @click="upShow=false">取消</button><button class="btn btn-primary" :disabled="!upFile" @click="doUpload">上传并解析</button></div>
    </div>
  </div>
</div>

<div class="main-grid">
  <!-- 左侧：流程树图片列表 -->
  <div class="card"><div class="card-header"><div class="title">流程树图库 <span v-if="recTotal">共 {{recTotal}} 条</span></div></div>
    <div class="card-body" style="padding:8px">
      <div v-if="!recList.length && (fDept||fPlat||searchKw.trim())" class="empty-state" style="padding:40px 20px">暂无匹配的流程树图片</div>
      <div v-if="!recList.length && !fDept&&!fPlat&&!searchKw.trim()" class="empty-state" style="padding:40px 20px">选择科室/平台或搜索关键词查看</div>

      <div class="thumb-grid" v-if="recPage.length">
        <div v-for="r in recPage" :key="r.id" class="thumb-item" :class="{active:selRec&&selRec.id===r.id}" @click="selRecord(r)">
          <div class="thumb-img-wrap">
            <img v-if="isImage(r)&&r.file_url" :src="r.file_url" :alt="r.file_name" class="thumb-img" @error="$event.target.style.display='none';$event.target.nextElementSibling.style.display='flex'">
            <div class="thumb-placeholder" :style="isImage(r)&&r.file_url?'display:none':'display:flex'">
              <span v-if="isPdf(r)" style="font-size:28px">&#128196;</span>
              <span v-else style="font-size:28px">&#128247;</span>
            </div>
          </div>
          <div class="thumb-info">
            <div class="thumb-name" :title="r.file_name">{{r.file_name}}</div>
            <div class="thumb-meta">
              <span class="tag tag-dept">{{dL(r.department)}}</span>
              <span class="tag tag-plat">{{pL(r.platform)}}</span>
              <span :class="['badge',r.status==='success'?'badge-green':'badge-gray']" :style="r.status==='failed'?'background:#fff2f0;color:#ff4d4f':(r.status==='parsing'?'background:#e8f0fe;color:#4a90d9':(r.status==='pending'?'background:#fffbe6;color:#faad14':''))">{{r.status==='success'?'成功':(r.status==='failed'?'失败':(r.status==='parsing'?'解析中':'待解析'))}}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="recTP>1" class="pagination-bar">
        <button class="btn btn-ghost btn-sm" :disabled="recPg===1" @click="recPg--;selRec=null">上一页</button>
        <span>{{recPg}} / {{recTP}}</span>
        <button class="btn btn-ghost btn-sm" :disabled="recPg===recTP" @click="recPg++;selRec=null">下一页</button>
      </div>
    </div>
  </div>

  <!-- 右侧：详情 / 对话 -->
  <div class="card">
    <template v-if="mode==='flow'">
      <div class="card-header"><div class="title">{{selRec?'流程树详情':'请选择一条记录'}}</div>
        <div v-if="selRec" style="display:flex;gap:6px">
          <button class="btn btn-outline btn-sm" @click="startEdit(selRec)">编辑描述</button>
          <button class="btn btn-primary btn-sm" v-if="selRec.status!=='success'&&selRec.status!=='parsing'" @click="reparse(selRec)">重新解析</button>
          <button class="btn btn-danger btn-sm" @click="delRecord(selRec)">删除</button>
        </div>
      </div>
      <div class="card-body">
        <div v-if="!selRec" class="empty-state">
          <div style="margin-bottom:8px">请从左侧图库选择一条记录查看详情</div>
          <div style="font-size:12px;color:#aaa">或点击"+ 上传流程树"上传新的图片/PDF</div>
        </div>
        <div v-if="selRec">
          <div class="detail-meta">
            <div><strong>文件名：</strong>{{selRec.file_name}}</div>
            <div><strong>科室：</strong>{{dL(selRec.department)}}</div>
            <div><strong>平台：</strong>{{pL(selRec.platform)}}</div>
            <div><strong>类型：</strong>{{selRec.file_type}}</div>
            <div><strong>状态：</strong>
              <span :class="['badge',selRec.status==='success'?'badge-green':'badge-gray']" :style="selRec.status==='failed'?'background:#fff2f0;color:#ff4d4f':(selRec.status==='parsing'?'background:#e8f0fe;color:#4a90d9':'')">{{selRec.status==='success'?'解析成功':(selRec.status==='failed'?'解析失败':(selRec.status==='parsing'?'解析中...':'待解析'))}}</span>
            </div>
            <div><strong>上传时间：</strong>{{selRec.created_at}}</div>
          </div>
          <div v-if="edId===selRec.id" style="margin-bottom:14px">
            <div style="margin-bottom:6px;font-size:12px;color:#666">编辑自然语言描述：</div>
            <textarea style="width:100%;padding:10px 14px;border:1px solid #4a90d9;border-radius:8px;font-size:13px;outline:none;background:#1a1a1a;color:#f0f0f0;line-height:1.7;min-height:120px;resize:vertical" v-model="edDesc"></textarea>
            <div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end"><button class="btn btn-ghost btn-sm" @click="cancelEdit">取消</button><button class="btn btn-primary btn-sm" @click="saveEdit(selRec)">保存</button></div>
          </div>
          <div class="desc-section">
            <div class="section-title">自然语言描述</div>
            <div v-if="selRec.description" class="desc-content">{{selRec.description}}</div>
            <div v-else class="desc-empty">暂无描述</div>
          </div>
          <div v-if="selRec.error" class="error-section">
            <div class="section-title" style="border-left-color:#ff4d4f">错误信息</div>
            <div class="desc-content" style="color:#ff4d4f;background:#fff2f0">{{selRec.error}}</div>
          </div>
          <div v-if="selRec.file_url&&isImage(selRec)" class="preview-section">
            <div class="section-title">图片预览</div>
            <img :src="selRec.file_url" style="max-width:100%;max-height:400px;border-radius:6px;border:1px solid #e8ecf0">
          </div>
        </div>
      </div>
    </template>

    <template v-if="mode==='chat'">
      <div class="card-header"><div class="title">LLM 对话</div><button class="btn btn-ghost btn-sm" @click="cMsgs=[]">清空</button></div>
      <div class="card-body"><div class="chat-container"><div class="chat-messages"><div v-for="(m,i) in cMsgs" :key="i" class="chat-msg" :class="{user:m.role==='user',assistant:m.role==='assistant'}"><div class="bubble">{{m.content}}</div></div><div v-if="cLoad" style="text-align:center;padding:12px;color:#4a90d9">思考中...</div></div><div class="chat-input-area"><textarea v-model="cIn" placeholder="输入消息，Enter 发送" @keydown.enter.exact.prevent="cSend"></textarea><button class="btn btn-primary" @click="cSend" :disabled="cLoad">发送</button></div></div></div>
    </template>
  </div>
</div></div>
<div v-if="toast.show" :class="['toast','toast-'+toast.type,ftPT?'toast-parsing':'']">{{toast.msg}}</div>
</div>`
}).mount('#app');
