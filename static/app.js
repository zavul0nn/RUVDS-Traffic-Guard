'use strict';
const base = document.documentElement.dataset.base;
const $ = selector => document.querySelector(selector);
let csrf = '', busy = false, statusData = null, historyData = null;
const modes = {active:'Учёт активен', waiting:'Ожидание 03:00', hold:'Лимит достигнут'};
const reasons = {threshold:'Достигнут суточный порог', user_pause:'Нода остановлена вручную', user_resume:'Нода запущена вручную', user_rearm:'Учёт восстановлен с ожиданием новых суток', first_full_utc_day:'Ожидание первых полных суток', host_reboot:'Учёт продолжен после перезагрузки сервера'};
const gb = bytes => (Number(bytes || 0) / 1e9).toLocaleString('ru-RU', {maximumFractionDigits:2}) + ' GB';
const mbps = bytes => (Number(bytes || 0) * 8 / 1e6).toLocaleString('ru-RU', {maximumFractionDigits:2}) + ' Мбит/с';
const dateTime = value => new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'}).format(new Date(value * 1000));
const svgElement = (tag, attrs, text) => { const e=document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v)); if(text!==undefined)e.textContent=text; return e; };
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; setTimeout(()=>$('#toast').hidden=true,6000); }
async function api(path, options={}) { const response=await fetch(base+'api/'+path, options); if(response.status===401){location.replace(base); throw new Error('Войдите в панель');} const data=await response.json(); if(!response.ok)throw new Error(data.error || 'Запрос не выполнен'); return data; }
function selectTab(name) { document.querySelectorAll('.tab-panel').forEach(el=>el.hidden=el.id!==name); document.querySelectorAll('[data-tab]').forEach(el=>{const selected=el.dataset.tab===name;el.classList.toggle('selected',selected); if(selected)el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');}); }
document.querySelectorAll('[data-tab]').forEach(el=>el.addEventListener('click',()=>selectTab(el.dataset.tab)));
document.querySelectorAll('[data-open-tab]').forEach(el=>el.addEventListener('click',()=>selectTab(el.dataset.openTab)));
function countdown() { const now=new Date(), next=new Date(now); next.setUTCHours(24,0,0,0); const seconds=Math.max(0,Math.floor((next-now)/1000)); $('#reset-countdown').textContent=[Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':'); }
function drawRuler(limit, used, trusted) {
  const mobile=matchMedia('(max-width:650px)').matches;
  $('#quota-ruler').setAttribute('viewBox','0 0 1000 '+(mobile?100:62));
  const ratio=Math.min(1,Math.max(0,used/limit));
  $('#quota-fill').setAttribute('width',trusted?1000*ratio:0);$('#quota-fill').classList.toggle('alert',ratio>=.9);
  const ticks=$('#quota-ticks');ticks.replaceChildren();
  for(let i=0;i<=10;i++){const x=i*100;ticks.append(svgElement('line',{x1:x,y1:28,x2:x,y2:i%2?34:39,class:'quota-tick'}));if(i%2===0)ticks.append(svgElement('text',{x,y:mobile?84:56,'text-anchor':i===0?'start':i===10?'end':'middle',class:'quota-tick-label'},(limit/1e9*i/10).toLocaleString('ru-RU',{maximumFractionDigits:1})));}
}
function renderStatus(data) {
  statusData=data; csrf=data.csrf;
  const snap=data.snapshot, health=data.health, state=snap?.state, now=Date.now()/1000;
  const fresh=health && now-health.at>=0 && now-health.at<30;
  const fault=data.fault || snap?.fault;
  $('#connection').textContent=fresh?'Контроллер на связи':'Нет свежих данных';
  $('#updated').textContent=snap?'Обновлено '+dateTime(snap.at):'Измерения ещё не поступили';
  const badge=$('#state-badge'); badge.className='badge';
  badge.textContent=fault?'Аварийная блокировка':!fresh?'Нет связи':!health.ok?'Ошибка наблюдения':modes[state?.mode] || 'Получение данных';
  if(fault || !fresh)badge.classList.add('alert');else if(state?.mode!=='active')badge.classList.add('waiting');
  const notice=$('#notice');notice.hidden=true;notice.className='notice';
  if(fault){notice.hidden=false;notice.classList.add('fault');notice.textContent='Guard заблокировал работу: '+(typeof fault==='string'?fault:fault.error)+'. Устраните причину и используйте «Восстановить учёт».';}
  else if(!fresh){notice.hidden=false;notice.classList.add('fault');notice.textContent='Контроллер не присылает свежие данные. Значения на экране сохранённые; управление временно недоступно.';}
  else if(!health.ok){notice.hidden=false;notice.textContent='Ошибка наблюдения: '+(health.error || 'Проверьте журнал guard')+'. До начала первого полного периода guard не меняет состояние ноды.';}
  else if(state?.mode==='waiting'){notice.hidden=false;notice.textContent=state.passive_wait?'Guard установлен. Расход до установки неизвестен. До 03:00 МСК нода продолжает работать без ограничения со стороны guard; затем начнётся полноценный суточный учёт.':'Учёт поставлен заново. Нода остановлена до ближайших 03:00 МСК, чтобы начать полные сутки.';}
  else if(snap?.node_missing){notice.hidden=false;notice.textContent='Контейнер ещё не создан. Guard ждёт появления ноды '+(state?.config.target || '')+'.';}
  else if(state?.accounting_complete===false){notice.hidden=false;notice.textContent='Учёт продолжен после перезагрузки. Сохранённый расход и трафик после загрузки учтены. Хвост перед выключением мог не попасть в последнюю запись; эти сутки отмечены как неполные.';}
  $('#page-error').textContent='';
  if(!state){$('#pause').disabled=$('#resume').disabled=true;$('#rearm').disabled=busy||!fresh;return;}
  const cfg=state.config, trusted=state.mode!=='waiting' && !fault;
  $('#node-name').textContent=cfg.target; document.title=cfg.target+' · Traffic Guard';
  $('#used-label').textContent=state.mode==='waiting'?'Наблюдено после установки':'Использовано сегодня';
  $('#used').textContent=gb(state.used);$('#remaining').textContent=trusted?gb(Math.max(0,cfg.limit-state.used)):'Неизвестно';
  $('#usage-percent').textContent=trusted?(state.used/cfg.limit*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'% порога использовано':'Расход за полные сутки неизвестен';
  $('#limit-caption').textContent='Защитный порог: '+gb(cfg.limit);
  $('#accounting-description').textContent=cfg.interface+': входящий + исходящий, сутки UTC';
  drawRuler(cfg.limit,state.used,trusted);
  $('#rate-rx').textContent=fresh&&health.ok&&!fault?mbps(snap.rate_rx):'—';$('#rate-tx').textContent=fresh&&health.ok&&!fault?mbps(snap.rate_tx):'—';
  $('#node-state').textContent=snap.node_missing?'Не создан':snap.node_running===null?'Не удалось проверить':snap.node_running?'Работает':'Остановлена';
  $('#node-explanation').textContent=fault?'Запуск заблокирован до восстановления учёта.':state.manual_paused?'Нода остановлена вручную. Автоматический запуск при смене суток отключён.':state.mode==='waiting'&&state.passive_wait?'Начнём ограничивать трафик с ближайших 03:00 МСК.':state.mode==='hold'?'Суточный порог достигнут. Дождитесь новых суток.':snap.node_running?'Guard следит за расходом трафика.':'Можно запустить при достоверном остатке лимита.';
  $('#interface').textContent=cfg.interface;$('#coverage').textContent=trusted&&state.accounting_complete!==false?'Полный':'Неполный';$('#controller-state').textContent=fault?'Блокировка':fresh&&health.ok?'Работает':'Требует проверки';
  $('#pause').disabled=busy||!fresh||!snap.node_running;$('#resume').disabled=busy||!fresh||!!fault||state.mode!=='active'||state.used>=cfg.limit||snap.node_missing||(snap.node_running&&!state.manual_paused);$('#rearm').disabled=busy||!fresh;
  $('#setting-target').textContent=cfg.target;$('#setting-interface').textContent=cfg.interface;$('#setting-limit').textContent=gb(cfg.limit);
}
function rows(target, days) { const body=$(target);body.replaceChildren();if(!days.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=5;td.textContent='История появится после первых измерений. Расход до установки неизвестен.';tr.append(td);body.append(tr);return;}for(const day of days){const tr=document.createElement('tr');for(const text of [day.day,gb(day.used),gb(day.rx),gb(day.tx)]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const td=document.createElement('td'),tag=document.createElement('span');tag.className='coverage-tag'+(day.complete?' full':'');tag.textContent=day.complete?'Полный':'Неполный';td.append(tag);tr.append(td);body.append(tr);} }
function drawChart(samples) {
  const chart=$('#traffic-chart');chart.replaceChildren();const mobile=matchMedia('(max-width:650px)').matches;
  chart.setAttribute('viewBox','0 0 800 '+(mobile?260:220));
  const now=Date.now()/1000,start=now-86400,left=mobile?78:48,right=786,top=15,bottom=mobile?210:182;
  const max=Math.max(1,...samples.flatMap(s=>[s.rx*8/1e6,s.tx*8/1e6]))*1.12;
  for(let i=0;i<=4;i++){const y=bottom-(bottom-top)*i/4;chart.append(svgElement('line',{x1:left,y1:y,x2:right,y2:y,class:'chart-grid'}));chart.append(svgElement('text',{x:mobile?70:40,y:y+4,'text-anchor':'end',class:'chart-label'},(max*i/4).toLocaleString('ru-RU',{maximumFractionDigits:1})));}
  for(let i=0;i<=4;i++){const x=left+(right-left)*i/4;const label=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'}).format(new Date((start+86400*i/4)*1000));chart.append(svgElement('text',{x,y:mobile?250:210,'text-anchor':i===0?'start':i===4?'end':'middle',class:'chart-label'},label));}
  for(const key of ['rx','tx']){let path='',last=0;for(const s of samples){const x=left+(s.at-start)/86400*(right-left),y=bottom-(s[key]*8/1e6/max)*(bottom-top);path+=(last&&s.at-last<=120?'L':'M')+x.toFixed(2)+' '+y.toFixed(2)+' ';last=s.at;}chart.append(svgElement('path',{d:path,class:'chart-line'+(key==='tx'?' tx':'')}));}
  $('#chart-empty').hidden=samples.length>1;
}
function renderHistory(data) {
  historyData=data;
  rows('#recent-days',data.days.slice(0,5));rows('#all-days',data.days);drawChart(data.samples);
  const list=$('#event-list');list.replaceChildren();
  if(!data.events.length){const p=document.createElement('p');p.className='empty-hint';p.textContent='Изменений режима пока нет. Здесь появятся смена суток и действия guard.';list.append(p);}
  for(const event of data.events){
    const row=document.createElement('div');row.className='event';
    const time=document.createElement('time');time.textContent=dateTime(event.at);
    const content=document.createElement('div'),strong=document.createElement('strong'),detail=document.createElement('p');
    strong.textContent=reasons[event.reason] || modes[event.mode] || event.mode;
    detail.textContent='Учтено '+gb(event.used);
    content.append(strong,detail);row.append(time,content);list.append(row);
  }
}
let resizeTimer;
addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(statusData)renderStatus(statusData);if(historyData)drawChart(historyData.samples);},150);});
async function refreshStatus() {try{renderStatus(await api('status'));}catch(e){$('#page-error').textContent=e.message;$('#connection').textContent='Связь потеряна';$('#pause').disabled=$('#resume').disabled=$('#rearm').disabled=true;}}
async function refreshHistory() {try{renderHistory(await api('history'));}catch(e){$('#page-error').textContent=e.message;}}
async function action(name) { if(busy)return;busy=true;if(statusData)renderStatus(statusData);try{const request=await api('action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({action:name,confirm:name==='rearm'?'rearm':undefined})});let result;for(let i=0;i<30;i++){await new Promise(resolve=>setTimeout(resolve,1000));result=await api('commands/'+request.id);if(!result.pending)break;}if(!result||result.pending)throw new Error('Ответ задерживается. Проверьте состояние перед повторным действием.');if(!result.ok)throw new Error(result.error || 'Действие не выполнено');toast({pause:'Нода остановлена',resume:'Нода запущена',rearm:'Учёт восстановлен. Нода ждёт 03:00 МСК.'}[name]);await refreshStatus();await refreshHistory();}catch(e){toast(e.message);}finally{busy=false;if(statusData)renderStatus(statusData);} }
$('#pause').addEventListener('click',()=>action('pause'));$('#resume').addEventListener('click',()=>action('resume'));$('#rearm').addEventListener('click',()=>$('#rearm-dialog').showModal());$('#confirm-rearm').addEventListener('click',()=>{$('#rearm-dialog').close();action('rearm');});$('#logout').addEventListener('click',async()=>{try{await api('logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});location.replace(base);}catch(e){toast(e.message);}});
countdown();setInterval(countdown,1000);refreshStatus();refreshHistory();setInterval(refreshStatus,5000);setInterval(refreshHistory,30000);
