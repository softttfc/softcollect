// Cloudflare Worker 入口

import { createApp } from './routes';
import { KVStorage } from './storage/kv';
import { runAggregation } from './aggregator';
import { DEFAULT_SPEED_TIMEOUT_MS, DEFAULT_SITE_TIMEOUT_MS, DEFAULT_FETCH_TIMEOUT_MS, KV_CRON_INTERVAL, KV_LAST_UPDATE, DEFAULT_CRON_INTERVAL } from './core/config';
import type { AppConfig } from './core/types';

interface CfEnv {
  KV: KVNamespace;
  REFRESH_TOKEN?: string;
  ADMIN_TOKEN?: string;
  SPEED_TIMEOUT_MS?: string;
  SITE_TIMEOUT_MS?: string;
  FETCH_TIMEOUT_MS?: string;
  WORKER_BASE_URL?: string;
}

function buildConfig(env: CfEnv): AppConfig {
  return {
    adminToken: env.ADMIN_TOKEN,
    refreshToken: env.REFRESH_TOKEN,
    speedTimeoutMs: parseInt(env.SPEED_TIMEOUT_MS || '') || DEFAULT_SPEED_TIMEOUT_MS,
    siteTimeoutMs: parseInt(env.SITE_TIMEOUT_MS || '') || DEFAULT_SITE_TIMEOUT_MS,
    fetchTimeoutMs: parseInt(env.FETCH_TIMEOUT_MS || '') || DEFAULT_FETCH_TIMEOUT_MS,
    workerBaseUrl: env.WORKER_BASE_URL || undefined,
  };
}

export default {
  async fetch(request: Request, env: CfEnv, ctx: ExecutionContext): Promise<Response> {
    const storage = new KVStorage(env.KV);
    const config = buildConfig(env);

    const app = createApp({
      storage,
      config,
      triggerRefresh: () => runAggregation(storage, config),
    });

    return app.fetch(request, env, ctx);
  },

  async scheduled(_event: ScheduledEvent, env: CfEnv, ctx: ExecutionContext): Promise<void> {
    const storage = new KVStorage(env.KV);
    const config = buildConfig(env);

    // 间隔检查：wrangler.toml 每小时触发，但按用户配置的间隔决定是否执行
    const intervalRaw = await storage.get(KV_CRON_INTERVAL);
    const intervalMinutes = intervalRaw ? parseInt(intervalRaw) : DEFAULT_CRON_INTERVAL;
    const lastUpdateRaw = await storage.get(KV_LAST_UPDATE);

    if (lastUpdateRaw && !lastUpdateRaw.startsWith('ERROR')) {
      const lastUpdate = new Date(lastUpdateRaw).getTime();
      const elapsed = Date.now() - lastUpdate;
      const intervalMs = intervalMinutes * 60 * 1000;

      if (elapsed < intervalMs) {
        console.log(`[scheduled] Skipping: ${Math.round(elapsed / 60000)}min since last update, interval is ${intervalMinutes}min`);
        return;
      }
    }

    console.log(`[scheduled] Running aggregation (interval: ${intervalMinutes}min)`);
    ctx.waitUntil(runAggregation(storage, config));
  },
};
