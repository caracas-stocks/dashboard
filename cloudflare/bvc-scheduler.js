// bvc-scheduler — Cloudflare Worker (cron triggers only, no HTTP endpoint).
//
// GitHub's own `schedule:` trigger is best-effort: with `*/5` it was firing
// only 1–2 times a day. This Worker calls GitHub's workflow_dispatch API on
// Cloudflare's reliable cron instead.
//
// Configuration (Cloudflare dashboard → Workers → bvc-scheduler → Settings):
//   Variables and Secrets
//     GH_REPO   (Text)    caracas-stocks/dashboard
//     GH_TOKEN  (Secret)  fine-grained GitHub token, only this repo,
//                         permission "Actions: Read and write". Never put it
//                         in code, chat or logs.
//   Trigger Events → Cron Triggers (UTC; Caracas = UTC−4)
//     */5 13-17 * * MON-FRI   every 5 min, 9:00 a. m. – 1:55 p. m.
//     0 19,21 * * MON-FRI     3:00 p. m. and 5:00 p. m. (official close reports)

const WORKFLOW = 'update.yml';

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, event.cron));
  },
};

async function dispatch(env, cron) {
  if (!env.GH_TOKEN || !env.GH_REPO || !/^[\w.-]+\/[\w.-]+$/.test(env.GH_REPO)) {
    console.log(JSON.stringify({ event: 'dispatch_skipped', reason: 'missing_or_invalid_config', cron }));
    return;
  }
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'bvc-scheduler',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: 'main' }),
  });
  // 204 = queued. Log only the outcome — never headers or the token.
  console.log(JSON.stringify({ event: 'dispatch', status: res.status, ok: res.status === 204, cron, at: new Date().toISOString() }));
}
