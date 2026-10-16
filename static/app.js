'use strict';
const base = document.documentElement.dataset.base;
const $ = selector => document.querySelector(selector);
let csrf = '', busy = false, statusData = null, historyData = null;
let settingsDirty = false, settingsLoaded = false;
let chartHours=24, chartState=null, chartIndex=-1;
let serverClock=null;
function syncServerClock(data) {if(Number.isFinite(data.server_time))serverClock={at:data.server_time,tick:performance.now()};}
function serverNow() {return serverClock?serverClock.at+(performance.now()-serverClock.tick)/1000:null;}
const modes = {active:'Учёт активен', waiting:'Ожидание 03:00', hold:'Лимит достигнут'};
const reasons = {threshold:'Достигнут суточный порог', user_pause:'Нода остановлена вручную', user_resume:'Нода запущена вручную', user_rearm:'Учёт восстановлен с ожиданием новых суток', first_full_utc_day:'Ожидание первых полных суток', host_reboot:'Учёт продолжен после перезагрузки сервера', user_settings:'Настройки трафика изменены'};
const gb = bytes => (Number(bytes || 0) / 1e9).toLocaleString('ru-RU', {maximumFractionDigits:2}) + ' GB';
const mbps = bytes => (Number(bytes || 0) * 8 / 1e6).toLocaleString('ru-RU', {maximumFractionDigits:2}) + ' Мбит/с';
const policyOf = state => state.settings || {limit_bytes:state.config.limit, stop_on_limit:true};
function formatLimit(bytes) {const b=BigInt(bytes), fraction=b%1000000000n;return String(b/1000000000n)+(fraction?'.'+String(fraction).padStart(9,'0').replace(/0+$/,''):'');}
function readSettings() {
  const value=$('#limit-gb').value.trim().replace(',','.');
  if(!/^\d+(\.\d{1,9})?$/.test(value))throw new Error('Введите положительное число GB, например 145 или 145,5. Не более 9 знаков после запятой.');
  const [whole,fraction='']=value.split('.'), bytes=BigInt(whole)*1000000000n+BigInt(fraction.padEnd(9,'0'));
  if(bytes<1n||bytes>9000000000000000n)throw new Error('Порог должен быть больше нуля и не превышать 9 000 000 GB.');
  return {limit_bytes:Number(bytes),stop_on_limit:$('#policy-stop').checked};
}
function settingsImpact() {
  try {const next=readSettings(), state=statusData?.snapshot?.state, reached=state&&state.used>=next.limit_bytes;
    $('#policy-impact').textContent=!next.stop_on_limit?'Нода продолжит работать даже при превышении порога. Если её остановил лимит, она вернётся после сохранения; ручная пауза сохранится.':reached?'Уже учтённый расход выше этого порога. После сохранения нода будет остановлена сразу.':'Нода будет остановлена при достижении порога. Если поднять порог, остановленная по лимиту нода вернётся при доступном остатке.';
  } catch {$('#policy-impact').textContent='Укажите порог, чтобы увидеть действие после сохранения.';}
}
function renderSettings(state, available) {
  $('#save-settings').disabled=busy||!available||!settingsDirty;
  for(const input of document.querySelectorAll('#quota-settings input'))input.disabled=busy||!available;
  $('#save-settings').textContent=busy?'Применяем…':'Сохранить настройки';
  $('#settings-status').textContent=!available?'Управление недоступно':busy?'Применяем изменения':settingsDirty?'Не сохранено':'Настройки применены';
  if(state&&!settingsDirty&&!busy){const p=policyOf(state);$('#limit-gb').value=formatLimit(p.limit_bytes);$('#policy-stop').checked=p.stop_on_limit;$('#policy-monitor').checked=!p.stop_on_limit;settingsLoaded=true;}
  settingsImpact();
}
const dateTime = value => new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'}).format(new Date(value * 1000));
const svgElement = (tag, attrs, text) => { const e=document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v)); if(text!==undefined)e.textContent=text; return e; };
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; setTimeout(()=>$('#toast').hidden=true,6000); }
async function api(path, options={}) { const response=await fetch(base+'api/'+path, options); if(response.status===401){location.replace(base); throw new Error('Войдите в панель');} const data=await response.json(); if(!response.ok)throw new Error(data.error || 'Запрос не выполнен'); return data; }
function selectTab(name) { document.querySelectorAll('.tab-panel').forEach(el=>el.hidden=el.id!==name); document.querySelectorAll('[data-tab]').forEach(el=>{const selected=el.dataset.tab===name;el.classList.toggle('selected',selected); if(selected)el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');}); }
document.querySelectorAll('[data-tab]').forEach(el=>el.addEventListener('click',()=>selectTab(el.dataset.tab)));
document.querySelectorAll('[data-open-tab]').forEach(el=>el.addEventListener('click',()=>selectTab(el.dataset.openTab)));
function countdown() { const stamp=serverNow();if(stamp===null){$('#reset-countdown').textContent='—';return;} const now=new Date(stamp*1000), next=new Date(now); next.setUTCHours(24,0,0,0); const seconds=Math.max(0,Math.floor((next-now)/1000)); $('#reset-countdown').textContent=[Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':'); }
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
  syncServerClock(data);countdown();
  const snap=data.snapshot, health=data.health, state=snap?.state;
  const fresh=data.controller_fresh===true;
  const fault=data.fault || snap?.fault;
  renderSettings(state,!!state&&fresh&&health.ok&&!fault);
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
  const policy=policyOf(state), cfg={...state.config,limit:policy.limit_bytes}, trusted=state.mode!=='waiting' && !fault;
  if(trusted&&!policy.stop_on_limit&&state.used>=cfg.limit&&fresh&&health.ok){badge.textContent='Порог превышен';badge.classList.add('waiting');}
  if(notice.hidden&&trusted&&!policy.stop_on_limit){notice.hidden=false;notice.textContent='Режим наблюдения: расход учитывается, но превышение порога не останавливает ноду.';}
  $('#node-name').textContent=cfg.target; document.title=cfg.target+' · Traffic Guard';
  $('#used-label').textContent=state.mode==='waiting'?'Наблюдено после установки':'Использовано сегодня';
  $('#used').textContent=gb(state.used);$('#remaining').textContent=trusted?gb(Math.max(0,cfg.limit-state.used)):'Неизвестно';
  $('#usage-percent').textContent=trusted?(state.used/cfg.limit*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'% порога использовано':'Расход за полные сутки неизвестен';
  $('#limit-caption').textContent=(policy.stop_on_limit?'Защитный порог: ':'Порог наблюдения: ')+gb(cfg.limit);
  $('.quota-remaining span').textContent=policy.stop_on_limit?'Осталось до остановки':'Осталось до порога';
  $('#accounting-description').textContent=cfg.interface+': входящий + исходящий, сутки UTC';
  drawRuler(cfg.limit,state.used,trusted);
  $('#rate-rx').textContent=fresh&&health.ok&&!fault?mbps(snap.rate_rx):'—';$('#rate-tx').textContent=fresh&&health.ok&&!fault?mbps(snap.rate_tx):'—';
  $('#node-state').textContent=snap.node_missing?'Не создан':snap.node_running===null?'Не удалось проверить':snap.node_running?'Работает':'Остановлена';
  $('#node-state').classList.toggle('running',snap.node_running===true);
  $('#node-explanation').textContent=fault?'Запуск заблокирован до восстановления учёта.':state.manual_paused?'Нода остановлена вручную. Автоматический запуск при смене суток отключён.':state.mode==='waiting'&&state.passive_wait?'Начнём полный учёт с ближайших 03:00 МСК.':state.mode==='hold'?'Суточный порог достигнут. Дождитесь новых суток.':!policy.stop_on_limit?'Трафик учитывается. Остановка по порогу отключена.':snap.node_running?'Guard следит за расходом трафика.':'Можно запустить при достоверном остатке лимита.';
  $('.node-footnote').textContent=policy.stop_on_limit?'Запуск разрешён только при доступном остатке лимита.':'Порог не ограничивает запуск. Ручная пауза и аварийная защита действуют.';
  $('#interface').textContent=cfg.interface;$('#coverage').textContent=trusted&&state.accounting_complete!==false?'Полный':'Неполный';$('#controller-state').textContent=fault?'Блокировка':fresh&&health.ok?'Работает':'Требует проверки';
  $('#pause').disabled=busy||!fresh||!snap.node_running;$('#resume').disabled=busy||!fresh||!!fault||state.mode!=='active'||(policy.stop_on_limit&&state.used>=cfg.limit)||snap.node_missing||(snap.node_running&&!state.manual_paused);$('#rearm').disabled=busy||!fresh;
  $('#setting-target').textContent=cfg.target;$('#setting-interface').textContent=cfg.interface;$('#setting-limit').textContent=gb(cfg.limit);
}
function rows(target, days) { const body=$(target);body.replaceChildren();if(!days.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=5;td.textContent='История появится после первых измерений. Расход до установки неизвестен.';tr.append(td);body.append(tr);return;}for(const day of days){const tr=document.createElement('tr');for(const text of [day.day,gb(day.used),gb(day.rx),gb(day.tx)]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const td=document.createElement('td'),tag=document.createElement('span');tag.className='coverage-tag'+(day.complete?' full':'');tag.textContent=day.complete?'Полный':'Неполный';td.append(tag);tr.append(td);body.append(tr);} }
function drawChart(samples) {
  const now=serverNow();if(now===null)return;
  const chart=$('#traffic-chart');chart.replaceChildren();hideChartPoint();const mobile=matchMedia('(max-width:650px)').matches;
  const height=mobile?260:220;
  chart.setAttribute('viewBox','0 0 800 '+height);
  const start=now-chartHours*3600,left=mobile?78:48,right=786,top=15,bottom=mobile?210:182;
  const points=samples.filter(s=>Number.isFinite(s.at)&&Number.isFinite(s.rx)&&Number.isFinite(s.tx)&&s.rx>=0&&s.tx>=0&&s.at>=start&&s.at<=now).sort((a,b)=>a.at-b.at);
  let peakRx=0,peakTx=0;for(const s of points){peakRx=Math.max(peakRx,s.rx);peakTx=Math.max(peakTx,s.tx);}
  const max=Math.max(1,peakRx*8/1e6,peakTx*8/1e6)*1.12;
  const xOf=s=>left+(s.at-start)/(now-start)*(right-left),yOf=value=>bottom-(value*8/1e6/max)*(bottom-top);
  for(let i=0;i<=4;i++){const y=bottom-(bottom-top)*i/4;chart.append(svgElement('line',{x1:left,y1:y,x2:right,y2:y,class:'chart-grid'}));chart.append(svgElement('text',{x:mobile?70:40,y:y+4,'text-anchor':'end',class:'chart-label'},(max*i/4).toLocaleString('ru-RU',{maximumFractionDigits:1})));}
  for(let i=0;i<=4;i++){const x=left+(right-left)*i/4;const label=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'}).format(new Date((start+(now-start)*i/4)*1000));chart.append(svgElement('text',{x,y:mobile?250:210,'text-anchor':i===0?'start':i===4?'end':'middle',class:'chart-label'},label));}
  for(const key of ['rx','tx']){
    let path='',area='',segment=[],last=0;
    const closeArea=()=>{if(segment.length>1)area+='M'+xOf(segment[0])+','+bottom+' '+segment.map(s=>'L'+xOf(s)+','+yOf(s[key])).join(' ')+' L'+xOf(segment[segment.length-1])+','+bottom+' Z ';segment=[];};
    for(const s of points){if(last&&s.at-last>120)closeArea();path+=(last&&s.at-last<=120?'L':'M')+xOf(s).toFixed(2)+' '+yOf(s[key]).toFixed(2)+' ';segment.push(s);last=s.at;}closeArea();
    if(key==='rx')chart.append(svgElement('path',{d:area,class:'chart-area'}));
    chart.append(svgElement('path',{d:path,class:'chart-line'+(key==='tx'?' tx':'')}));
  }
  const guide=svgElement('g',{class:'chart-selection',visibility:'hidden','aria-hidden':'true'});
  guide.append(svgElement('line',{y1:top,y2:bottom,class:'chart-crosshair'}),svgElement('circle',{r:4.5,class:'chart-dot rx'}),svgElement('circle',{r:4.5,class:'chart-dot tx'}));chart.append(guide);
  chartState={points,start,now,left,right,top,bottom,height,xOf,yOf,guide};
  $('#peak-rx').textContent=points.length?mbps(peakRx):'—';$('#peak-tx').textContent=points.length?mbps(peakTx):'—';
  $('.chart-panel .panel-heading p').textContent='Измеренная скорость за '+({1:'последний час',6:'последние 6 часов',24:'последние 24 часа'}[chartHours]);
  $('#chart-empty').hidden=points.length>1;
}
function hideChartPoint(){chartIndex=-1;if(chartState)chartState.guide.setAttribute('visibility','hidden');const tip=$('#chart-tooltip');if(tip)tip.hidden=true;}
function showChartPoint(index, targetAt=null){
  const g=chartState;if(!g||!g.points.length)return;
  const s=g.points[index],gap=targetAt!==null&&Math.abs(s.at-targetAt)>120;
  const tip=$('#chart-tooltip');tip.hidden=false;
  $('#chart-point-time').textContent=dateTime(gap?targetAt:s.at)+' МСК';
  $('#chart-point-rx').textContent=gap?'Нет измерений':mbps(s.rx);$('#chart-point-tx').textContent=gap?'Нет измерений':mbps(s.tx);
  const x=gap?g.left+(targetAt-g.start)/(g.now-g.start)*(g.right-g.left):g.xOf(s);
  const [line,rx,tx]=g.guide.children;line.setAttribute('x1',x);line.setAttribute('x2',x);
  rx.setAttribute('cx',x);rx.setAttribute('cy',g.yOf(s.rx));tx.setAttribute('cx',x);tx.setAttribute('cy',g.yOf(s.tx));
  rx.setAttribute('visibility',gap?'hidden':'visible');tx.setAttribute('visibility',gap?'hidden':'visible');g.guide.setAttribute('visibility','visible');
  const width=$('#traffic-chart').getBoundingClientRect().width;
  tip.style.left=Math.max(8,Math.min(width-tip.offsetWidth-8,x/800*width-tip.offsetWidth/2))+'px';
  chartIndex=index;
}
function inspectChart(event){
  const g=chartState;if(!g||!g.points.length)return;
  const rect=$('#traffic-chart').getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width*800,y=(event.clientY-rect.top)/rect.height*g.height;
  if(x<g.left||x>g.right||y<g.top||y>g.bottom){hideChartPoint();return;}
  const at=g.start+(x-g.left)/(g.right-g.left)*(g.now-g.start);
  let lo=0,hi=g.points.length-1;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(g.points[mid].at<at)lo=mid+1;else hi=mid;}
  const index=lo>0&&Math.abs(g.points[lo-1].at-at)<Math.abs(g.points[lo].at-at)?lo-1:lo;showChartPoint(index,at);
}
const chart=$('#traffic-chart'),wrap=document.createElement('div');wrap.className='chart-wrap';chart.before(wrap);wrap.append(chart);
chart.setAttribute('tabindex','0');chart.setAttribute('aria-describedby','chart-hint chart-tooltip');
const tooltip=document.createElement('div');tooltip.id='chart-tooltip';tooltip.className='chart-tooltip';tooltip.hidden=true;tooltip.setAttribute('role','status');
tooltip.innerHTML='<strong id="chart-point-time"></strong><div><span class="rx-label">Входящий</span><b id="chart-point-rx"></b></div><div><span class="tx-label">Исходящий</span><b id="chart-point-tx"></b></div>';wrap.append(tooltip);
const chartExtras=document.createElement('div');chartExtras.className='chart-extras';chartExtras.innerHTML='<div class="chart-ranges" role="group" aria-label="Период графика"><button type="button" data-hours="1" aria-pressed="false">1 час</button><button type="button" data-hours="6" aria-pressed="false">6 часов</button><button type="button" data-hours="24" aria-pressed="true">24 часа</button></div><p id="chart-hint">Наведите курсор или коснитесь графика. С клавиатуры — стрелки.</p>';wrap.before(chartExtras);
const peaks=document.createElement('div');peaks.className='chart-peaks';peaks.innerHTML='<span>Пик входящего <strong id="peak-rx">—</strong></span><span>Пик исходящего <strong id="peak-tx">—</strong></span>';wrap.after(peaks);
chart.addEventListener('pointermove',inspectChart);chart.addEventListener('pointerdown',inspectChart);chart.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')hideChartPoint();});chart.addEventListener('pointercancel',hideChartPoint);chart.addEventListener('blur',hideChartPoint);
chart.addEventListener('focus',()=>{if(chartState?.points.length)showChartPoint(chartState.points.length-1);});
chart.addEventListener('keydown',event=>{const g=chartState;if(!g||!g.points.length)return;if(event.key==='Escape'){hideChartPoint();return;}if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const index=event.key==='Home'?0:event.key==='End'?g.points.length-1:Math.max(0,Math.min(g.points.length-1,(chartIndex<0?g.points.length-1:chartIndex)+(event.key==='ArrowLeft'?-1:1)));showChartPoint(index);});
document.querySelectorAll('[data-hours]').forEach(button=>button.addEventListener('click',()=>{chartHours=Number(button.dataset.hours);document.querySelectorAll('[data-hours]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));if(historyData)drawChart(historyData.samples);}));
const navIcons={overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',history:'M4 20V10 M10 20V4 M16 20v-7 M22 20H2',events:'M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',settings:'M4 7h16 M4 17h16 M9 4v6 M16 14v6'};
document.querySelectorAll('.sidebar [data-tab]').forEach(button=>{const icon=svgElement('svg',{viewBox:'0 0 24 24',class:'nav-icon','aria-hidden':'true'});icon.append(svgElement('path',{d:navIcons[button.dataset.tab]}));button.prepend(icon);});
$('.brand span:last-child').lastChild.textContent=' Guard';
function renderHistory(data) {
  syncServerClock(data);
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
async function action(name, extra={}) {
  if(busy)return false;
  busy=true;if(statusData)renderStatus(statusData);
  try {
    const request=await api('action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({action:name,confirm:name==='rearm'?'rearm':undefined,...extra})});
    let result;
    for(let i=0;i<30;i++){await new Promise(resolve=>setTimeout(resolve,1000));result=await api('commands/'+request.id);if(!result.pending)break;}
    if(!result||result.pending)throw new Error('Ответ задерживается. Проверьте состояние перед повторным действием.');
    if(!result.ok)throw new Error(result.error || 'Действие не выполнено');
    if(name==='settings'){settingsDirty=false;$('#settings-feedback').textContent='Сохранено. Настройки уже действуют.';$('#settings-error').textContent='';}
    toast({pause:'Нода остановлена',resume:'Нода запущена',rearm:'Учёт восстановлен. Нода ждёт 03:00 МСК.',settings:'Настройки сохранены и применены'}[name]);
    await refreshStatus();await refreshHistory();return true;
  } catch(e){toast(e.message);if(name==='settings')$('#settings-error').textContent=e.message;return false;}
  finally{busy=false;if(statusData)renderStatus(statusData);}
}
$('#quota-settings').addEventListener('input',()=>{settingsDirty=true;$('#settings-error').textContent='';$('#limit-gb').removeAttribute('aria-invalid');$('#settings-feedback').textContent='Изменения ещё не применены.';if(statusData)renderStatus(statusData);});
$('#limit-gb').addEventListener('blur',()=>{if(settingsLoaded)try{readSettings();}catch(e){$('#settings-error').textContent=e.message;$('#limit-gb').setAttribute('aria-invalid','true');}});
$('#quota-settings').addEventListener('submit',event=>{event.preventDefault();try{const settings=readSettings();$('#settings-error').textContent='';$('#limit-gb').removeAttribute('aria-invalid');action('settings',{settings});}catch(e){$('#settings-error').textContent=e.message;$('#limit-gb').setAttribute('aria-invalid','true');$('#limit-gb').focus();}});
$('#pause').addEventListener('click',()=>action('pause'));$('#resume').addEventListener('click',()=>action('resume'));$('#rearm').addEventListener('click',()=>$('#rearm-dialog').showModal());$('#confirm-rearm').addEventListener('click',()=>{$('#rearm-dialog').close();action('rearm');});$('#logout').addEventListener('click',async()=>{try{await api('logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});location.replace(base);}catch(e){toast(e.message);}});
countdown();setInterval(countdown,1000);refreshStatus();refreshHistory();setInterval(refreshStatus,5000);setInterval(refreshHistory,30000);
