import {ModelRuntime} from '@earendil-works/pi-coding-agent';

// Device authentication keeps tokens on the host. Only the short-lived code
// and OpenAI URL are returned to the researcher's private application.
export class BrowserLogin {
  current: any = null;
  pending: Promise<any> | null = null;
  aborter: AbortController | null = null;
  constructor(public authPath: string) {}
  async start() {
    if (this.current?.status === 'pending' && this.current.expires_at > Date.now()) return this.current;
    if (this.pending) return this.pending;
    this.aborter?.abort();
    const aborter = new AbortController(); this.aborter = aborter;
    let resolveReady!: (value:any)=>void, rejectReady!: (reason:any)=>void;
    this.pending = new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
    void (async()=>{
      try {
        const runtime = await ModelRuntime.create({authPath:this.authPath,refreshOnCreate:false});
        await runtime.login('openai-codex','oauth',{
          signal:AbortSignal.any([aborter.signal,AbortSignal.timeout(900000)]),
          notify:(event:any)=>{
            if(event.type==='device_code') {
              this.current={status:'pending',url:event.verificationUri,user_code:event.userCode,expires_at:Date.now()+event.expiresInSeconds*1000};
              resolveReady(this.current);this.pending=null;
            }
          },
          prompt:async(prompt:any)=>{
            const method=prompt.type==='select' && prompt.options.find((o:any)=>o.label.toLowerCase().includes('device'));
            if(!method) throw Error('Pi does not offer device authentication.');
            return method.id;
          }
        });
        this.current={status:'connected'};
      } catch(error:any) {
        this.current={status:'expired',reason:'Sign-in did not complete. Generate another browser code.'};
        rejectReady(Error('Could not start or complete OpenAI device sign-in. Retry from the agent team panel.'));
      } finally {this.pending=null;}
    })();
    return this.pending;
  }
  close(){this.aborter?.abort();}
}
