import { createAgentSession, createExtensionRuntime, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import fs from 'node:fs';
import path from 'node:path';

// Locked mode (no profile): neither host extensions nor repository instructions are
// discovered, and all tools are scoped by the application gateway, not a prompt.
// Dev mode (GRATING_PI_PROFILE names an agent directory): that profile's settings,
// packages, extensions and skills load, so Pi configurations can be tried without
// changing Balsamic. Pi's built-in file/shell tools stay off in both modes.
export const LEGACY_PROVIDER = process.env.GRATING_PI_PROVIDER || 'openai-codex';
export const profile = process.env.GRATING_PI_PROFILE ? path.resolve(process.env.GRATING_PI_PROFILE) : null;
const OVERRIDES = {compaction: {enabled: true, reserveTokens: 16384, keepRecentTokens: 20000},
  retry: {enabled: true, maxRetries: 3, baseDelayMs: 2000}};

async function modelRuntime(authPath: string) {
  return ModelRuntime.create({authPath, refreshOnCreate: false,
    ...(profile ? {modelsPath: path.join(profile, 'models.json')} : {})} as any);
}

export async function createRuntime(options: any) {
  const provider = options.provider || LEGACY_PROVIDER;
  const runtime = await modelRuntime(options.authPath);
  await runtime.refresh({providers: [provider], allowNetwork: false});
  const model = runtime.getModel(provider, options.model);
  if (!model) throw new Error(`Model unavailable: ${provider}/${options.model}`);
  const sessionDir = path.join(options.directory, 'sessions');
  fs.mkdirSync(sessionDir, {recursive: true, mode: 0o700});
  const manager = options.sessionFile ? SessionManager.open(options.sessionFile)
    : SessionManager.create(options.directory, sessionDir);
  let resourceLoader: any, settingsManager: any;
  if (profile) {
    settingsManager = SettingsManager.create(options.directory, profile);
    settingsManager.applyOverrides(OVERRIDES);
    resourceLoader = new DefaultResourceLoader({cwd: options.directory, agentDir: profile, settingsManager,
      systemPrompt: options.instructions, noContextFiles: true});
    await resourceLoader.reload();
  } else {
    settingsManager = SettingsManager.inMemory(OVERRIDES);
    resourceLoader = {
      getExtensions: () => ({extensions: [], errors: [], runtime: createExtensionRuntime()}),
      getSkills: () => ({skills: [], diagnostics: []}), getPrompts: () => ({prompts: [], diagnostics: []}),
      getThemes: () => ({themes: [], diagnostics: []}), getAgentsFiles: () => ({agentsFiles: []}),
      getSystemPrompt: () => options.instructions, getSystemPromptSource: () => undefined,
      getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
      extendResources: () => {}, reload: async () => {},
    };
  }
  const {session, modelFallbackMessage} = await createAgentSession({
    cwd: options.directory, modelRuntime: runtime, model, thinkingLevel: options.effort,
    resourceLoader, customTools: options.tools, sessionManager: manager, settingsManager,
    // Dev profiles may add extension tools; the locked runtime exposes only gateway tools.
    ...(profile ? {noTools: 'builtin'} : {tools: options.tools.map((t: any) => t.name)}),
  } as any);
  if (modelFallbackMessage) { session.dispose(); throw new Error(modelFallbackMessage); }
  const auth = await runtime.checkAuth(provider).catch(() => undefined);
  (session as any).billing = auth?.type === 'oauth' ? 'subscription' : auth?.type === 'api_key' ? 'api' : 'unknown';
  return session;
}

/** Switch an existing session in place; Pi records the change in the session file. */
export async function applySpec(session: any, spec: any) {
  const provider = spec.provider || LEGACY_PROVIDER;
  if (session.model?.provider !== provider || session.model?.id !== spec.model) {
    await session.modelRuntime.refresh({providers: [provider], allowNetwork: false});
    const model = session.modelRuntime.getModel(provider, spec.model);
    if (!model) throw new Error(`Model unavailable: ${provider}/${spec.model}`);
    await session.setModel(model);
    const auth = await session.modelRuntime.checkAuth(provider).catch(() => undefined);
    session.billing = auth?.type === 'oauth' ? 'subscription' : auth?.type === 'api_key' ? 'api' : 'unknown';
  }
  if (spec.effort && session.thinkingLevel !== spec.effort) session.setThinkingLevel(spec.effort);
}

function levels(model: any) {
  if (!model.reasoning) return ['off'];
  const map = model.thinkingLevelMap;
  const all = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  return map ? ['off', ...all.slice(1).filter(level => map[level] !== undefined && map[level] !== null)] : all;
}

export async function authStatus(authPath: string) {
  const runtime = await modelRuntime(authPath);
  // checkAuth reads persisted credentials and the environment, also picking up
  // sign-in through the browser or a separate CLI process.
  const providers: Record<string, any> = {};
  const ids = profile ? runtime.getProviders().map((p: any) => p.id) : [LEGACY_PROVIDER];
  for (const id of ids) {
    const auth = await runtime.checkAuth(id).catch(() => undefined);
    if (auth) providers[id] = {auth: auth.type, billing: auth.type === 'oauth' ? 'subscription' : 'api'};
  }
  const models = Object.keys(providers).flatMap(id => runtime.getModels(id).map((m: any) => ({
    provider: id, id: m.id, name: m.name, reasoning: Boolean(m.reasoning), thinking_levels: levels(m),
    context_window: m.contextWindow, cost: m.cost})));
  let defaults: any = {provider: LEGACY_PROVIDER, model: null, effort: null};
  if (profile) {
    const settings = SettingsManager.create(profile, profile);
    defaults = {provider: settings.getDefaultProvider() || null, model: settings.getDefaultModel() || null,
      effort: settings.getDefaultThinkingLevel() || null};
  }
  return {configured: Object.keys(providers).length > 0, mode: profile ? 'dev' : 'locked',
    provider: defaults.provider, providers, defaults, models};
}
