'use strict';
let resourceRows=[], resourcePending=false;
const resourcePercent=value=>Number(value).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%';
const resourceGiB=value=>(value/1073741824).toLocaleString('ru-RU',{maximumFractionDigits:2});
const resourceSection=document.createElement('section');
resourceSection.className='resource-section';
resourceSection.innerHTML='<div class="panel-heading"><div><h2>Ресурсы VPS</h2><p>Весь сервер · обновление каждые 30 секунд</p></div><span id="resource-status" class="subtle-note" role="status">Получаем измерения…</span></div><div class="resource-grid"></div>';
$('.recent-panel').before(resourceSection);

class ResourceChart {
  constructor(key,title,description){
    this.key=key;this.hours=24;this.index=-1;
    this.card=document.createElement('section');this.card.className='resource-card';this.card.id='resource-'+key;
    this.card.innerHTML='<div class="panel-heading"><div><h3></h3><p></p></div><strong class="resource-current">—</strong></div><div class="chart-extras"><div class="chart-ranges" role="group" aria-label="Период графика"><button type="button" data-resource-hours="1" aria-pressed="false">1 час</button><button type="button" data-resource-hours="6" aria-pressed="false">6 часов</button><button type="button" data-resource-hours="24" aria-pressed="true">24 часа</button></div></div><div class="chart-wrap"><svg class="resource-chart" viewBox="0 0 800 240" tabindex="0" role="img"></svg><div class="chart-tooltip" role="status" hidden><strong></strong><div><span></span><b></b></div></div></div><p class="resource-empty">История появится после первых измерений.</p><div class="resource-caption"><span class="resource-peak">Пик за период —</span><span>Наведите курсор или коснитесь графика</span></div>';
    $('.resource-grid').append(this.card);
    this.find=selector=>this.card.querySelector(selector);
    this.find('h3').textContent=title;this.find('.panel-heading p').textContent=description;
    this.svg=this.find('svg');this.tip=this.find('.chart-tooltip');
    this.svg.setAttribute('aria-label',title+': история в процентах; стрелки, Home, End для просмотра');
    this.tip.id='resource-tip-'+key;this.svg.setAttribute('aria-describedby',this.tip.id);
    this.tip.querySelector('span').textContent=key==='cpu'?'Загрузка':'Занято';
    this.card.querySelectorAll('[data-resource-hours]').forEach(button=>button.addEventListener('click',()=>{
      this.hours=Number(button.dataset.resourceHours);
      this.card.querySelectorAll('[data-resource-hours]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
      this.render();
    }));
    this.svg.addEventListener('pointermove',event=>this.inspect(event));
    this.svg.addEventListener('pointerdown',event=>this.inspect(event));
    this.svg.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')this.hide();});
    this.svg.addEventListener('pointercancel',()=>this.hide());this.svg.addEventListener('blur',()=>this.hide());
    this.svg.addEventListener('focus',()=>{if(this.points?.length)this.show(this.points.length-1);});
    this.svg.addEventListener('keydown',event=>{
      if(event.key==='Escape'){this.hide();return;}
      if(!this.points?.length||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const last=this.points.length-1;
      this.show(event.key==='Home'?0:event.key==='End'?last:Math.max(0,Math.min(last,(this.index<0?last:this.index)+(event.key==='ArrowLeft'?-1:1))));
    });
  }
  value(row){return this.key==='cpu'?row[1]:row[2]/row[3]*100;}
  format(row){return this.key==='cpu'?resourcePercent(row[1]):resourceGiB(row[2])+' / '+resourceGiB(row[3])+' GiB ('+resourcePercent(this.value(row))+')';}
  render(){
    const now=serverNow();if(now===null)return;
    this.now=now;this.start=now-this.hours*3600;this.hide();
    this.points=resourceRows.filter(row=>row[0]>=this.start&&row[0]<=now&&this.value(row)!==null);
    this.svg.replaceChildren();
    const mobile=matchMedia('(max-width:650px)').matches;
    this.left=mobile?78:65;this.right=785;this.top=15;this.bottom=195;
    this.x=at=>this.left+(at-this.start)/(this.now-this.start)*(this.right-this.left);
    this.y=value=>this.bottom-value/100*(this.bottom-this.top);
    for(let i=0;i<=4;i++){
      const value=i*25,y=this.y(value);
      this.svg.append(svgElement('line',{x1:this.left,y1:y,x2:this.right,y2:y,class:'chart-grid'}));
      this.svg.append(svgElement('text',{x:this.left-12,y:y+6,'text-anchor':'end',class:'resource-axis'},value+'%'));
      const at=this.start+(this.now-this.start)*i/4;
      const label=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'}).format(new Date(at*1000));
      this.svg.append(svgElement('text',{x:this.x(at),y:231,'text-anchor':i===0?'start':i===4?'end':'middle',class:'resource-axis'},label));
    }
    let path='',area='',segment=[],previous=null;
    const close=()=>{if(segment.length>1)area+='M'+this.x(segment[0][0])+','+this.bottom+' '+segment.map(row=>'L'+this.x(row[0])+','+this.y(this.value(row))).join(' ')+' L'+this.x(segment[segment.length-1][0])+','+this.bottom+' Z ';segment=[];};
    for(const row of this.points){const connected=previous!==null&&row[0]-previous<=90;if(!connected)close();path+=(connected?'L':'M')+this.x(row[0]).toFixed(2)+','+this.y(this.value(row)).toFixed(2)+' ';segment.push(row);previous=row[0];}close();
    this.svg.append(svgElement('path',{d:area,class:'resource-area'}),svgElement('path',{d:path,class:'resource-line'}));
    this.guide=svgElement('g',{visibility:'hidden','aria-hidden':'true'});
    this.guide.append(svgElement('line',{y1:this.top,y2:this.bottom,class:'chart-crosshair'}),svgElement('circle',{r:5,class:'resource-dot'}));this.svg.append(this.guide);
    const latest=resourceRows[resourceRows.length-1],fresh=latest&&now-latest[0]>=0&&now-latest[0]<90;
    this.find('.resource-current').textContent=fresh&&this.value(latest)!==null?this.format(latest):'—';
    let peak=0;for(const row of this.points)peak=Math.max(peak,this.value(row));
    this.find('.resource-peak').textContent='Пик за период '+(this.points.length?resourcePercent(peak):'—');
    this.find('.resource-empty').hidden=this.points.length>1;
  }
  hide(){this.index=-1;if(this.guide)this.guide.setAttribute('visibility','hidden');this.tip.hidden=true;}
  show(index,at=null){
    const row=this.points[index];if(!row)return;
    this.index=index;const gap=at!==null&&Math.abs(at-row[0])>90,x=this.x(gap?at:row[0]);
    this.tip.hidden=false;this.tip.querySelector('strong').textContent=dateTime(gap?at:row[0])+' МСК';
    this.tip.querySelector('b').textContent=gap?'Нет измерений':this.format(row);
    const [line,dot]=this.guide.children;line.setAttribute('x1',x);line.setAttribute('x2',x);
    dot.setAttribute('cx',x);dot.setAttribute('cy',this.y(this.value(row)));dot.setAttribute('visibility',gap?'hidden':'visible');this.guide.setAttribute('visibility','visible');
    const width=this.svg.getBoundingClientRect().width;
    this.tip.style.left=Math.max(8,Math.min(width-this.tip.offsetWidth-8,x/800*width-this.tip.offsetWidth/2))+'px';
  }
  inspect(event){
    if(!this.points?.length)return;
    const rect=this.svg.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width*800,y=(event.clientY-rect.top)/rect.height*240;
    if(x<this.left||x>this.right||y<this.top||y>this.bottom){this.hide();return;}
    const at=this.start+(x-this.left)/(this.right-this.left)*(this.now-this.start);
    let lo=0,hi=this.points.length-1;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(this.points[mid][0]<at)lo=mid+1;else hi=mid;}
    if(lo>0&&Math.abs(this.points[lo-1][0]-at)<Math.abs(this.points[lo][0]-at))lo--;
    this.show(lo,at);
  }
}
const resourceCharts=[new ResourceChart('cpu','Процессор','Средняя загрузка всех ядер'),new ResourceChart('memory','Оперативная память','Занято без доступного файлового кэша')];
async function refreshResources(){
  if(resourcePending||document.hidden)return;
  resourcePending=true;
  try{
    const since=resourceRows.length?resourceRows[resourceRows.length-1][0]:0;
    const data=await api('resources?since='+since);syncServerClock(data);
    const valid=data.samples.filter(row=>Array.isArray(row)&&row.length===4&&Number.isFinite(row[0])&&(row[1]===null||(Number.isFinite(row[1])&&row[1]>=0&&row[1]<=100))&&Number.isFinite(row[2])&&Number.isFinite(row[3])&&row[3]>0&&row[2]>=0&&row[2]<=row[3]);
    resourceRows=[...new Map([...resourceRows,...valid].map(row=>[row[0],row])).values()].filter(row=>row[0]>=serverNow()-86400&&row[0]<=serverNow()).sort((a,b)=>a[0]-b[0]).slice(-3000);
    resourceCharts.forEach(chart=>chart.render());
    const last=resourceRows[resourceRows.length-1];
    $('#resource-status').textContent=!last?'История накапливается':serverNow()-last[0]>=90?'Нет свежих измерений':'Обновлено '+dateTime(last[0]);
  }catch(error){$('#resource-status').textContent='Метрики временно недоступны';resourceCharts.forEach(chart=>chart.render());}
  finally{resourcePending=false;}
}
let resourceResize;
addEventListener('resize',()=>{clearTimeout(resourceResize);resourceResize=setTimeout(()=>resourceCharts.forEach(chart=>chart.render()),150);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshResources();});
refreshResources();setInterval(refreshResources,30000);
