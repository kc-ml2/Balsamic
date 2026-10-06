import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {timingSafeEqual} from 'node:crypto';
import {Supervisor} from './supervisor.js';
import {createRuntime, authStatus, applySpec} from './runtime.js';
import {BrowserLogin} from './login.js';

const directory = path.resolve(process.env.GRATING_PI_DIRECTORY || 'runs/pi');
const tokenFile = process.env.GRATING_PI_TOKEN_FILE;
if (!tokenFile) throw new Error('GRATING_PI_TOKEN_FILE is required');
const token = fs.readFileSync(tokenFile, 'utf8').trim();
if (token.length < 32) throw new Error('Invalid harness token');
const backend = process.env.GRATING_PI_WORKSPACE_URL || 'http://127.0.0.1:8765';
const authPath = process.env.GRATING_PI_AUTH_FILE || path.join(os.homedir(), '.pi/agent/auth.json');
const login = new BrowserLogin(authPath);
const supervisor = new Supervisor(directory, createRuntime, async (operation, data, signal) => {
  for (let attempt=0; ; attempt++) {
    try {
      const response = await fetch(`${backend}/api/internal/pi/${operation}`, {method: 'POST',
        headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token}`}, body: JSON.stringify(data),
        signal: AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(120000)])});
      const body: any = await response.json();
      if (response.ok) return body;
      const error:any = new Error(body.detail || `Gateway error ${response.status}`);
      error.definitive = response.status < 500 && response.status !== 429; throw error;
    } catch (error:any) {
      if (signal?.aborted || error.definitive || attempt >= 2) throw error;
      // Stable tool identity reconciles mutations even if their reply was lost.
      await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
    }
  }
}, authPath, async ids => {
  await fetch(`${backend}/api/internal/pi/notify`, {method: 'POST',
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token}`},
    body: JSON.stringify({agent_ids: ids}), signal: AbortSignal.timeout(10000)});
}, applySpec);
function authorized(req: http.IncomingMessage) {
  const supplied = Buffer.from(req.headers.authorization || ''); const expected = Buffer.from(`Bearer ${token}`);
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}
const server = http.createServer(async (req, res) => {
  res.setHeader('Content-Type', 'application/json');
  try {
    const url = new URL(req.url || '/', 'http://localhost');
    if (url.pathname === '/health') { res.end(JSON.stringify({status: 'ok', service: 'grating-pi', harness: 'pi', version: '0.87.1'})); return; }
    if (!authorized(req)) { res.statusCode = 401; res.end('{"detail":"Authentication required"}'); return; }
    if (url.pathname === '/v1/status' && req.method === 'GET') { res.end(JSON.stringify({...await authStatus(authPath),login:login.current})); return; }
    if (url.pathname === '/v1/auth/start' && req.method === 'POST') { res.end(JSON.stringify(await login.start())); return; }
    const match = url.pathname.match(/^\/v1\/agents\/([a-zA-Z0-9_-]+)(?:\/(runs|control))?$/);
    if (!match) { res.statusCode = 404; res.end('{}'); return; }
    if (req.method === 'GET' && !match[2]) { const value = supervisor.view(match[1], Number(url.searchParams.get('after') || 0)); res.statusCode = value ? 200 : 404; res.end(JSON.stringify(value)); return; }
    if (req.method !== 'POST' || !match[2]) { res.statusCode = 405; res.end('{}'); return; }
    let raw = ''; for await (const chunk of req) { raw += chunk; if (Buffer.byteLength(raw) > 2 * 1024 * 1024) throw new Error('Request exceeds transport limit; use evidence references'); }
    const body = JSON.parse(raw);
    const value = match[2] === 'runs' ? await supervisor.submit(match[1], body) : await supervisor.control(match[1], body.action);
    res.end(JSON.stringify(value));
  } catch (e: any) { res.statusCode = 400; res.end(JSON.stringify({detail: String(e.message || e).slice(0, 1500)})); }
});
server.listen(Number(process.env.GRATING_PI_PORT || 8768), '127.0.0.1');
process.on('SIGTERM', async () => { login.close(); await supervisor.close(); server.close(); });
process.on('SIGINT', async () => { login.close(); await supervisor.close(); server.close(); });
