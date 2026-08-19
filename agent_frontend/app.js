// Agent 前端 - Vue3 应用
const {createApp,ref,reactive,onMounted,onUnmounted,nextTick,computed}=Vue;
const API='';
createApp({
  setup(){
    const mode=ref('flow'),llmOk=ref(false),llmModel=ref(''),llmProvider=ref('openai');
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

    // 机器人配置（科室 -> 平台 -> 机器人id 三级级联）
    const bots=ref([]),fBot=ref(''),botCfg=ref(null),cfgLoad=ref(false);
    const cSession=ref(''),cUseTools=ref(true),cThink=ref(false);

    // 对话列表（LLM 对话页左侧边栏）
    const convList=ref([]),convTotal=ref(0),convPg=ref(1),convPS=ref(30);
    const convBotKw=ref(''),convKw=ref(''),convLoad=ref(false);
    const convTP=computed(()=>Math.ceil(convTotal.value/convPS.value)||1);

    function ST(m,t='info'){toast.msg=m;toast.type=t;toast.show=true;if(!ftPT.value)setTimeout(()=>toast.show=false,2500);}
    async function api(p,o={}){try{const r=await fetch(API+p,{headers:{'Content-Type':'application/json'},...o});if(!r.ok)throw new Error(await r.text());return await r.json();}catch(e){ST('请求失败: '+e.message,'error');return null;}}
    function dL(k){return depts.value.find(d=>d.key===k)?.label||k;}
    function pL(k){return plats.value.find(p=>p.key===k)?.label||k;}

    async function loadMeta(){const[d,p]=await Promise.all([api('/api/meta/departments'),api('/api/meta/platforms')]);if(d)depts.value=d.departments;if(p)plats.value=p.platforms;}
    async function chkLLM(){const r=await api('/api/llm/status');if(r){llmOk.value=r.configured;llmModel.value=r.model_name||'';llmProvider.value=r.provider||'openai';}}
    async function testLLM(){ST('测试中...','info');try{const r=await fetch(API+'/api/llm/test',{method:'POST',headers:{'Content-Type':'application/json'}});if(r.ok){ST('成功: '+(await r.json()).response,'success');}else ST('失败: '+await r.text(),'error');}catch(e){ST('失败: '+e.message,'error');}}

    // ========== LLM API 版本管理 ==========
    const llmCfgShow=ref(false),llmVers=ref([]),llmLoad=ref(false);
    const llmForm=ref(null),llmSaving=ref(false);

    async function openLlmCfg(){llmCfgShow.value=true;llmForm.value=null;await loadLlmVers();}

    async function loadLlmVers(){
      llmLoad.value=true;
      const r=await api('/api/llm/versions');
      llmVers.value=(r&&r.versions)||[];
      llmLoad.value=false;
    }

    function newLlmVer(){
      llmForm.value={id:0,name:'',base_url:'',api_key:'',model_name:'',provider:'openai'};
    }

    function editLlmVer(v){
      // api_key 上游已掩码，留空表示不修改
      llmForm.value={id:v.id,name:v.name||'',base_url:v.base_url||'',
                     api_key:'',model_name:v.model_name||'',provider:v.provider||'openai'};
    }

    async function saveLlmVer(){
      const f=llmForm.value;
      if(!f)return;
      if(!f.base_url.trim()){ST('Base URL 不能为空','error');return;}
      llmSaving.value=true;
      const body={name:f.name,base_url:f.base_url.trim(),model_name:f.model_name,provider:f.provider};
      // 编辑时 api_key 留空则不覆盖原值
      if(f.api_key.trim()||!f.id)body.api_key=f.api_key.trim();
      const r=f.id
        ? await api('/api/llm/versions/'+f.id,{method:'PUT',body:JSON.stringify(body)})
        : await api('/api/llm/versions',{method:'POST',body:JSON.stringify(body)});
      llmSaving.value=false;
      if(r){ST(f.id?'版本已更新':'版本已创建','success');llmForm.value=null;await loadLlmVers();await chkLLM();}
    }

    async function activateLlmVer(v){
      const r=await api('/api/llm/versions/'+v.id+'/activate',{method:'POST'});
      if(r){
        ST(`已切换到「${r.name||v.name}」`,'success');
        await loadLlmVers();
        await chkLLM();
      }
    }

    async function delLlmVer(v){
      if(!confirm(`确定删除版本「${v.name||'未命名'}」？`))return;
      const r=await api('/api/llm/versions/'+v.id,{method:'DELETE'});
      if(r){ST('版本已删除','success');await loadLlmVers();}
    }

    function swM(m){
      mode.value=m;
      // 切到对话页时加载对话列表；切走时清掉列表检索词，避免影响流程树搜索框
      if(m==='chat'){convPg.value=1;loadSessions();}
    }

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

    // ========== 机器人 id 级联 ==========
    // 机器人配置指纹，用于检测提示词管理侧的配置变更
    const botSig=ref(''),botSyncAt=ref('');

    // 科室/平台变化 -> 重新拉取可选机器人
    // keepSelected=true 时不清空当前选中项（用于反向回填场景，此时 fBot 是可信的）
    async function loadBots(keepSelected){
      const p=new URLSearchParams();
      if(fDept.value)p.set('department',fDept.value);
      if(fPlat.value)p.set('platform',fPlat.value);
      const r=await api('/api/agent/bots?'+p.toString());
      if(!r)return;
      bots.value=r.bots||[];
      botSig.value=r.signature||'';
      botSyncAt.value=new Date().toLocaleTimeString('zh-CN',{hour12:false});
      if(!keepSelected&&fBot.value&&!bots.value.some(b=>b.value===fBot.value)){
        fBot.value='';botCfg.value=null;
      }
    }

    // 选中机器人 -> 反向回填科室/平台（保证三者一致）
    async function onBotChange(){
      const bot=bots.value.find(b=>b.value===fBot.value);
      if(bot&&bot.configured!==false){
        const changed=(bot.department&&fDept.value!==bot.department)
                    ||(bot.platform&&fPlat.value!==bot.platform);
        if(changed){
          if(bot.department)fDept.value=bot.department;
          if(bot.platform)fPlat.value=bot.platform;
          // keepSelected：回填后的候选集必然包含该机器人，不能被清空逻辑误清
          await loadBots(true);
          doSearch();
          if(mode.value==='chat'){convPg.value=1;loadSessions();}
        }
      }
      cSession.value='';   // 切换机器人后重置会话，避免沿用旧人格上下文
      cMsgs.value=[];
      await loadBotConfig();
    }

    // 手动刷新机器人配置（提示词管理侧改动后立即同步）
    async function syncBots(){
      await loadBots(true);
      await loadBotConfig();
      ST(`已同步 ${bots.value.length} 个机器人配置`,'success');
    }

    // 轮询检测配置变更：指纹变化才真正重拉列表，成本极低
    let botSigTimer=null;
    async function checkBotSignature(){
      const r=await api('/api/agent/bots/signature');
      if(r&&r.signature&&botSig.value&&r.signature!==botSig.value){
        await loadBots(true);
        ST('机器人配置已更新','success');
      }
    }

    // 上级筛选变化的统一入口
    async function onFilterChange(){
      selRec.value=null;
      doSearch();
      await loadBots();
      await loadBotConfig();
      cSession.value='';
      if(mode.value==='chat'){convPg.value=1;loadSessions();}
    }

    async function loadBotConfig(){
      if(!fBot.value&&!fDept.value&&!fPlat.value){botCfg.value=null;return;}
      cfgLoad.value=true;
      const p=new URLSearchParams();
      if(fBot.value)p.set('bot_id',fBot.value);
      if(fDept.value)p.set('department',fDept.value);
      if(fPlat.value)p.set('platform',fPlat.value);
      const r=await api('/api/agent/config?'+p.toString());
      botCfg.value=r?{...r.meta,tools:r.tools,preview:r.system_prompt_preview,runtime:r.runtime}:null;
      cfgLoad.value=false;
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

    // ========== 对话列表 ==========
    let convTimer=null;
    function loadSessionsDebounced(){
      clearTimeout(convTimer);
      convTimer=setTimeout(()=>{convPg.value=1;loadSessions();},300);
    }

    async function loadSessions(){
      convLoad.value=true;
      const p=new URLSearchParams({page:convPg.value,page_size:convPS.value});
      if(convBotKw.value.trim())p.set('bot_id',convBotKw.value.trim());
      if(convKw.value.trim())p.set('keyword',convKw.value.trim());
      if(fDept.value)p.set('department',fDept.value);
      if(fPlat.value)p.set('platform',fPlat.value);
      const r=await api('/api/agent/sessions?'+p.toString());
      if(r){convList.value=r.items||[];convTotal.value=r.total||0;}
      convLoad.value=false;
    }

    // 点击对话 -> 恢复历史消息与配置上下文
    async function openConv(s){
      const r=await api('/api/agent/sessions/'+s.session_id);
      if(!r)return;
      cSession.value=s.session_id;
      cMsgs.value=(r.messages||[]).map(m=>({
        role:m.role,content:m.content,segments:m.segments||[],
        tools:m.tools||[],turn:m.turn,thinkOpen:false
      }));
      // 回填该会话的机器人配置，保证继续对话时人格一致
      if(r.department)fDept.value=r.department;
      if(r.platform)fPlat.value=r.platform;
      if(r.bot_id&&r.bot_id!==fBot.value)fBot.value=r.bot_id;
      await loadBots(true);
      await loadBotConfig();
      await nextTick();
      const el=document.querySelector('.chat-messages');
      if(el)el.scrollTop=el.scrollHeight;
    }

    async function delConv(s){
      if(!confirm('确定删除该对话？'))return;
      const r=await api('/api/agent/sessions/'+s.session_id,{method:'DELETE'});
      if(r&&r.success){
        if(cSession.value===s.session_id){cSession.value='';cMsgs.value=[];}
        ST('对话已删除','success');
        loadSessions();
      }
    }

    function newConv(){cSession.value='';cMsgs.value=[];}

    function fmtTime(iso){
      if(!iso)return '';
      const d=new Date(iso),now=new Date();
      const sameDay=d.toDateString()===now.toDateString();
      const p=n=>String(n).padStart(2,'0');
      return sameDay?`${p(d.getHours())}:${p(d.getMinutes())}`
                    :`${d.getMonth()+1}/${d.getDate()}`;
    }

    // chat：走智能客服 Agent 接口（带机器人配置 + 会话记忆 + 工具调用）
    async function cSend(){
      const t=cIn.value.trim();
      if(!t||cLoad.value)return;
      cMsgs.value.push({role:'user',content:t});cIn.value='';cLoad.value=true;
      try{
        const r=await fetch(API+'/api/agent/chat',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            message:t,
            session_id:cSession.value||null,
            bot_id:fBot.value||null,
            department:fDept.value||null,
            platform:fPlat.value||null,
            use_tools:cUseTools.value,
            enable_thinking:cThink.value
          })});
        if(!r.ok)throw new Error(await r.text());
        const d=await r.json();
        const isNew=!cSession.value;
        cSession.value=d.session_id||cSession.value;
        cMsgs.value.push({
          role:'assistant',
          content:d.reply,
          segments:d.segments||[],
          tools:d.tool_calls||[],
          reasoning:d.reasoning||'',
          thinkOpen:false,
          turn:d.turn
        });
        if(d.meta)botCfg.value={...(botCfg.value||{}),...d.meta};
        if(d.error)ST('Agent 异常: '+d.error,'error');
        else loadSessions();   // 刷新左侧列表（新会话置顶 / 更新末条消息）
      }catch(e){cMsgs.value.push({role:'assistant',content:'错误: '+e.message});}
      cLoad.value=false;
      await nextTick();
      const el=document.querySelector('.chat-messages');
      if(el)el.scrollTop=el.scrollHeight;
    }

    async function cClear(){
      if(cSession.value)await api('/api/agent/reset',{method:'POST',body:JSON.stringify({session_id:cSession.value})});
      cMsgs.value=[];cSession.value='';
    }

    function isImage(r){return r.file_type==='image';}
    function isPdf(r){return r.file_type==='pdf';}

    onMounted(()=>{
      loadMeta();chkLLM();loadBots();
      // 每 20 秒比对配置指纹，变更时自动刷新机器人下拉
      botSigTimer=setInterval(checkBotSignature,20000);
      // 页面重新可见时立即同步一次（切回标签页的常见场景）
      document.addEventListener('visibilitychange',()=>{
        if(!document.hidden)checkBotSignature();
      });
    });
    onUnmounted(()=>{clearInterval(botSigTimer);});
    return{
      mode,llmOk,llmModel,llmProvider,depts,plats,fDept,fPlat,searchKw,
      toast,ftPT,recList,recTotal,selRec,recPg,recPS,recTP,recPage,
      upShow,upFile,upFm,edId,edDesc,
      cMsgs,cIn,cLoad,
      bots,fBot,botCfg,cfgLoad,cSession,cUseTools,cThink,botSig,botSyncAt,
      convList,convTotal,convPg,convPS,convTP,convBotKw,convKw,convLoad,
      llmCfgShow,llmVers,llmLoad,llmForm,llmSaving,
      dL,pL,swM,testLLM,doSearch,selRecord,openUpload,upFC,doUpload,
      delRecord,reparse,startEdit,cancelEdit,saveEdit,
      loadBots,onBotChange,onFilterChange,loadBotConfig,syncBots,
      loadSessions,loadSessionsDebounced,openConv,delConv,newConv,fmtTime,
      openLlmCfg,loadLlmVers,newLlmVer,editLlmVer,saveLlmVer,activateLlmVer,delLlmVer,
      cSend,cClear,isImage,isPdf
    };
  },
  template:`
<div>
<div class="header"><h1>Agent 服务</h1><div class="header-actions"><div class="llm-status"><span :class="['status-dot',llmOk?'on':'off']"></span><span>{{llmOk?'LLM 已连接':'LLM 未配置'}}</span><span v-if="llmModel" style="opacity:.7;margin-left:4px">({{llmModel}})</span><span v-if="llmProvider==='vllm'" class="provider-tag" title="本地 vLLM 原生接口">本地</span><button class="btn btn-sm" style="background:rgba(255,255,255,.15);color:#fff;border:none" @click="testLLM">测试</button></div><button class="icon-btn" title="LLM API 配置" @click="openLlmCfg"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button><button class="icon-btn" title="返回主服务" onclick="location.href='http://'+location.hostname+':8900'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg></button></div></div>

<!-- LLM API 版本管理弹窗 -->
<div v-if="llmCfgShow" class="upload-overlay" @click.self="llmCfgShow=false">
  <div class="upload-dialog llm-dialog">
    <div class="upload-dialog-header"><span>LLM API 版本管理</span><button class="btn btn-ghost btn-sm" @click="llmCfgShow=false">&#10005;</button></div>
    <div class="upload-dialog-body">
      <div class="llm-bar">
        <span class="llm-bar-title">已保存的版本</span>
        <button class="btn btn-success btn-sm" @click="newLlmVer">+ 新建版本</button>
      </div>

      <!-- 新建/编辑表单 -->
      <div v-if="llmForm" class="llm-form">
        <div class="llm-form-title">{{llmForm.id?'编辑版本':'新建版本'}}</div>
        <div class="form-row">
          <div class="form-group"><label>版本名称</label><input v-model="llmForm.name" placeholder="如：本地 vLLM"></div>
          <div class="form-group"><label>接口类型</label>
            <select v-model="llmForm.provider">
              <option value="openai">OpenAI 兼容接口</option>
              <option value="vllm">本地 vLLM 原生接口</option>
            </select>
          </div>
        </div>
        <div class="form-group"><label>Base URL</label>
          <input v-model="llmForm.base_url" :placeholder="llmForm.provider==='vllm'?'http://127.0.0.1:8608':'https://dashscope.aliyuncs.com/compatible-mode/v1'">
        </div>
        <div class="form-row">
          <div class="form-group"><label>模型名称</label>
            <input v-model="llmForm.model_name" :placeholder="llmForm.provider==='vllm'?'如 Qwen3-8B（展示用）':'如 deepseek-v4-flash'">
          </div>
          <div class="form-group"><label>API Key{{llmForm.id?'（留空不修改）':''}}</label>
            <input v-model="llmForm.api_key" :placeholder="llmForm.provider==='vllm'?'本地部署通常留空':'sk-...'">
          </div>
        </div>
        <div class="llm-hint" v-if="llmForm.provider==='vllm'">
          本地 vLLM 走 <code>/llm/generate</code> 协议，Base URL 填服务根地址即可（自动补全路径）。该协议不支持工具调用，Agent 会自动降级为纯对话。
        </div>
        <div class="actions-bar">
          <button class="btn btn-ghost btn-sm" @click="llmForm=null">取消</button>
          <button class="btn btn-primary btn-sm" @click="saveLlmVer" :disabled="llmSaving">{{llmSaving?'保存中...':'保存'}}</button>
        </div>
      </div>

      <div v-if="llmLoad" class="empty-state" style="padding:24px">加载中...</div>
      <div v-else-if="!llmVers.length" class="empty-state" style="padding:24px">暂无版本，点击"新建版本"添加</div>
      <div v-else class="llm-list">
        <div v-for="v in llmVers" :key="v.id" class="llm-item" :class="{active:v.is_active}">
          <div class="llm-item-head">
            <span class="llm-name">{{v.name||'未命名'}}</span>
            <span v-if="v.is_active" class="llm-active-tag">● 当前激活</span>
            <span class="llm-prov" :class="v.provider">{{v.provider==='vllm'?'本地 vLLM':'OpenAI 兼容'}}</span>
            <div class="llm-item-btns">
              <button v-if="!v.is_active" class="btn btn-success btn-sm" @click="activateLlmVer(v)">激活</button>
              <button class="btn btn-ghost btn-sm" @click="editLlmVer(v)">编辑</button>
              <button v-if="!v.is_active" class="btn btn-ghost btn-sm llm-del" @click="delLlmVer(v)">删除</button>
            </div>
          </div>
          <div class="llm-item-meta">{{v.base_url||'-'}} ｜ {{v.model_name||'-'}} ｜ Key: {{v.api_key||'(空)'}}</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="container"><div class="toolbar">
  <div class="filter-group">
    <div class="select-wrap"><select v-model="fDept" @change="fPlat='';onFilterChange()"><option value="">全部科室</option><option v-for="d in depts" :key="d.key" :value="d.key">{{d.label}}</option></select></div>
    <div class="select-wrap"><select v-model="fPlat" @change="onFilterChange()"><option value="">全部平台</option><option v-for="p in plats" :key="p.key" :value="p.key">{{p.label}}</option></select></div>
    <div class="select-wrap bot-select" :title="botCfg?('生效提示词: '+(botCfg.prompt_name||'-')+' v'+(botCfg.prompt_version||0)):'选择机器人以加载其对话配置'">
      <select v-model="fBot" @change="onBotChange()">
        <option value="">{{bots.length?'全部机器人 ('+bots.length+')':'暂无机器人配置'}}</option>
        <option v-for="b in bots" :key="b.value" :value="b.value" :disabled="b.enabled===false">{{b.label}}</option>
      </select>
    </div>
    <button class="sync-btn" :title="'与提示词管理同步机器人配置'+(botSyncAt?'（上次同步 '+botSyncAt+'）':'')" @click="syncBots">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
    </button>
    <span v-if="fBot&&botCfg" class="cfg-badge" :class="{warn:botCfg.prompt_source==='fallback'}">
      v{{botCfg.prompt_version||0}}<span v-if="botCfg.prompt_version_locked" title="版本已锁定">锁</span>
      <span v-if="botCfg.flow_record_count">· 流程{{botCfg.flow_record_count}}</span>
      <span v-else title="该科室/平台未配置流程树">· 无流程</span>
    </span>
  </div>
  <div class="search-box">
    <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input v-if="mode==='chat'" v-model="convKw" @input="loadSessionsDebounced()" placeholder="检索对话内容..." class="search-input">
    <input v-else v-model="searchKw" @input="selRec=null;doSearch()" placeholder="搜索流程树..." class="search-input">
  </div>
  <div class="spacer"></div>
  <button class="btn btn-toggle" :class="{active:mode==='flow'}" @click="swM('flow')">流程树管理</button>
  <button class="btn btn-toggle" :class="{active:mode==='chat'}" @click="swM('chat')">Agent对话测试</button>
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
  <!-- 左侧：LLM 对话模式 = 对话列表；其他模式 = 流程树图库 -->
  <div v-if="mode==='chat'" class="card">
    <div class="card-header">
      <div class="title">对话列表 <span v-if="convTotal">共 {{convTotal}} 条</span></div>
      <button class="btn btn-primary btn-sm" @click="newConv">+ 新对话</button>
    </div>
    <div class="conv-filter">
      <input v-model="convBotKw" @input="loadSessionsDebounced()" placeholder="按机器人 id 筛选" class="conv-bot-input">
      <button v-if="convBotKw" class="btn btn-ghost btn-sm" @click="convBotKw='';loadSessions()">清除</button>
    </div>
    <div class="card-body" style="padding:8px">
      <div v-if="convLoad" class="empty-state" style="padding:30px 20px">加载中...</div>
      <div v-else-if="!convList.length" class="empty-state" style="padding:40px 20px">
        {{convBotKw||convKw?'没有匹配的对话':'暂无对话记录，发送消息后自动保存'}}
      </div>
      <div v-else class="conv-list">
        <div v-for="s in convList" :key="s.session_id" class="conv-item"
             :class="{active:cSession===s.session_id}" @click="openConv(s)">
          <div class="conv-item-top">
            <span class="conv-title" :title="s.title">{{s.title||'新对话'}}</span>
            <button class="conv-del" title="删除对话" @click.stop="delConv(s)">&#10005;</button>
          </div>
          <div class="conv-item-last" :title="s.last_message">{{s.last_message}}</div>
          <div class="conv-item-meta">
            <span v-if="s.bot_id" class="conv-tag bot">{{s.bot_id}}</span>
            <span v-if="s.department_zh" class="conv-tag">{{s.department_zh}}</span>
            <span v-if="s.platform_zh" class="conv-tag">{{s.platform_zh}}</span>
            <span class="conv-tag turns">{{s.turns}}轮</span>
            <span class="conv-time">{{fmtTime(s.updated_at)}}</span>
          </div>
        </div>
      </div>
      <div v-if="convTP>1" class="pager">
        <button class="btn btn-ghost btn-sm" :disabled="convPg<=1" @click="convPg--;loadSessions()">上一页</button>
        <span style="font-size:12px;color:#5a6b7d">{{convPg}} / {{convTP}}</span>
        <button class="btn btn-ghost btn-sm" :disabled="convPg>=convTP" @click="convPg++;loadSessions()">下一页</button>
      </div>
    </div>
  </div>

  <!-- 左侧：流程树图片列表 -->
  <div v-else class="card"><div class="card-header"><div class="title">流程树图库 <span v-if="recTotal">共 {{recTotal}} 条</span></div><button class="btn btn-success btn-sm" @click="openUpload">+ 上传流程树</button></div>
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
      <div class="card-header">
        <div class="title">智能客服 Agent 对话
          <span v-if="fBot" style="font-weight:400;opacity:.75">· 机器人 {{fBot}}</span>
          <span v-else style="font-weight:400;opacity:.6">· 未选机器人（按科室/平台默认配置）</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <label class="tool-toggle" title="仅控制 function calling（工具调用）。关闭后 Agent 仍会使用提示词模板与知识库生成话术，只是不再主动调用工具做检索"><input type="checkbox" v-model="cUseTools">工具调用</label>
          <label class="switch-wrap" :title="cThink?'已开启模型思考（推理模式）':'已关闭模型思考'">
            <span class="switch-label">思考</span>
            <span class="switch" :class="{on:cThink}" @click="cThink=!cThink"><span class="switch-dot"></span></span>
          </label>
          <button class="btn btn-ghost btn-sm" @click="cClear">清空</button>
        </div>
      </div>
      <div v-if="botCfg" class="cfg-bar cfg-bar-fixed">
        <span>{{botCfg.department_zh||'-'}} / {{botCfg.platform_zh||'-'}}</span>
        <span v-if="botCfg.company">· {{botCfg.company}}</span>
        <span>· 提示词 {{botCfg.prompt_name||'内置兜底'}} v{{botCfg.prompt_version||0}}</span>
        <span>· 流程片段 {{botCfg.flow_record_count||0}}</span>
        <span :title="(botCfg.kb_types||[]).join('、')||'该科室/平台知识库为空'">· 知识条目 {{botCfg.kb_injected||0}}</span>
        <span v-if="botCfg.leftover_placeholders&&botCfg.leftover_placeholders.length" style="color:#faad14" :title="'模板中未绑定的变量：'+botCfg.leftover_placeholders.join('、')">· 未绑定 {{botCfg.leftover_placeholders.length}}</span>
        <span v-if="botCfg.flow_knowledge_truncated" style="color:#faad14">· 知识已截断</span>
        <span v-if="cSession" style="opacity:.6">· 会话 {{cSession.slice(0,8)}}</span>
      </div>
      <div class="card-body chat-body">
        <div class="chat-messages">
          <div v-for="(m,i) in cMsgs" :key="i" class="chat-msg" :class="{user:m.role==='user',assistant:m.role==='assistant'}">
            <div v-if="m.reasoning" class="think-box">
              <div class="think-head" @click="m.thinkOpen=!m.thinkOpen">
                <span>模型思考 ({{m.reasoning.length}} 字)</span>
                <span>{{m.thinkOpen?'收起':'展开'}}</span>
              </div>
              <div v-if="m.thinkOpen" class="think-body">{{m.reasoning}}</div>
            </div>
            <div v-if="m.tools&&m.tools.length" class="tool-trace">
              <div v-for="(t,j) in m.tools" :key="j" class="tool-item" :title="t.result_preview">
                <b>{{t.name}}</b> <span style="opacity:.7">{{t.arguments}}</span> <em>{{t.elapsed_ms}}ms</em>
              </div>
            </div>
            <template v-if="m.segments&&m.segments.length>1">
              <div v-for="(s,k) in m.segments" :key="k" class="bubble">{{s}}</div>
            </template>
            <div v-else class="bubble">{{m.content}}</div>
          </div>
          <div v-if="cLoad" style="text-align:center;padding:12px;color:#4a90d9">思考中...</div>
        </div>
      </div>
      <div class="chat-input-area chat-input-fixed">
        <textarea v-model="cIn" placeholder="输入消息，Enter 发送" @keydown.enter.exact.prevent="cSend"></textarea>
        <button class="btn btn-primary" @click="cSend" :disabled="cLoad">发送</button>
      </div>
    </template>
  </div>
</div></div>
<div v-if="toast.show" :class="['toast','toast-'+toast.type,ftPT?'toast-parsing':'']">{{toast.msg}}</div>
</div>`
}).mount('#app');
