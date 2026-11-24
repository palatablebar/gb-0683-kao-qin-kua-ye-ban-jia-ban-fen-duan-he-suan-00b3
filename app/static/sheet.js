'use strict';
const sheet={pending:false};const message=document.getElementById('message');
async function recount(input){if(sheet.pending)return;sheet.pending=true;message.textContent='工时重算中…';try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});const data=await response.json();if(!response.ok)throw new Error(data.message);location.reload();}catch(error){message.textContent=error.message+'，请修正打卡';}finally{sheet.pending=false;}}
document.getElementById('recount').addEventListener('click',()=>recount({sample:'dirty.csv'}));
const file=document.getElementById('clock-file');if(file)file.addEventListener('submit',event=>{event.preventDefault();const values=new FormData(file);const csv=values.get('csv');recount(csv?{csv}:{sample:values.get('sample')});});
const check=document.getElementById('check');if(check)check.addEventListener('click',async()=>{try{const response=await fetch('/api/runs/'+check.dataset.id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(check.dataset.version),decision:'通过'})});const data=await response.json();if(!response.ok)throw Error(data.message);check.dataset.version=data.review_version;message.textContent='工资复核通过 / '+data.review_version;}catch(error){message.textContent=error.message;}});
const paper=document.getElementById('paper');if(paper)paper.addEventListener('click',()=>window.print());
document.querySelectorAll('.period-seal').forEach(button=>button.addEventListener('click',async()=>{
  try{
    const response=await fetch('/api/periods/'+button.dataset.period+'/seal',{method:'POST'});
    const data=await response.json();
    if(!response.ok)throw Error(data.message);
    message.textContent='期间 '+button.dataset.period+' 已封存 / '+data.body_hash.slice(0,12);
    location.reload();
  }catch(error){message.textContent='封存被阻止：'+error.message;}
}));
document.querySelectorAll('#period-import,#period-supplement').forEach(button=>button.addEventListener('click',async()=>{
  if(sheet.pending)return;sheet.pending=true;message.textContent='期间分摊重算中…';
  try{
    const response=await fetch(button.dataset.url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();
    if(!response.ok)throw new Error(data.message);
    const adjs=(data.new_adjustments||[]).length;
    message.textContent='已生成 '+data.written.length+' 个期间快照'+(adjs?'，调整 '+adjs+' 笔':'')+(data.idempotent?'（同批次幂等，无重复调整）':'');
    location.reload();
  }catch(error){message.textContent='重算失败已回滚：'+error.message;}finally{sheet.pending=false;}
}));
document.querySelectorAll('td[tabindex]').forEach(cell=>cell.addEventListener('focus',()=>message.textContent=cell.textContent.trim()));
