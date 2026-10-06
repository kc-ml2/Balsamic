import {expect,test} from '@playwright/test';
import type {Page} from '@playwright/test';

function fixture() {
  const campaign={id:'pi-campaign',version:1,name:'2D grating',autonomy:'delegated',objective:'Implement H12',compute_budget_seconds:100,llm_budget_usd:0};
  return {workspace_id:'pi-workspace',campaign,campaigns:[campaign],tasks:[],trials:[],hypotheses:[],decisions:[],messages:[],events:[],algorithms:[],manager_issues:[],manager_commands:[],research_runs:[],budget:{},
    settings:{llm_configured:true,provider:{configured:true,enabled:true,provider:'pi',model:'gpt-6-astra',billing_mode:'subscription'}},
    agent_runtime:{configuration:{enabled:true,status:'running',control_revision:3,objective:'Implement H12',provider:{configured:true}},
      agents:[{id:'pi',role:'lead',status:'waiting',model:'gpt-6-astra',reasoning_effort:'xhigh',objective:'Implement H12',control_revision:0},
        {id:'child',parent_agent_id:'pi',role:'methodology_specialist',status:'running',model:'gpt-6-sol',reasoning_effort:'xhigh',objective:'Inspect fixed-mask validation',control_revision:2}],
      questions:[{id:'question',status:'pending',question:'Which scientific objective should the pilot use?',reason:'Resolve conflicting objectives.'}],aliases:[]}};
}
async function mock(page:Page,state:any,write:(body:any)=>any) {
  await page.addInitScript(()=>{
    const streams: EventTarget[]=[];
    (window as any).__updateWorkspace=()=>streams.forEach(stream=>stream.dispatchEvent(new MessageEvent('update')));
    (window as any).EventSource=class extends EventTarget {onopen:any;constructor(){super();streams.push(this);setTimeout(()=>this.onopen?.({}),1);}close(){}};
  });
  await page.route('**/api/**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(request.method()==='POST') return route.fulfill(await write(request.postDataJSON()) || {json:{status:'completed',outcome:{}}});
    if(path.includes('/api/v1/commands/')) return route.fulfill({status:404,json:{detail:'Not received'}});
    return route.fulfill({json:state});
  });
}
test('Lead agent questions have a working answer form and expose the parent and child sessions',async({page})=>{
  const state=fixture(),writes:any[]=[];await mock(page,state,body=>{writes.push(body);state.agent_runtime.questions=[];});
  await page.goto('/#notebook');const panel=page.getByRole('region',{name:'Agent team'});
  await expect(panel).toContainText('gpt-6-astra');await expect(panel).toContainText('gpt-6-sol');
  await panel.getByRole('button',{name:'Answer this request'}).click();
  await panel.getByLabel('Your answer to the lead agent').fill('Use mean TE/TM efficiency at order +1.');
  await panel.getByRole('button',{name:'Send to lead agent'}).click();
  await expect(panel).toContainText('Message saved for the lead agent.');
  expect(writes).toHaveLength(1);expect(writes[0]).toMatchObject({operation:'agent.message',payload:{question_id:'question',mode:'steer'}});
});
test('network failure preserves the draft and retries the same command identity',async({page})=>{
  const state=fixture(),writes:any[]=[];await mock(page,state,body=>{writes.push(body);return writes.length===1?{status:503,json:{detail:'Temporary network failure'}}:undefined;});
  await page.goto('/#notebook');const panel=page.getByRole('region',{name:'Agent team'});
  await panel.getByLabel('Message the lead agent').fill('Continue from the saved H12 handoff.');
  await panel.getByRole('button',{name:'Send to lead agent'}).click();
  await expect(panel.getByRole('alert')).toContainText('Temporary network failure');
  await expect(panel.getByLabel('Message the lead agent')).toHaveValue('Continue from the saved H12 handoff.');
  await panel.getByRole('button',{name:'Send to lead agent'}).click();
  await expect(panel.getByLabel('Message the lead agent')).toHaveValue('');
  expect(writes).toHaveLength(2);expect(writes[0].id).toBe(writes[1].id);
});
test('pause and child cancellation pin the current control revision',async({page})=>{
  const state=fixture(),writes:any[]=[];await mock(page,state,body=>{writes.push(body);});
  await page.goto('/#notebook');const panel=page.getByRole('region',{name:'Agent team'});
  await panel.getByRole('button',{name:'Pause agents',exact:true}).click();
  await expect(panel).toContainText('Pause requested');
  await panel.getByRole('button',{name:'Stop',exact:true}).click();
  await expect(panel).toContainText('Stop requested');
  expect(writes[0]).toMatchObject({operation:'agent.control',payload:{action:'pause',expected_control_revision:3}});
  expect(writes[1]).toMatchObject({operation:'agent.control',payload:{agent_id:'child',action:'stop',expected_control_revision:2}});
});

