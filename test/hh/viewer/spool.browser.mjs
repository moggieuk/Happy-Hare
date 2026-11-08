// Optional browser regression: node --test test/hh/viewer/spool.browser.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source=readFileSync(new URL('./spool.js',import.meta.url),'utf8');

test('retained spool updates match fresh SVG without replacing geometry',async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH,
    // Software rasterization avoids GPU filter-cache rounding differences.
    args:['--disable-gpu']});
  try {
    const cases=[];
    for (const style of ['cardboard-windowed','dark-windowed','cardboard-solid','dark-solid'])
    for (const fillPercent of [0,45,100]) for (const degrees of [0,45,65,77,78,85,90]) for (const rotation of [0,73]) for (const shadowStyle of ['blur','crisp'])
      cases.push({idPrefix:`spool-${cases.length}`,style,fillPercent,viewAngle:degrees*Math.PI/180,
        rotation,shadowStyle,showFillPercent:true,color:fillPercent===45?'24ba5e80':'24ba5e',arrowOpacity:rotation?.6:0,
        direction:'rewind',glow:rotation?.7:0});
    const pages=[];
    for (const retained of [false,true]) {
      const page=await browser.newPage({viewport:{width:1200,height:Math.ceil(cases.length/8)*130+40}});pages.push(page);
      await page.route('http://spool.test/**',route=>route.fulfill(route.request().url().endsWith('.js')
        ? {body:source,contentType:'text/javascript'}
        : {body:'<body style="margin:0;background:#1b2933"><svg width="1200" height="6000"></svg>',contentType:'text/html'}));
      await page.goto('http://spool.test/');
      await page.evaluate(async({cases,retained})=>{
        const {renderSpool,SpoolRenderer}=await import('/spool.js');
        const svg=document.querySelector('svg');
        const spools=cases.map((options,i)=>{
          const host=document.createElementNS(svg.namespaceURI,'g');
          host.setAttribute('transform',`translate(${70+i%8*150} ${70+Math.floor(i/8)*130}) scale(3)`);
          svg.append(host);
          const renderer=retained?new SpoolRenderer(host):null;
          return {host,renderer,options};
        });
        window.checkPhase=phase=>{
          for (const spool of spools) {
            const {renderer,options}=spool;
            const next=phase===0?options:phase===1?{...options,rotation:123,arrowOpacity:.8,direction:'assist',glow:.4}
              :phase===2?{...options,rotation:0,arrowOpacity:0,glow:0}
              :phase===3?{...options,style:'dark-solid',fillPercent:80,viewAngle:Math.PI/4}
              :{idPrefix:options.idPrefix,style:'dark-solid',color:options.color,fillPercent:80,viewAngle:Math.PI/4,showFillPercent:true};
            if (!renderer) {spool.host.innerHTML=renderSpool(next);continue;}
            const first=spool.host.firstChild;
            renderer.update(next);
            if (phase===1 || phase===2) {
              if (first!==spool.host.firstChild) throw Error('Motion rebuilt geometry');
              const observer=new MutationObserver(()=>{});
              observer.observe(spool.host,{subtree:true,attributes:true,childList:true});
              renderer.update(next);
              if (observer.takeRecords().length) throw Error('Identical props mutated the SVG');
              observer.disconnect();
            }
            // Simulate the legacy viewer rebuilding the surrounding scene.
            const replacement=spool.host.cloneNode(false), geometry=spool.host.firstChild;
            spool.host.replaceWith(replacement);renderer.mount(replacement);spool.host=replacement;
            if (replacement.firstChild!==geometry) throw Error('Remount replaced geometry');
          }
        };
      },{cases,retained});
    }
    for (let phase=0;phase<5;phase++) {
      for (const page of pages) await page.evaluate(phase=>window.checkPhase(phase),phase);
      assert.ok((await pages[1].screenshot()).equals(await pages[0].screenshot()),`phase ${phase}`);
    }
  } finally {await browser.close();}
});
