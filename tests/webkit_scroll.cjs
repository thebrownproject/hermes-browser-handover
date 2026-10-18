// Synthetic WebKit viewer -> WebSocket -> remote Chromium gesture acceptance.
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const {webkit,chromium}=require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
async function main(){
 const data=JSON.parse(process.env.HBH_WEBKIT_FIXTURE);const upstream=new URL(data.origin);
 assert.equal(upstream.hostname,'127.0.0.1');
 const targetBrowser=await chromium.connectOverCDP(data.cdp);
 const target=targetBrowser.contexts()[0].pages().find(p=>p.url().includes('/local-demo'));
 const browser=await webkit.launch({headless:true});let proxyOrigin;
 // Loopback synthetic identity proxy: WebKit doesn't copy extra HTTP headers to
 // WebSocket upgrades. Production uses Tailscale Serve instead of this test proxy.
 const headers=req=>{const h={...req.headers,host:upstream.host,'tailscale-user-login':'owner@example.test'};if(h.origin===proxyOrigin)h.origin=data.origin;return h};
 const proxy=http.createServer((req,res)=>{const r=http.request({hostname:'127.0.0.1',port:upstream.port,path:req.url,method:req.method,headers:headers(req)},u=>{res.writeHead(u.statusCode,u.headers);u.pipe(res)});r.on('error',()=>res.destroy());req.pipe(r)});
 const sockets=new Set();proxy.on('connection',s=>{sockets.add(s);s.on('close',()=>sockets.delete(s))});
 proxy.on('upgrade',(req,socket,head)=>{const r=http.request({hostname:'127.0.0.1',port:upstream.port,path:req.url,headers:headers(req)});r.on('upgrade',(res,remote,rest)=>{socket.on('error',()=>remote.destroy());remote.on('error',()=>socket.destroy());socket.write('HTTP/1.1 101 Switching Protocols\r\n'+Object.entries(res.headers).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');if(head.length)remote.write(head);if(rest.length)socket.write(rest);remote.pipe(socket);socket.pipe(remote)});r.on('response',res=>socket.end('HTTP/1.1 '+res.statusCode+' Refused\r\n\r\n'));r.on('error',()=>socket.destroy());r.end()});
 await new Promise(r=>proxy.listen(0,'127.0.0.1',r));proxyOrigin='http://127.0.0.1:'+proxy.address().port;
 const results=[];
 try{
  for(const scheme of ['light','dark']){
   await target.reload();
   const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,colorScheme:scheme});
   const page=await context.newPage();page.setDefaultTimeout(10000);
   try{
    await page.goto(proxyOrigin+'/h/'+data.handover_id);
    await page.waitForFunction(()=>window.__hbhView?.state==='viewing');
    await page.locator('#ctl').click();
    await page.waitForFunction(()=>window.__hbhView?.controller==='you'&&document.querySelector('canvas').width===390);
    async function swipe(){await page.evaluate(async()=>{
     const c=document.querySelector('canvas'),r=c.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height*.8;
     function fire(type,yy){const t={identifier:1,target:c,clientX:x,clientY:yy};const e=new Event(type,{bubbles:true,cancelable:true});Object.defineProperties(e,{touches:{value:type==='touchend'?[]:[t]},changedTouches:{value:[t]}});c.dispatchEvent(e)}
     fire('touchstart',y);for(let i=1;i<=8;i++){fire('touchmove',y-i*30);await new Promise(r=>setTimeout(r,30))}fire('touchend',y-240);
    })}
    await swipe();await target.waitForFunction(()=>scrollY>150);
    const rootScroll=await target.evaluate(()=>scrollY);assert(rootScroll<360,'Gesture duplicated');
    assert.equal(await target.locator('#out').textContent(),'','A swipe must not activate the form button');
    if(process.env.HBH_SCREENSHOT_DIR){fs.mkdirSync(process.env.HBH_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.HBH_SCREENSHOT_DIR,'handover-'+scheme+'.png')})}
    await target.evaluate(()=>{document.body.innerHTML='<div id="panel"><div id="inside">Scrollable modal</div></div>';document.styleSheets[0].insertRule('#panel{position:fixed;inset:0;overflow:auto;background:white}');document.styleSheets[0].insertRule('#inside{height:2400px}')});
    await swipe();await target.waitForFunction(()=>document.getElementById('panel').scrollTop>150);
    const panelScroll=await target.locator('#panel').evaluate(e=>e.scrollTop);assert(panelScroll<360,'Panel gesture duplicated');
    await page.locator('#ctl').click();await page.waitForFunction(()=>window.__hbhView?.controller==='agent');
    assert.equal(await page.locator('#msg').textContent(),'','Routine instruction overlay must stay hidden');
    assert.equal(await target.evaluate(()=>innerWidth),1280);
    if(scheme==='dark'){await page.locator('#fin').click();await page.locator('#fin').click();await page.waitForFunction(()=>document.querySelector('#msg').textContent.includes('Session finished'))}
    results.push({scheme,rootScroll,panelScroll,release:true});
   }finally{await context.close()}
  }
  console.log(JSON.stringify({engine:'WebKit',native_touch_event_path:true,results,finish:true,submitted:false}));
 }finally{await browser.close();for(const s of sockets)s.destroy();await new Promise(r=>proxy.close(r));await targetBrowser.close()}
}
main().catch(e=>{console.error(e.message);process.exitCode=1});
