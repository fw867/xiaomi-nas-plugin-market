// Browser-only fixtures for Transmission Docker config UI.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true});
  const out = path.join(__dirname,'../test-results'); fs.mkdirSync(out,{recursive:true});
  try {
    for (const width of [1280,768,393,320]) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors = []; page.on('pageerror',e=>errors.push(e.message));
      let configured=false,running=false;
      const calls=[];
      await page.route('**/api/**',async route=>{
        const req=route.request(),url=new URL(req.url()),op=url.pathname.replace('/api/','');
        const data=req.method()==='POST'?req.postDataJSON():undefined;calls.push({op,data});
        let body={ok:true};
        if(op==='status') Object.assign(body,{
          configured,running,ready:running,busy:false,error:'',
          download:configured?'Downloads':'',config:configured?'Config':'',watch:configured?'Watch':'',
          username:configured?'admin':'',preview:true,imageVersion:'4.1.3 / LSIO ls362',
          address:(configured&&running)?'http://192.168.1.15:9091':'',
        });
        else if(op==='browse') body.items=url.searchParams.get('path')?[]:[
          {name:'Downloads',path:'Downloads'},{name:'Config',path:'Config'},{name:'Watch',path:'Watch'}
        ];
        else if(op==='service/setup'){configured=true;running=true;}
        else if(op==='service/stop')running=false;
        else if(op==='service/start')running=true;
        await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(process.env.TR_TEST_URL || 'http://127.0.0.1:18140/');
      async function pick(target,name){
        await page.locator(`.choose[data-target=${target}]`).click();
        await page.getByRole('button',{name,exact:true}).click();
        await page.locator('#selectFolder').click();
      }
      await pick('download','Downloads');
      await pick('config','Config');
      await pick('watch','Watch');
      await page.locator('#setupForm [name=username]').fill('admin');
      await page.locator('#setupForm [name=password]').fill('Example123!');
      await page.locator('#setupForm [type=checkbox]').check();
      await page.locator('#setupForm [type=submit]').click();
      await page.locator('#serviceActions').waitFor();
      await page.locator('#access').waitFor();
      await page.screenshot({path:path.join(out,`qa-tr-docker-${width}.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'viewport overflow');
      assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.some(i=>i.getBoundingClientRect().width>0&&(!i.complete||!i.naturalWidth))),false,'broken icon');
      assert(calls.some(c=>c.op==='service/setup' && c.data?.username==='admin' && c.data?.download==='Downloads' && c.data?.config==='Config' && c.data?.watch==='Watch'));
      await page.getByRole('button',{name:'停止服务',exact:true}).click();
      await page.getByRole('button',{name:'启动服务',exact:true}).click();
      assert(calls.some(c=>c.op==='service/stop'));
      assert(calls.some(c=>c.op==='service/start'));
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: three-dir setup + start/stop; no overflow or broken icons`);
      await page.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
