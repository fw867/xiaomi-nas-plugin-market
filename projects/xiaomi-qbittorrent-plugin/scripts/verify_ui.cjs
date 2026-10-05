// Browser-only fixtures; no NAS, Docker, peers, or real downloads are contacted.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// 浏览器选择：默认 chromium，缺它时自动回退到系统 Edge/Chrome
// （很多机器没跑过 `npx playwright install`），也可用 PW_CHANNEL 指定。
async function launchBrowser() {
  const channel = process.env.PW_CHANNEL || '';
  const attempts = channel ? [{channel}] : [{}, {channel: 'msedge'}, {channel: 'chrome'}];
  const problems = [];
  for (const options of attempts) {
    try {
      return await chromium.launch({headless: true, ...options});
    } catch (error) {
      problems.push(`${options.channel || 'chromium'}: ${error.message.split('\n')[0]}`);
    }
  }
  throw new Error('无法启动任何浏览器，可设置 PW_CHANNEL=msedge；详情：\n  ' + problems.join('\n  '));
}

(async () => {
  const browser = await launchBrowser();
  const out = path.join(__dirname,'../test-results'); fs.mkdirSync(out,{recursive:true});
  try {
    for (const width of [1280,768,393,320]) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors = []; page.on('pageerror',e=>errors.push(e.message));
      let configured=false,running=false,loggedIn=false,removed=false;
      let downloadAbs='/nas/pool0/u3943892/data/MiShare';
      // 两个「存储位置」：内置存储池 + 外接设备（U 盘），目录选择弹窗据此切换
      const roots=[{index:0,label:'存储池',path:'/nas/pool0/u3943892/data',exists:true},
                   {index:1,label:'外接设备',path:'/nas/mnt/usb',exists:true}];
      const calls=[];
      const task={hash:'a'.repeat(40),name:'Ubuntu_测试文件_长名称用于验证窗口换行与布局.iso',size:2147483648,progress:.45,state:'downloading',dlspeed:2048000,upspeed:10240};
      await page.route('**/api/**',async route=>{
        const req=route.request(),url=new URL(req.url()),op=url.pathname.replace('/api/','');
        const data=req.method()==='POST'?req.postDataJSON():undefined;calls.push({op,data});
        let body={ok:true};
        if(op==='status') Object.assign(body,{configured,running,loggedIn,busy:false,error:'',directory:configured?'MiShare':'',download_abs:configured?downloadAbs:'',roots,preview:true,address:(configured&&running)?'http://192.168.1.15:18123':'',ready:running});
        else if(op==='browse') {const root=url.searchParams.get('root')||'0';body.items=url.searchParams.get('path')?[]:(root==='1'?[{name:'照片',path:'照片'}]:[{name:'MiShare',path:'MiShare'}]);}
        else if(op==='service/setup'){configured=true;running=true;downloadAbs=data.path;}
        else if(op==='service/reconfigure'){configured=true;running=true;downloadAbs=data.path;body.state={configured,running,download_abs:downloadAbs,roots};}
        else if(op==='service/reset'){configured=false;running=false;}
        else if(op==='service/stop')running=false;
        else if(op==='service/start')running=true;
        else if(op==='login')loggedIn=true;
        else if(op==='logout')loggedIn=false;
        else if(op==='torrents'){body.items=removed?[]:[task];body.transfer={dl_info_speed:2048000,up_info_speed:10240,dl_info_data:102400000};}
        else if(op==='stop')task.state='stoppedDL';
        else if(op==='start')task.state='downloading';
        else if(op==='remove')removed=true;
        else if(op==='limits' && req.method()==='GET')Object.assign(body,{download:0,upload:0,active:2});
        else if(op.startsWith('detail'))Object.assign(body,{files:[{name:task.name,size:task.size,progress:.45}],properties:{total_downloaded:12000,total_uploaded:1000,share_ratio:.08}});
        await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(process.env.QB_TEST_URL || 'http://127.0.0.1:18122/');
      // 1) 从干净的未配置状态走一遍初始化表单：先在第 0 个位置（内置存储池）选目录
      await page.locator('#choose').click();
      // 弹窗顶部显示的是当前浏览位置的完整绝对路径（位置根部就是根自己）
      await page.getByRole('button',{name:'MiShare',exact:true}).waitFor();
      assert.equal(await page.locator('#browsePath').textContent(),roots[0].path);
      await page.getByRole('button',{name:'MiShare',exact:true}).click();
      assert.equal(await page.locator('#browsePath').textContent(),roots[0].path+'/MiShare');
      await page.locator('#selectFolder').click();
      // 「选择此目录」会关掉弹窗，写回表单的是完整绝对路径
      assert.equal(await page.locator('#downloadPath').inputValue(),roots[0].path+'/MiShare');
      // 2) 位置切换：重新打开弹窗切到「外接设备」，浏览路径回到该位置根部
      await page.locator('#choose').click();
      await page.locator('#roots button',{hasText:'外接设备'}).click();
      await page.getByRole('button',{name:'照片',exact:true}).waitFor();
      assert.equal(await page.locator('#browsePath').textContent(),roots[1].path);
      await page.getByRole('button',{name:'照片',exact:true}).click();
      await page.locator('#selectFolder').click();
      assert.equal(await page.locator('#downloadPath').inputValue(),roots[1].path+'/照片');
      await page.locator('#setupForm [name=password]').fill('Example123!');
      await page.locator('#setupForm [type=checkbox]').check();
      await page.locator('#setupForm [type=submit]').click();
      await page.locator('#serviceActions').waitFor();
      assert(calls.some(c=>c.op==='service/setup' && c.data?.path===roots[1].path+'/照片'
        && c.data?.password==='Example123!'),'setup payload');
      // 3) 状态卡片显示完整绝对路径，并带 title（长路径悬停能看全）
      assert.equal(await page.locator('#directory').textContent(),roots[1].path+'/照片');
      assert.equal(await page.locator('#directory').getAttribute('title'),roots[1].path+'/照片');
      await page.screenshot({path:path.join(out,`qa-qb-${width}.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'viewport overflow');
      assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.some(i=>i.getBoundingClientRect().width>0&&(!i.complete||!i.naturalWidth))),false,'broken icon');
      // 4) 控制台：登录、任务列表与各个操作
      await page.locator('#openConsole').click();
      await page.locator('#loginForm [name=password]').fill('Example123!');
      await page.locator('#loginForm [type=submit]').click();
      await page.locator('.task').waitFor();
      await page.screenshot({path:path.join(out,`qa-qb-console-${width}.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'console overflow');
      await page.getByRole('button',{name:'暂停',exact:true}).click();
      await page.getByRole('button',{name:'继续',exact:true}).click();
      await page.locator('.task-name').click();
      await page.locator('#detailDialog[open]').waitFor();
      await page.locator('[data-close=detailDialog]').click();
      await page.locator('#limits').click();
      await page.locator('#limitForm [name=upload]').fill('128');
      await page.locator('#limitForm [type=submit]').click();
      await page.locator('#add').click();
      await page.locator('#addForm textarea').fill('magnet:?xt=urn:btih:'+'b'.repeat(40));
      await page.locator('#addForm [type=submit]').click();
      await page.locator('#addDialog').waitFor({state:'hidden'});
      await page.locator('#add').click();
      await page.locator('#addForm [type=file]').setInputFiles({name:'test.torrent',mimeType:'application/x-bittorrent',buffer:Buffer.from('d4:infodee')});
      await page.locator('#addForm [type=submit]').click();
      await page.locator('#addDialog').waitFor({state:'hidden'});
      await page.getByRole('button',{name:'移除任务，保留文件',exact:true}).click();
      await page.locator('#confirmRemove').click();
      await page.locator('#empty').waitFor();
      assert(calls.some(c=>c.op==='limits' && c.data?.upload===128));
      for(const op of ['magnet','torrent','start','stop','remove'])assert(calls.some(c=>c.op===op),op);
      // 5) 修改目录：确认弹窗要写清后果，选中后重建容器并把状态卡换成新的绝对路径
      await page.locator('#backToStatus').click();
      await page.locator('#reconfigure').click();
      assert.equal(await page.locator('#currentDirectory').textContent(),'当前目录：'+roots[1].path+'/照片');
      const reconfigureText = await page.locator('#reconfigureDialog').textContent();
      assert.match(reconfigureText,/原下载目录里的文件不会被删除或移动/);
      assert.match(reconfigureText,/重建/);
      await page.locator('#chooseNew').click();
      await page.locator('#roots button',{hasText:'存储池'}).click();
      await page.getByRole('button',{name:'MiShare',exact:true}).click();
      await page.locator('#selectFolder').click();
      assert.equal(await page.locator('#reconfigurePath').inputValue(),roots[0].path+'/MiShare');
      await page.locator('#reconfigureForm [type=submit]').click();
      await page.locator('#reconfigureDialog').waitFor({state:'hidden'});
      assert(calls.some(c=>c.op==='service/reconfigure' && c.data?.path===roots[0].path+'/MiShare'
        && c.data?.password===''),'reconfigure payload');
      assert.equal(await page.locator('#directory').textContent(),roots[0].path+'/MiShare');
      assert.equal(await page.locator('#directory').getAttribute('title'),roots[0].path+'/MiShare');
      // 更长的绝对路径同样不能把布局撑出横向滚动
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'long path overflow');
      // 6) 重新初始化：确认弹窗写清「不动下载目录」，确认后回到初始化表单
      await page.locator('#reset').click();
      const resetText = await page.locator('#resetDialog').textContent();
      assert.match(resetText,/下载目录不受影响/);
      assert.match(resetText,/清空/);
      await page.locator('#confirmReset').click();
      await page.locator('#resetDialog').waitFor({state:'hidden'});
      assert(calls.some(c=>c.op==='service/reset' && c.data?.confirm===true),'reset confirmation');
      await page.locator('#setup').waitFor({state:'visible'});
      assert.equal(await page.locator('#serviceActions').isVisible(),false);   // 服务卡随配置一起收起
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: two storage roots + absolute paths in dialog/status/form + setup/login/browse/add/pause/resume/details/limits/remove/reconfigure/reset; no overflow or broken icons`);
      await page.close();
    }
    // 默认部署只设了 LOCAL_ROOT（一个存储位置）：这时不该出现「位置」切换，绝对路径照旧
    const single = await browser.newPage({viewport:{width:393,height:900}});
    const singleErrors = []; single.on('pageerror',e=>singleErrors.push(e.message));
    await single.route('**/api/**',async route=>{
      const op = new URL(route.request().url()).pathname.replace('/api/','');
      let body = {ok:true};
      if (op === 'status') Object.assign(body,{configured:false,running:false,loggedIn:false,busy:false,error:'',
        directory:'',download_abs:'',preview:true,address:'',
        roots:[{index:0,label:'存储池',path:'/nas/pool0/u3943892/data',exists:true}]});
      else if (op === 'browse') body.items = [{name:'MiShare',path:'MiShare'}];
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await single.goto(process.env.QB_TEST_URL || 'http://127.0.0.1:18122/');
    await single.locator('#choose').click();
    await single.getByRole('button',{name:'MiShare',exact:true}).waitFor();
    assert.equal(await single.locator('#roots').isVisible(),false,'只有一个存储位置时不该显示位置切换');
    assert.equal(await single.locator('#browsePath').textContent(),'/nas/pool0/u3943892/data');
    assert.deepEqual(singleErrors,[]);
    console.log('PASS single root: 位置切换隐藏，弹窗顶部仍是完整绝对路径');
    await single.close();
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
