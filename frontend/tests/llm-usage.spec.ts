import {expect,test} from '@playwright/test';
import type {Page} from '@playwright/test';

const totals=(calls:number,input:number,output:number,cache:number,cost:number|null,charged=0)=>({calls,input,output,cache_read:cache,cache_write:0,reasoning:Math.round(output/4),
  total_tokens:input+output+cache,cost_usd:cost,priced_calls:cost===null?0:calls,charged_usd:charged,first_at:'2026-10-06T01:00:00Z',last_at:'2026-10-06T03:10:00Z'});

function state() {
  const campaign={id:'deep',version:1,name:'DeepSeek campaign',autonomy:'delegated',objective:'Optimize',compute_budget_seconds:100,llm_budget_usd:5};
  return {workspace_id:'ws',campaign,campaigns:[campaign],tasks:[],trials:[],hypotheses:[],decisions:[],messages:[],events:[],algorithms:[],manager_issues:[],manager_commands:[],research_runs:[],budget:{},
    settings:{llm_configured:true,provider:{configured:true,enabled:true,provider:'pi',model:'deepseek-v4-pro',billing_mode:'api'}},
    agent_runtime:{configuration:{enabled:true,status:'running',control_revision:0,objective:'Optimize',provider:{configured:true}},
      agents:[{id:'lead_deep',role:'lead',status:'waiting',provider:'deepseek',model:'deepseek-v4-pro',reasoning_effort:'high',objective:'Optimize',control_revision:0},
        {id:'child',parent_agent_id:'lead_deep',role:'results_analyst',status:'running',provider:'deepseek',model:'deepseek-v4-pro',reasoning_effort:null,objective:'Analyze',control_revision:0},
        {id:'impl',parent_agent_id:'lead_deep',role:'implementation_builder',grant_id:'grant',status:'completed',provider:'deepseek',model:'deepseek-v4-pro',reasoning_effort:'high',objective:'Build',control_revision:0}],
      questions:[],aliases:[]}};
}
const usage={family:'deepseek',bucket:'hour',totals:totals(12,9000,3000,27000,0.042,0.042),
  series:[{bucket:'2026-10-06T01:00:00Z',...totals(5,4000,1000,9000,0.015,0.015)},{bucket:'2026-10-06T02:00:00Z',...totals(4,3000,1500,10000,0.018,0.018)},
    {bucket:'2026-10-06T03:00:00Z',...totals(3,2000,500,8000,0.009,0.009)}],
  by_model:[{family:'deepseek',provider:'deepseek',model:'deepseek-v4-pro',billing:'api',...totals(12,9000,3000,27000,0.042,0.042)}],
  by_agent:[{agent_id:'lead_deep',role:'lead',...totals(8,6000,2000,18000,0.03)},{agent_id:'child',role:'results_analyst',...totals(4,3000,1000,9000,0.012)}],
  by_thinking:[{provider:'deepseek',model:'deepseek-v4-pro',thinking_level:'high',...totals(12,9000,3000,27000,0.042)}],
  budget:{cap_usd:5,agent_charged_usd:0.042,implementation_committed_usd:0,spent_usd:0.042},
  agents:{lead_deep:{role:'lead',status:'waiting',provider:'deepseek',model:'deepseek-v4-pro',effort:'high'},child:{role:'results_analyst',status:'running',provider:'deepseek',model:'deepseek-v4-pro',effort:null}}};
const models={mode:'dev',family:'deepseek',default:{provider:'deepseek',model:'deepseek-v4-pro',effort:null},providers:{deepseek:{auth:'api_key',billing:'api'}},
  models:[{provider:'deepseek',id:'deepseek-v4-pro',name:'DeepSeek V4 Pro',thinking_levels:['off','high','max'],family:'deepseek'},
          {provider:'deepseek',id:'deepseek-flash',name:'DeepSeek Flash',thinking_levels:['off','low','high','max'],family:'deepseek'}]};

async function mock(page:Page,commands:any[]) {
  await page.addInitScript(()=>{(window as any).EventSource=class extends EventTarget {onopen:any;constructor(){super();setTimeout(()=>this.onopen?.({}),1);}close(){}};});
  await page.route('**/api/**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(request.method()==='POST'){commands.push(request.postDataJSON());return route.fulfill({json:{status:'completed',outcome:{}}});}
    if(path.includes('/api/v1/commands/')) return route.fulfill({status:404,json:{detail:'Not received'}});
    if(path==='/api/v1/llm-usage') return route.fulfill({json:usage});
    if(path.endsWith('/agents/models')) return route.fulfill({json:models});
    return route.fulfill({json:state()});
  });
}

test('LLM usage shows spend against the cap, a stacked token chart with hover details, and a table view',async({page})=>{
  await mock(page,[]);
  await page.goto('/#llm-usage');
  await expect(page.getByRole('heading',{name:'LLM usage'})).toBeVisible();
  await expect(page.getByRole('meter',{name:'API spend against the cap'})).toBeVisible();
  await expect(page.getByText('of $5.00 campaign cap')).toBeVisible();
  await expect(page.getByText('This campaign is locked to the')).toContainText('deepseek');
  const tokens=page.locator('.llm-chart').filter({hasText:'Tokens per hour'});
  await expect(tokens.locator('.llm-legend')).toContainText('Cache read');
  await tokens.locator('.llm-hit').nth(1).hover();
  await expect(tokens.locator('.llm-tooltip')).toContainText('4 calls');
  await tokens.getByRole('button',{name:'Show table'}).click();
  await expect(tokens.getByRole('table')).toContainText('10K');
  await expect(page.locator('.llm-breakdown').filter({hasText:'By agent'})).toContainText('Results analyst');
});

test('dev mode switches an agent within the campaign family; implementation agents stay frozen',async({page})=>{
  const commands:any[]=[];
  await mock(page,commands);
  await page.goto('/#models');
  await expect(page.getByRole('heading',{name:'Agent models'})).toBeVisible();
  await expect(page.getByText('(frozen with its grant)')).toBeVisible();
  await page.getByLabel('Results analyst model').selectOption('deepseek/deepseek-flash');
  const thinking=page.getByLabel('Results analyst thinking level');
  await expect(thinking.locator('option')).toHaveText(['Model default','off','low','high','max']);
  await thinking.selectOption('max');
  await page.getByRole('row',{name:/Results analyst/}).getByRole('button',{name:'Apply'}).click();
  await expect(page.getByText('The agent switches at its next turn.')).toBeVisible();
  expect(commands.at(-1).operation).toBe('agent.configure');
  expect(commands.at(-1).payload).toEqual({agent_id:'child',model:{provider:'deepseek',model:'deepseek-flash',effort:'max'}});
});
