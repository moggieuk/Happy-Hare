// Optional integration regression: node --test test/hh/viewer/live.browser.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {liveObjects} from './live-fixture.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('live subscriptions remain isolated from file playback',async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 try {
  const page=await browser.newPage();const errors=[],telemetryRequests=[];
  page.on('pageerror',e=>errors.push(e.message));
  const objects=liveObjects();
  Object.assign(objects.mmu,{filament_pos:3,bowden_progress:25});
  let socket,time=1;
  await page.routeWebSocket('**/websocket',ws=>{
    socket=ws;
    ws.onMessage(text=>{
      const request=JSON.parse(text);let result;
      if (request.method==='server.info') result={moonraker_version:'v0.9.3'};
      else if (request.method==='printer.objects.list') result={objects:Object.keys(objects)};
      else if (request.method==='printer.objects.query' || request.method==='printer.objects.subscribe') result={status:objects,eventtime:time};
      else { ws.send(JSON.stringify({id:request.id,error:{message:'Method not found'}}));return; }
      ws.send(JSON.stringify({id:request.id,result}));
    });
  });
  await page.route('http://viewer.test/**',route=>{
    const name=new URL(route.request().url()).pathname.split('/').at(-1) || 'index.html';
    if(name==='telemetry') {telemetryRequests.push(name);return route.fulfill({status:500});}
    return route.fulfill({body:readFileSync(new URL('./'+name,import.meta.url)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://viewer.test/');
  await page.locator('#scene [data-part="espooler"]').first().waitFor();
  assert.match(await page.locator('#mode').textContent(),/^Live view/);
  assert.equal(await page.locator('#previous').isVisible(),false);
  assert.equal(await page.locator('#simulator-controls').isVisible(),false);
  const notify=delta=>socket.send(JSON.stringify({method:'notify_status_update',params:[delta,++time]}));
  notify({mmu:{bowden_progress:60,sensors:{mmu_entry:true,encoder:false,extruder:false,toolhead:false}}});
  await page.waitForTimeout(100);
  assert.deepEqual(telemetryRequests,[]);
  assert.equal(await page.locator('#open-recording').count(),0);
  await page.locator('#view-mode').selectOption('playback');
  assert.equal(await page.locator('#load-telemetry').isVisible(),true);
  await page.locator('#telemetry-file').setInputFiles(new URL('./fixtures/emu.json',import.meta.url).pathname);
  await page.waitForTimeout(100);
  assert.match(await page.locator('#mode').textContent(),/^Telemetry playback/);
  const position=await page.locator('#position').textContent();
  notify({mmu:{filament_pos:10,bowden_progress:-1},extruder:{target:250}});
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#position').textContent(),position);
  await page.locator('#play-step').click();await page.waitForTimeout(100);
  await page.locator('#view-mode').selectOption('live');await page.waitForTimeout(500);
  assert.match(await page.locator('#mode').textContent(),/^Live view/);
  assert.deepEqual(errors,[]);assert.deepEqual(telemetryRequests,[]);
 } finally {await browser.close();}
});
