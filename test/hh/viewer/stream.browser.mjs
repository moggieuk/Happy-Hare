// Optional browser integration: node --test test/hh/viewer/stream.browser.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
test('printer telemetry arrives by subscription and stops when leaving playback',async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 try {
  const page=await browser.newPage(), errors=[],calls=[];
  page.on('pageerror',e=>errors.push(e.message));
  const records=JSON.parse(readFileSync(new URL('./fixtures/emu.json',import.meta.url)));
  const machine=records[0].state.printer.mmu_machine;
  const status={mmu_machine:{configuration:machine},mmu:{...records[0].state.printer.mmu,filament_pos:0}};
  let socket;
  const batch=(items,delivery,reset=false)=>({stream_id:'browser-test',delivery_id:delivery,reset,records:items});
  await page.routeWebSocket('**/websocket',ws=>{
   socket=ws;
   ws.onMessage(text=>{
    const req=JSON.parse(text);calls.push(req.method);let result={};
    if(req.method==='server.info') result={moonraker_version:'v0.9.3'};
    else if(req.method==='printer.objects.list') result={objects:Object.keys(status)};
    else if(req.method==='printer.objects.query'||req.method==='printer.objects.subscribe') result={status,eventtime:1};
    else if(req.method==='server.mmu.telemetry.subscribe') result=batch(records.slice(0,20),1,true);
    else if(!req.method.startsWith('server.mmu.telemetry.')) {ws.send(JSON.stringify({id:req.id,error:{message:'Method not found'}}));return;}
    ws.send(JSON.stringify({id:req.id,result}));
   });
  });
  await page.route('http://viewer.test/**',route=>{
   const name=new URL(route.request().url()).pathname.split('/').at(-1)||'index.html';
   assert.notEqual(name,'telemetry');
   route.fulfill({body:readFileSync(new URL('./'+name,import.meta.url)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://viewer.test/');
  await page.locator('#scene [data-part="espooler"]').first().waitFor();
  assert.equal(calls.includes('server.mmu.telemetry.subscribe'),false);
  await page.locator('#view-mode').selectOption('playback');
  await page.waitForFunction(()=>document.querySelector('#trace').textContent.includes('move'));
  assert.equal(calls.filter(c=>c==='server.mmu.telemetry.ack').length,1);
  const before=await page.locator('#trace').textContent();
  socket.send(JSON.stringify({method:'notify_mmu_telemetry',params:[batch(records.slice(20),2)]}));
  await page.waitForTimeout(250);
  assert.notEqual(await page.locator('#trace').textContent(),before);
  assert.equal(calls.filter(c=>c==='server.mmu.telemetry.ack').length,2);
  await page.locator('#view-mode').selectOption('live');
  await page.waitForTimeout(100);
  assert.ok(calls.includes('server.mmu.telemetry.unsubscribe'));
  await page.locator('#view-mode').selectOption('playback');
  await page.locator('#telemetry-file').setInputFiles(new URL('./fixtures/emu.json',import.meta.url).pathname);
  await page.waitForFunction(()=>document.querySelector('#view-mode').value==='recording');
  const subscribed=calls.filter(c=>c==='server.mmu.telemetry.subscribe').length;
  socket.send(JSON.stringify({method:'notify_klippy_ready',params:[]}));
  await page.waitForTimeout(100);
  assert.equal(calls.filter(c=>c==='server.mmu.telemetry.subscribe').length,subscribed);
  assert.equal(await page.locator('#view-mode').inputValue(),'recording');
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
});
