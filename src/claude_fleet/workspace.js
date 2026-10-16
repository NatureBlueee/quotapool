(() => {
  const names={reminder:'定时提醒',digest:'运行日报',services:'服务监控',disk:'磁盘监控'};
  const statuses={success:'执行成功',failed:'执行失败',unchanged:'状态无变化',preview:'试运行',running:'执行中',interrupted:'执行中断'};
  let state={jobs:[],runs:[]}, busy=false, editing=null;
  VIEW_HASH.automations='automations-section';
  const nav=document.createElement('a');nav.href='#automations-section';nav.dataset.nav='automations';nav.innerHTML='<span class="nav-icon">09</span>自动化中心';nav.onclick=e=>{e.preventDefault();activateView('automations');refresh()};
  document.querySelector('.nav').insertBefore(nav,document.querySelector('[data-nav="lark"]'));
  document.querySelectorAll('.nav .nav-icon').forEach((node,i)=>node.textContent=String(i+1).padStart(2,'0'));
  const myself=document.createElement('button');myself.type='button';myself.className='button';myself.textContent='＋ 添加我自己';
  document.querySelector('.recipient-search').append(myself);
  myself.onclick=()=>{const user=latest?.dashboard?.lark?.user;if(!user?.openId){notice('请先以用户身份连接飞书，并刷新连接状态。',true);return}const key='user:'+user.openId;if(!larkDraft.some(r=>r.key===key)){larkDraft.push({key,type:'user',id:user.openId,label:user.name||'我自己',detail:'当前已绑定的飞书用户',default:true});larkDirty=true;renderLarkRecipientDraft();notice('已添加你自己，请保存分享设置。')}else notice('你已在分享对象中。')};
  document.querySelector('.brand small').textContent='你的本地工作助手';
  const section=document.createElement('section');section.dataset.view='automations';section.id='automations-section';section.hidden=true;
  section.innerHTML=`<div class="workspace-heading"><div><span class="workspace-kicker">AUTOMATIONS</span><h2>让日常检查自动发生</h2><p>定时执行、持续观察，在需要你关注时送达飞书。</p></div><button class="button primary" id="automation-new">＋ 创建自动化</button></div>
  <div class="automation-stats"><div class="automation-stat"><span>正在启用</span><strong id="automation-enabled">—</strong></div><div class="automation-stat"><span>最近记录中的失败</span><strong id="automation-failed">—</strong></div><div class="automation-stat"><span>下次执行 · 本机时间</span><strong id="automation-next" style="font-size:18px">—</strong></div></div>
  <div class="automation-templates">${Object.entries(names).map(([kind,name])=>`<button class="automation-template" data-template="${kind}"><strong>${name} ↗</strong><span>${{reminder:'把待办或提醒按时发给自己',digest:'每天汇总服务和磁盘状况',services:'服务异常与恢复时通知',disk:'可用空间低于阈值时提醒'}[kind]}</span></button>`).join('')}</div>
  <div class="workspace-context"><p id="automation-engine">正在连接自动化服务…</p><button class="button" id="automation-lark">配置飞书收件人 ↗</button></div>
  <div class="automation-toolbar"><h3>我的自动化</h3><input type="search" id="automation-search" aria-label="搜索自动化" placeholder="搜索任务名称…"></div><div id="automation-list" class="automation-list"></div>
  <div class="automation-history"><div class="automation-toolbar"><h3>运行记录</h3><button class="button" id="automation-refresh">刷新记录</button></div><div id="automation-runs"></div></div>`;
  document.querySelector('main').insertBefore(section,document.querySelector('main footer'));
  const dialog=document.createElement('dialog');dialog.className='automation-dialog';dialog.id='automation-dialog';dialog.setAttribute('aria-labelledby','automation-editor-title');
  dialog.innerHTML=`<form id="automation-form"><h2 id="automation-editor-title">创建自动化</h2><p>先保存并试运行；启用后，后台按本机时间执行。</p><label class="automation-field">任务名称<input id="auto-name" required maxlength="80" placeholder="例如：每天早上提醒我检查任务"></label><div class="automation-form-grid"><label class="automation-field">执行内容<select id="auto-kind">${Object.entries(names).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label class="automation-field">时间规则<select id="auto-schedule"><option value="interval">固定间隔</option><option value="daily">每天定时</option></select></label><label class="automation-field" id="auto-interval-field">间隔（分钟）<input id="auto-minutes" type="number" min="1" max="10080" value="10"></label><label class="automation-field" id="auto-time-field" hidden>每天几点<input id="auto-at" type="time" value="09:00"></label></div><label class="automation-field" id="auto-threshold-field" hidden>磁盘剩余空间低于（GB）<input id="auto-threshold" type="number" min="1" max="10000" value="20"></label><label class="automation-field" id="auto-content-field">提醒内容<textarea id="auto-content" maxlength="4000" placeholder="写下想按时收到的信息"></textarea></label><label class="automation-check"><input id="auto-notify" type="checkbox">发送飞书通知</label><div id="auto-recipients" class="automation-recipients" hidden></div><label class="automation-check"><input id="auto-enabled" type="checkbox">保存后启用</label><p id="auto-explain">服务与磁盘监控只在异常变化及恢复时通知。电脑休眠期间不执行，唤醒后补做一次。</p><div id="auto-error" class="automation-error" role="alert"></div><div class="automation-form-actions"><button class="button" type="button" id="auto-cancel">取消</button><button class="button primary" id="auto-save" type="submit">保存自动化</button></div></form>`;
  document.body.append(dialog);
  const time=t=>t?new Date(t*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
  function renderJobs(){
    const openRuns=new Set([...$('automation-runs').querySelectorAll('details[open]')].map(x=>x.dataset.run));
    const q=$('automation-search').value.toLowerCase(),jobs=state.jobs.filter(j=>j.name.toLowerCase().includes(q));
    $('automation-enabled').textContent=state.jobs.filter(j=>j.enabled).length;
    $('automation-failed').textContent=state.runs.filter(r=>['failed','interrupted'].includes(r.status)).length;
    $('automation-next').textContent=time(state.jobs.filter(j=>j.enabled).sort((a,b)=>a.nextRun-b.nextRun)[0]?.nextRun);
    $('automation-list').innerHTML=jobs.length?jobs.map(j=>`<article class="automation-card"><div><h3>${esc(j.name)}<span class="automation-badge ${j.enabled?'':'paused'}">${j.running?'执行中':j.enabled?'已启用':'已暂停'}</span></h3><p>${names[j.kind]} · ${j.schedule==='daily'?'每天 '+esc(j.at):'每 '+j.minutes+' 分钟'} · ${j.notify?'飞书 '+j.recipientKeys.length+' 个对象':'仅记录结果'}</p><p>${j.enabled?'下次 '+time(j.nextRun):'启用后按设定时间执行'}</p></div><div class="automation-card-actions"><button class="button" data-edit="${esc(j.id)}">编辑</button><button class="button" data-preview="${esc(j.id)}">试运行</button><button class="button" data-auto-toggle="${esc(j.id)}">${j.enabled?'暂停':'启用'}</button><button class="button" data-delete="${esc(j.id)}">删除</button></div></article>`).join(''):`<div class="automation-empty"><strong>${q?'没有匹配的任务':'把第一件例行工作交给 QuotaPool'}</strong>${q?'试试其他名称':'选择上方模板，设置时间，再试运行查看结果。'}</div>`;
    $('automation-runs').innerHTML=state.runs.length?state.runs.map(r=>`<details class="automation-run" data-run="${esc(r.id)}" ${openRuns.has(r.id)?'open':''}><summary><span>${esc(r.name)} · <span class="${r.status==='failed'?'bad':''}">${statuses[r.status]||esc(r.status)}</span></span><span>${time(r.at)}</span></summary><pre>${esc(r.detail)}</pre></details>`).join(''):'<p class="panel-note">还没有执行记录。试运行不会发送飞书消息。</p>';
  }
  async function refresh(){if(busy)return;busy=true;try{const r=await fetch('/api/automations');if(!r.ok)throw Error('自动化服务尚未加载，请检查面板服务版本');state=await r.json();renderJobs();$('automation-engine').textContent=state.engine==='running'?`后台调度已就绪 · ${state.timezone} · 关闭浏览器后继续运行，退出 QuotaPool 后暂停。`:`预览模式 · ${state.timezone} · 可保存和试运行，自动调度尚未启动。`;}catch(e){$('automation-engine').textContent=e.message}finally{busy=false}}
  function fields(){const kind=$('auto-kind').value,daily=$('auto-schedule').value==='daily';$('auto-time-field').hidden=!daily;$('auto-interval-field').hidden=daily;$('auto-threshold-field').hidden=kind!=='disk';$('auto-content-field').hidden=kind!=='reminder';$('auto-content').required=kind==='reminder';$('auto-recipients').hidden=!$('auto-notify').checked;}
  function editor(job={},kind='reminder'){
    editing=job.id||null;$('automation-editor-title').textContent=editing?'编辑自动化':'创建自动化';$('auto-name').value=job.name||names[kind];$('auto-kind').value=job.kind||kind;$('auto-schedule').value=job.schedule||(kind==='digest'?'daily':'interval');$('auto-minutes').value=job.minutes||10;$('auto-at').value=job.at||'09:00';$('auto-threshold').value=job.threshold||20;$('auto-content').value=job.content||'';$('auto-notify').checked=!!job.notify;$('auto-enabled').checked=!!job.enabled;$('auto-error').textContent='';
    const recipients=latest?.dashboard?.lark?.settings?.recipients||[];
    $('auto-recipients').innerHTML=recipients.length?recipients.map(r=>`<label class="automation-check"><input type="checkbox" value="${esc(r.key)}" ${(job.recipientKeys||[]).includes(r.key)?'checked':''}>${esc(r.label)} · ${r.type==='chat'?'群聊':'用户'}</label>`).join(''):'尚未保存收件人。请先到飞书设置添加自己或目标群。';fields();dialog.showModal();$('auto-name').focus();
  }
  $('automation-new').onclick=()=>editor();section.querySelectorAll('[data-template]').forEach(b=>b.onclick=()=>editor({},b.dataset.template));$('automation-lark').onclick=()=>activateView('lark');$('automation-refresh').onclick=refresh;$('automation-search').oninput=renderJobs;$('auto-cancel').onclick=()=>dialog.close();['auto-kind','auto-schedule','auto-notify'].forEach(id=>$(id).onchange=fields);
  $('automation-form').onsubmit=async e=>{e.preventDefault();const b=$('auto-save');b.disabled=true;try{await larkPost('/api/automations/save',{id:editing,name:$('auto-name').value,kind:$('auto-kind').value,schedule:$('auto-schedule').value,minutes:Number($('auto-minutes').value),at:$('auto-at').value,threshold:Number($('auto-threshold').value),content:$('auto-content').value,notify:$('auto-notify').checked,enabled:$('auto-enabled').checked,recipientKeys:[...$('auto-recipients').querySelectorAll('input:checked')].map(x=>x.value)});dialog.close();await refresh();notice('自动化已保存。可先试运行检查结果。')}catch(e){$('auto-error').textContent=e.message}finally{b.disabled=false}};
  $('automation-list').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const key=['edit','preview','autoToggle','delete'].find(k=>b.dataset[k]);if(!key)return;const job=state.jobs.find(j=>j.id===b.dataset[key]);if(key==='edit'){editor(job);return}if(key==='delete'&&!confirm(`删除“${job.name}”？历史记录会保留。`))return;b.disabled=true;try{if(key==='autoToggle')await larkPost('/api/automations/save',{...job,enabled:!job.enabled});else await larkPost('/api/automations/'+(key==='delete'?'delete':'preview'),{id:job.id});await refresh();if(key==='preview'){notice('试运行结束，请展开最新记录查看结果；没有发送飞书消息。');$('automation-runs').querySelector('details')?.setAttribute('open','')}}catch(e){notice(e.message,true)}finally{b.disabled=false}};
  const context=document.createElement('div');context.className='workspace-context';context.innerHTML='<p>让服务与空间检查自动执行，在异常或恢复时通知你。</p><button class="button">配置定时监控 ↗</button>';context.querySelector('button').onclick=()=>{activateView('automations');refresh()};$('overview').prepend(context);
  // A stable workspace header, grouped navigation and contextual actions.
  const pages={overview:['工作空间','运行概览','掌握账号、服务与本机资源的当前状态。'],accounts:['工作空间','账号与额度','查看可用额度、重置时间与账号状态。'],automations:['工作空间','自动化中心','安排例行任务，在需要关注时收到通知。'],analytics:['观察','用量分析','了解工作量分布与额度利用效率。'],activity:['观察','请求记录','追踪会话活动、执行状态与账号分配。'],windows:['管理','磁盘与存储','查看空间压力与安全维护建议。'],resources:['管理','系统资源','了解 CPU、内存和任务资源占用。'],routes:['管理','模型路由','核对模型与账号池的分配规则。'],lark:['管理','飞书与通知','管理连接身份、通知对象和内容分享。']};
  const navigation=document.querySelector('.nav');
  for(const group of ['工作空间','观察','管理']){
    const label=document.createElement('div');label.className='workspace-nav-group';label.textContent=group;navigation.append(label);
    for(const [key,value] of Object.entries(pages))if(value[0]===group){const link=navigation.querySelector(`[data-nav="${key}"]`);link.lastChild.textContent=value[1];navigation.append(link)}
  }
  document.querySelector('.nav-label').hidden=true;
  const icons={overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',accounts:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M17 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-4',automations:'M13 2 3 14h8l-1 8 11-13h-8z',analytics:'M4 20V10 M10 20V4 M16 20v-7 M22 20H2',activity:'M3 5h18 M3 12h18 M3 19h18',windows:'M4 3h16v18H4z M8 7h8 M8 17h1 M12 17h1',resources:'M8 8h8v8H8z M4 4h16v16H4z M8 1v3 M16 1v3 M8 20v3 M16 20v3 M1 8h3 M20 8h3',routes:'M5 3v12a4 4 0 0 0 4 4h10 M15 15l4 4-4 4 M5 8h14 M15 4l4 4-4 4',lark:'m3 3 18 9-18 9 4-9z M7 12h14'};
  for(const [key,path] of Object.entries(icons))navigation.querySelector(`[data-nav="${key}"] .nav-icon`).innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
  const header=document.querySelector('.topbar'), oldActions=header.querySelector('.actions');
  const bar=document.createElement('div');bar.className='workspace-location';bar.innerHTML='<span>我的工作空间 <span class="location-divider">/</span> <strong id="workspace-location-name">运行概览</strong></span><span class="workspace-local">本机连接</span>';
  document.querySelector('main').prepend(bar);
  const title=document.createElement('div');title.className='workspace-page-title';title.innerHTML='<h1 id="workspace-page-name"></h1><p id="workspace-page-description"></p>';
  header.prepend(title);
  const status=document.createElement('div');status.className='workspace-status-strip';
  status.append($('system-health'),$('last-updated'),$('desktop-status'));
  header.after(status);
  // Existing IDs and handlers are retained when controls move.
  const tools=document.createElement('div');tools.className='workspace-global-tools';tools.append($('refresh'),$('theme-toggle'));
  bar.append(tools);
  const more=oldActions.querySelector('.popover');more.append($('auto-refresh'));
  const navSearch=document.createElement('input');navSearch.className='workspace-nav-search';navSearch.type='search';navSearch.placeholder='查找页面…';navSearch.setAttribute('aria-label','查找页面');navigation.before(navSearch);
  navSearch.oninput=()=>{const q=navSearch.value.trim();navigation.querySelectorAll('a').forEach(a=>a.hidden=!!q&&!a.textContent.includes(q))};
  // Keep live-render targets intact while putting the primary information first.
  const diskPage=$('windows-section'), drives=diskPage.querySelector('.drive-section');
  diskPage.querySelector('.panel-head').after(drives);
  diskPage.querySelector('h2').textContent='存储概况';
  diskPage.querySelector('.runtime-summary').hidden=true;
  const maintenance=document.createElement('details');maintenance.className='workspace-maintenance';
  maintenance.innerHTML='<summary>维护策略与系统盘详情<span>查看检测频率、安全策略和 C 盘明细</span></summary>';
  maintenance.append(diskPage.querySelector('.status-grid'),diskPage.querySelector('.space'));
  drives.after(maintenance);
  $('resources-section').querySelector('.runtime-summary').hidden=true;
  $('resources-section').querySelector('h2').textContent='资源概况';
  section.querySelector('.workspace-heading h2').textContent='例行工作';
  const baseActivate=activateView;
  activateView=function(view,updateHash=true){baseActivate(view,updateHash);const info=pages[view]||pages.overview;$('workspace-page-name').textContent=info[1];$('workspace-page-description').textContent=info[2];$('workspace-location-name').textContent=info[1];document.title=info[1]+' · QuotaPool';document.body.dataset.page=view;$('add-account').hidden=!['accounts','overview'].includes(view);$('open-lark-share').hidden=!['lark','overview'].includes(view);navSearch.value='';navigation.querySelectorAll('a').forEach(a=>a.hidden=false)};
  activateView(viewFromHash(),false);refresh();setInterval(()=>{if(!document.hidden)refresh()},15000);
})();