test('agent output renders Markdown in the sidebar and notebook without executing HTML or unsafe links',async({page})=>{
  const state:any=fixture();
  state.messages=[{id:'formatted',agent_id:'pi',origin:'pi',role:'assistant',content:[
    '## H12 progress', '', '**Specification saved** with *independent review* and `MEENT`.', '',
    '1. Read the handoff', '2. Run diagnostics', '   - Check both polarizations', '',
    '> Validation remains pending.', '',
    '[Reference implementation](https://github.com/jLabKAIST/flrl)', '',
    '```python', 'if efficiency < 1:', '    print("<mask>")', '```', '',
    '| Method | Status |', '| --- | --- |', '| H12 | Prepared |', '',
    '<script>window.markdownExecuted = true</script>',
    '<img src="/unexpected-image" onerror="window.markdownExecuted = true">', '',
    '[Unsafe link](javascript:alert%281%29)',
  ].join('\n')}];
  await mock(page,state,()=>undefined);
  await page.goto('/#notebook/conversation');
  for (const body of [page.getByRole('complementary',{name:'Lead agent conversation'}),page.locator('.notebook-conversation')]) {
    await expect(body.getByRole('heading',{name:'H12 progress'})).toHaveCount(1);
    await expect(body.locator('strong').filter({hasText:'Specification saved'})).toHaveCount(1);
    await expect(body.locator('em')).toHaveText('independent review');
    await expect(body.locator('ol > li')).toHaveCount(2);
    await expect(body.locator('ol ul > li')).toHaveText('Check both polarizations');
    await expect(body.locator('blockquote')).toContainText('Validation remains pending.');
    await expect(body.locator('pre code')).toHaveText('if efficiency < 1:\n    print("<mask>")\n');
    await expect(body.getByRole('cell',{name:'Prepared',exact:true})).toHaveCount(1);
    await expect(body.getByRole('link',{name:'Reference implementation'})).toHaveAttribute('href','https://github.com/jLabKAIST/flrl');
    await expect(body.getByRole('link',{name:'Unsafe link'})).toHaveCount(0);
    await expect(body.locator('script, img')).toHaveCount(0);
  }
  expect(await page.evaluate(()=>(window as any).markdownExecuted)).toBeUndefined();
});

for (const viewport of [{width:1440,height:900},{width:390,height:740}]) {
  test(`Lead agent history scrolls within the sidebar and keeps the reading position at ${viewport.width}px`,async({page})=>{
    await page.setViewportSize(viewport);
    const state:any=fixture();
    state.messages=Array.from({length:25},(_,i)=>({id:`message-${i}`,agent_id:'pi',origin:'pi',role:'assistant',
      content:`### Update ${i}\n\nSaved findings for this iteration.\n\n- Check the TE efficiency\n- Check the TM efficiency`}));
    await mock(page,state,()=>undefined);
    await page.goto('/#notebook');
    if(viewport.width<1200) await page.getByRole('button',{name:'Lead agent',exact:true}).click();
    const sidebar=page.getByRole('complementary',{name:'Lead agent conversation'});
    const history=sidebar.getByRole('region',{name:'Lead agent message history'});
    const composer=sidebar.getByLabel('Message to lead agent',{exact:true});
    const gap=()=>history.evaluate(el=>el.scrollHeight-el.clientHeight-el.scrollTop);
    await expect(composer).toBeInViewport();
    await expect(sidebar.getByRole('button',{name:'Close research panel'})).toBeInViewport();
    await expect.poll(()=>history.evaluate(el=>el.scrollHeight>el.clientHeight)).toBe(true);
    await expect.poll(gap).toBeLessThan(2);
    await history.hover();await page.mouse.wheel(0,-700);
    await expect.poll(gap).toBeGreaterThan(500);
    // Wait for wheel scrolling to settle before recording the reading position.
    await history.evaluate(el=>{el.scrollTop=80;el.dispatchEvent(new Event('scroll'));});
    state.messages.push({id:'new-message',agent_id:'pi',origin:'pi',role:'assistant',content:'New diagnostic result.'});
    await page.evaluate(()=>(window as any).__updateWorkspace());
    await expect(history.getByText('New diagnostic result.')).toHaveCount(1);
    await expect.poll(()=>history.evaluate(el=>el.scrollTop)).toBe(80);
    await expect(composer).toBeInViewport();
    await history.evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
    state.messages.push({id:'latest-message',agent_id:'pi',origin:'pi',role:'assistant',content:'Latest diagnostic result.'});
    await page.evaluate(()=>(window as any).__updateWorkspace());
    await expect(history.getByText('Latest diagnostic result.')).toBeInViewport();
    await expect.poll(gap).toBeLessThan(2);
    const bounds=await sidebar.boundingBox();
    expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(viewport.height+1);
    expect(await history.evaluate(el=>el.scrollWidth-el.clientWidth)).toBeLessThanOrEqual(1);
  });
}
