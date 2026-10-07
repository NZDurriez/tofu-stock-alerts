// Mr Tofu stock watcher on Cloudflare Workers.
// Runs every minute (cron trigger), compares the shop's product list with the
// last snapshot in KV, and posts Discord alerts for new listings, restocks and
// newly-buyable options. Same behaviour as check_stock.py on GitHub Actions.
//
// Also answers Discord slash commands at /interactions:
//   /password <pw>  log in behind the shop's password page and keep watching there (owner only)
//   /scan           check right now        /instock  list what's buyable (owner only)
//   /status         what the watcher sees right now (owner only)
//   /wishlist ...   anyone in the server: keep a wishlist; pings go to their own private
//                   channel (made the first time). Drop mode on the owner's PC watches the
//                   wishlists (it reads them from /wishlists with DROP_MODE_KEY) and pings.
//   /wishlist-panel posts buttons for it (Add something / My wishlist, with a pop-up form),
//                   so people can click instead of typing commands (owner only)
// Drop mode can change someone's wishlist too (POST /wishlists/<their id>, with DROP_MODE_KEY);
// they get a note in their channel saying what changed.

const SHOP = "https://mrtofu.store";
const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36";
const OUTAGE_WARN_MS = 15 * 60 * 1000;
// Discord's API (DISCORD_API_BASE can point at a pretend Discord when testing locally)
const apiBase = (env) => (env.DISCORD_API_BASE || "https://discord.com/api").replace(/\/$/, "");
const discordApi = (env) => `${apiBase(env)}/v10`;
const EPHEMERAL = 64;

const LABELS = {
  new: ["🆕 New in the shop", 0xf5a524],
  "new-soon": ["🆕 New listing (not on sale yet)", 0xa78bfa],
  "new-soldout": ["🆕 New listing (sold out)", 0x9ca3af],
  restock: ["🔁 Back in stock", 0x22c55e],
  option: ["➕ New option available", 0x38bdf8],
};

// /instock filters: friendly name -> the shop's collections. Anything that
// isn't one of these is treated as words to search product names for.
const FILTERS = [
  { name: "🎥 Mr Tofu Live Stream", value: "live", handles: ["mr-tofu-live-stream"], aliases: ["live", "stream", "livestream", "live stream", "tofu", "breaks", "break"] },
  { name: "Pokémon (all)", value: "pokemon", handles: ["pokemon-tcg", "pokemon-tcg-japanese", "pokemon-tcg-single-cards"], aliases: ["pokemon", "pkmn", "poke", "pokemon tcg"] },
  { name: "Pokémon Japanese", value: "pokemon japanese", handles: ["pokemon-tcg-japanese"], aliases: ["pokemon japanese", "japanese", "jp", "pokemon jp"] },
  { name: "One Piece", value: "one piece", handles: ["one-piece-tcg", "one-piece-tcg-single-cards"], aliases: ["one piece", "onepiece", "op", "one piece tcg"] },
  { name: "Magic: The Gathering", value: "magic", handles: ["magic-the-gathering"], aliases: ["magic", "mtg", "magic the gathering"] },
  { name: "Final Fantasy", value: "final fantasy", handles: ["final-fantasy-tcg", "final-fantasy-singles"], aliases: ["final fantasy", "ff", "fftcg"] },
  { name: "Riftbound (League of Legends)", value: "riftbound", handles: ["riftbound-league-of-legends-tcg"], aliases: ["riftbound", "lol", "league", "league of legends"] },
  { name: "Dragon Ball Super", value: "dragon ball", handles: ["dragon-ball-super-fusion-world"], aliases: ["dragon ball", "dragonball", "dbs", "fusion world"] },
  { name: "Gundam", value: "gundam", handles: ["gundam-card-game"], aliases: ["gundam"] },
  { name: "Weiss Schwarz", value: "weiss schwarz", handles: ["weiss-schwarz"], aliases: ["weiss schwarz", "weiss", "ws"] },
  { name: "Accessories", value: "accessories", handles: ["accessories"], aliases: ["accessories", "sleeves", "toploaders", "binders"] },
  { name: "Store events & tournaments", value: "events", handles: ["store-events-tournaments"], aliases: ["events", "event", "tournaments", "tournament", "prerelease", "pre-release"] },
];

// Lowercase and strip accents so "pokemon" matches "Pokémon"
const norm = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

function resolveFilter(text) {
  const t = norm(text).replace(/\s+/g, " ");
  if (!t) return null;
  const f = FILTERS.find((x) => norm(x.value) === t || x.aliases.some((a) => norm(a) === t));
  return f ? { label: f.name, handles: f.handles } : { label: `“${text.trim()}”`, words: t.split(" ") };
}

// Bump when the command list changes; the cron re-registers them once.
const COMMANDS_VERSION = 7;
const COMMANDS = [
  {
    name: "password",
    description: "Log in behind Mr Tofu's shop password so I can keep watching",
    options: [{ type: 3, name: "password", description: "The shop password", required: true }],
  },
  { name: "scan", description: "Check Mr Tofu's shop right now" },
  {
    name: "instock",
    description: "List what's buyable in Mr Tofu's shop right now",
    options: [{
      type: 3,
      name: "filter",
      description: "e.g. live, pokemon, one piece, magic, or any words like delta reign",
      required: false,
      autocomplete: true,
    }],
  },
  { name: "status", description: "What the stock watcher can see right now" },
  {
    name: "dropmode",
    description: "Start, stop or check drop mode on your PC",
    default_member_permissions: "32", // (only people who can manage the server see it; only the owner can use it)
    options: [{
      type: 3, name: "do", description: "What to do", required: true,
      choices: [
        { name: "start (open it and start watching)", value: "start" },
        { name: "stop (close it)", value: "stop" },
        { name: "status", value: "status" },
      ],
    }],
  },
  { name: "speed", description: "Drop mode's controls: start, stop, and each shop's check speed", default_member_permissions: "32" },
  {
    name: "dropmode-panel",
    description: "Post drop mode's controls in this channel (only you can use them; it keeps itself up to date)",
    default_member_permissions: "32",
    contexts: [0],
  },
  {
    name: "wishlist",
    description: "Your own wishlist: get pinged in your private channel when something's in stock",
    options: [
      {
        type: 1, name: "add", description: "Watch for something (every word has to be in the product's name)",
        options: [
          { type: 3, name: "keywords", description: "e.g. focused fighters   (a - before a word leaves it out: booster bundle -tin)", required: true, max_length: 100 },
          { type: 4, name: "quantity", description: "How many to put in the checkout link (1-5, normally 1)", required: false, min_value: 1, max_value: 5 },
          { type: 10, name: "max_price", description: "Don't ping me if it costs more than this (each)", required: false, min_value: 0 },
        ],
      },
      { type: 1, name: "list", description: "See your wishlist" },
      {
        type: 1, name: "remove", description: "Take something off your wishlist",
        options: [{ type: 3, name: "item", description: "Which one", required: true, autocomplete: true }],
      },
      { type: 1, name: "clear", description: "Empty your wishlist" },
    ],
  },
  {
    name: "wishlist-panel",
    description: "Post the wishlist buttons in this channel, for everyone to use",
    default_member_permissions: "32", // (only people who can manage the server see it)
    contexts: [0],
  },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// KV reads can be up to a minute stale at the edge, so within one run keep
// our own writes in memory and read those first. Stops repeat alerts between
// the several checks a run makes. What's read is kept too (a run lasts a minute),
// so checking every few seconds stays well inside the free plan's daily KV reads.
function withMemory(kv) {
  const mem = new Map();
  return {
    get: async (k) => {
      if (!mem.has(k)) mem.set(k, await kv.get(k));
      return mem.get(k);
    },
    put: async (k, v) => { mem.set(k, v); await kv.put(k, v); },
    delete: async (k) => { mem.set(k, null); await kv.delete(k); },
  };
}

export default {
  // Cron fires once a minute; spread CHECKS_PER_MINUTE checks across it.
  // Unchanged checks are cheap (fingerprint only), so this fits the free plan.
  async scheduled(event, env, ctx) {
    const run = { ...env, STATE: withMemory(env.STATE) };
    ctx.waitUntil((async () => {
      await registerCommands(run);
      try {
        await watchDropMode(run);
        await refreshPanel(run);
      } catch (err) {
        console.log("Couldn't look for drop mode:", String(err));
      }
      const n = Math.max(1, Math.min(12, parseInt(env.CHECKS_PER_MINUTE || "4", 10) || 4));
      const start = Date.now();
      for (let k = 0; k < n; k++) {
        const wait = start + (k * 60000) / n - Date.now();
        if (wait > 0) await sleep(wait);
        try {
          await runCheck(run);
        } catch (err) {
          console.log("Check crashed:", String(err));
        }
      }
    })());
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/interactions" && request.method === "POST") return handleInteraction(request, env, ctx);
    if (url.pathname === "/wishlists" && request.method === "GET") return wishlistFeed(request, env);
    const edit = url.pathname.match(/^\/wishlists\/(\d{15,25})$/);
    if (edit && request.method === "POST") return wishlistEdit(request, env, ctx, edit[1]);
    if (url.pathname === "/test-alert" && request.method === "POST") return testAlert(request, env);
    if (url.pathname === "/dropmode/poll" || url.pathname === "/dropmode/done") return dropModeHelper(request, env, url);
    // Tiny status page; nothing secret in it
    const meta = await getMeta(env);
    return new Response(statusLines(meta).join("\n"), { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};

async function getMeta(env) {
  return JSON.parse((await env.STATE.get("meta")) || "{}");
}

function statusLines(meta) {
  return [
    "Mr Tofu stock watch is running.",
    `Last change seen: ${meta.lastChange || "none yet"}`,
    meta.locked
      ? `🔒 Shop password-locked since: ${meta.locked}` +
        (meta.behindLock ? " (watching behind the lock with your password)" : " (watching for it to reopen)")
      : meta.failingSince
        ? `Having trouble since: ${meta.failingSince}`
        : "Checks: OK",
  ];
}

// Returns a short summary for /scan; alerts go to the webhook as usual.
// opts.force skips the "nothing changed" shortcut (used by /scan).
async function runCheck(env, opts = {}) {
  const meta = await getMeta(env);
  const ping = (env.DISCORD_PING || "").trim();
  const cookie = await env.STATE.get("cookie");
  const storedHash = await env.STATE.get("hash");
  const etags = JSON.parse((await env.STATE.get("etags")) || "{}"); // { pub, pw }
  const fetchOpts = (mode) => ({
    pages: meta.pageCount || 1,
    skipHash: opts.force ? null : storedHash,
    etag: opts.force ? null : etags[mode],
  });
  let products;
  try {
    products = await fetchProducts(null, fetchOpts("pub"));
  } catch (err) {
    if (!(err && err.locked)) {
      await recordFailure(env, meta, err);
      return err && err.busy ? "⏳ The shop is busy right now (it asked me to slow down); the next check will retry." : `Couldn't check the shop: ${err}`;
    }
    // Shop switched to its password page (usually while a drop is being set up)
    if (!meta.locked) {
      meta.locked = new Date().toISOString();
      delete meta.failingSince;
      delete meta.warned;
      await env.STATE.put("meta", JSON.stringify(meta));
      let msg = `${ping} 🔒 **Mr Tofu's shop just went password-locked.** That often means a drop is being set up. I'll ping you the second it reopens.`;
      if (!cookie) msg += "\nKnow the password? Use `/password` and I'll keep watching behind the lock.";
      await notify(env, msg.trim());
    }
    if (!cookie) return "🔒 The shop is password-locked. Use /password if you know it.";
    try {
      products = await fetchProducts(cookie, fetchOpts("pw"));
    } catch (e2) {
      if (e2 && e2.locked) {
        // Saved password stopped working (it was changed)
        await env.STATE.delete("cookie");
        delete meta.behindLock;
        await env.STATE.put("meta", JSON.stringify(meta));
        await notify(env, `${ping} 🔑 The shop password I had stopped working (it was probably changed). Send the new one with \`/password\` if you know it.`.trim());
        return "🔑 The saved password no longer works.";
      }
      await recordFailure(env, meta, e2);
      return e2 && e2.busy ? "⏳ The shop is busy right now (it asked me to slow down); the next check will retry." : `Couldn't check the shop: ${e2}`;
    }
    if (!meta.behindLock) {
      meta.behindLock = new Date().toISOString();
      await env.STATE.put("meta", JSON.stringify(meta));
      await notify(env, "👀 I'm in behind the password: watching the locked shop. Anything Tofu adds will show up here (you'll need the password in your browser to buy until it opens).");
    }
  }

  let metaChanged = false;
  if (meta.locked && !products.viaPassword) {
    await notify(env, `${ping} 🔓 **MR TOFU'S SHOP IS OPEN AGAIN!** ${SHOP}\nAnything new will follow below with ATC / ⚡ buttons.`.trim());
    delete meta.locked;
    delete meta.behindLock;
    metaChanged = true;
  }
  if (meta.failingSince) {
    if (meta.warned) await notify(env, "✅ Stock watch is back: checks are working again.");
    delete meta.failingSince;
    delete meta.warned;
    metaChanged = true;
  }
  if (metaChanged) await env.STATE.put("meta", JSON.stringify(meta));

  const via = products.viaPassword ? " behind the password" : "";
  const mode = products.viaPassword ? "pw" : "pub";
  // Saved only after alerts and the snapshot succeed, so a failed send is retried
  const saveEtag = async () => {
    if (products.etag && products.etag !== etags[mode]) {
      etags[mode] = products.etag;
      await env.STATE.put("etags", JSON.stringify(etags));
    }
  };
  if (products.unchanged) {
    await saveEtag();
    return `Checked${via}: nothing has changed.`;
  }

  const current = {};
  for (const p of products) current[p.id] = summarise(p);
  const rawPrev = await env.STATE.get("state");
  const firstRun = rawPrev === null;
  const previous = firstRun ? {} : JSON.parse(rawPrev);

  const kw = keywords(env);
  const found = firstRun ? [] : diff(previous, current, kw);
  if (found.length) {
    const lockNote = products.viaPassword ? "🔒 Shop is still password-locked: enter the password on the site to buy." : null;
    await send(env, alertMessages(found, kw, lockNote), found.some(([, i]) => watched(i, kw)));
  }

  // Only write when something changed: KV's free plan allows 1,000 writes/day
  const snap = JSON.stringify(compact(current));
  if (snap !== rawPrev) {
    await env.STATE.put("state", snap);
    meta.lastChange = new Date().toISOString();
  }
  if (snap !== rawPrev || products.pageCount !== (meta.pageCount || 1)) {
    meta.pageCount = products.pageCount;
    await env.STATE.put("meta", JSON.stringify(meta));
  }
  if (products.hash !== storedHash) await env.STATE.put("hash", products.hash);
  await saveEtag();
  const inStock = Object.values(current).filter((i) => i.available).length;
  return (
    `Checked ${Object.keys(current).length} products (${inStock} in stock)${via}: ` +
    (found.length ? `${found.length} alert${found.length > 1 ? "s" : ""} sent to the channel.` : "nothing new.")
  );
}

// The helper on the owner's PC (drop_mode_helper.py, with DROP_MODE_KEY): every few seconds it says how drop
// mode is doing and picks up a /dropmode command; then it says how that went.
async function dropModeHelper(request, env, url) {
  const key = (env.DROP_MODE_KEY || "").trim();
  if (!key) return new Response("Drop mode's key isn't set up on the bot yet.", { status: 503 });
  if (!sameText(request.headers.get("Authorization") || "", `Bearer ${key}`)) return new Response("forbidden", { status: 403 });
  if (url.pathname === "/dropmode/poll") {
    return request.method === "POST"
      ? wishlistStore(env).fetch("https://wishlists/dm/poll", { method: "POST", body: await request.text() })
      : wishlistStore(env).fetch("https://wishlists/dm/poll" + url.search);
  }
  if (request.method !== "POST") return new Response("POST it", { status: 405 });
  return wishlistStore(env).fetch("https://wishlists/dm/done", { method: "POST", body: await request.text() });
}

// Drop mode's controls (/speed, and the panel /dropmode-panel posts): how it is, start and close it, and each
// shop's speed and watching, with buttons for one shop at a time. Only the owner can use the buttons.
const SPEEDS = [0.5, 1, 2, 3, 5, 10];
const every = (n) => (n === 0.5 ? "½s" : n ? `${n}s` : "? (restart drop mode to show it)");
function controlPanel(st, sel, note) {
  const h = st.helper || {}, fresh = !!h.at && Date.now() - h.at < 45000, shops = h.shops || [];
  const shop = shops.find((s) => s.id === sel) || shops[0];
  const on = shops.filter((s) => s.running);
  let status;
  if (!h.at) status = "⚪ The helper on your PC hasn't checked in yet (it starts when you log in to Windows).";
  else if (!fresh) status = `🔴 Can't reach your PC: its helper last checked in <t:${Math.floor(h.at / 1000)}:R>. Is it on?`;
  else if (!h.running) status = "⚪ **Closed**";
  else status = on.length ? "🟢 **Running and watching**" : "🟡 **Open, but not watching**";
  let body = status;
  if (fresh && h.running && shops.length) {
    body += "\n" + shops.map((s) => `${s.running ? "🟢" : "⚪"} ${s.name} · ${s.running ? `every ${every(s.interval)}` : `not watching${s.interval ? ` (set to ${every(s.interval)})` : ""}`}`).join("\n");
    if (shops.length > 1 && shop) body += `\n\nThe speed buttons are for **${shop.name}**.`;
  }
  const button = (style, label, emoji, custom_id) => ({ type: 2, style, label, emoji: { name: emoji }, custom_id });
  const components = [];
  const reach = fresh && h.at;
  components.push(row(
    ...(reach && !(h.running && on.length) ? [button(3, h.running ? "Start watching" : "Start drop mode", "▶️", "dm:start")] : []),
    ...(reach && h.running ? [button(4, "Close drop mode", "⏹️", "dm:stop")] : []),
    button(2, "Refresh", "🔄", `dm:refresh:${(shop || {}).id || ""}`)));
  if (reach && h.running && shop) {
    if (shops.length > 1) components.push(row({ type: 3, custom_id: "dm:shop", options: shops.map((s) => ({ label: s.name, value: s.id, default: s.id === shop.id })) }));
    const speed = (n) => ({ type: 2, style: shop.interval === n ? 3 : 2, label: every(n), custom_id: `dm:speed:${shop.id}:${n}` });
    components.push(row(...SPEEDS.slice(0, 5).map(speed)));
    components.push(row(speed(10), shop.running ? button(2, `Stop watching ${shop.name}`.slice(0, 80), "⏸️", `dm:unwatch:${shop.id}`)
      : button(1, `Watch ${shop.name}`.slice(0, 80), "▶️", `dm:watch:${shop.id}`)));
  }
  return { content: "🎛️ **Drop mode**\n" + body + (note ? `\n\n${note}` : ""), components, allowed_mentions: { parse: [] } };
}
const panelSig = (panel) => JSON.stringify([panel.content, panel.components]);

// A click on the controls: pick a shop, refresh, or ask the helper to start or close drop mode, change a speed,
// or start or stop watching a shop. (On the posted panel, the shop picked is remembered for its updates.)
async function controlClick(i, env, ctx, followUp) {
  const [, what, sid, n] = String(i.data.custom_id).split(":");
  const st = await dropModeState(env), h = st.helper || {};
  const posted = st.panel && i.message && i.message.id === st.panel.message;
  const show = async (panel, sel) => { // (and keep the posted panel's record in step)
    if (posted) await wishlistStore(env).fetch("https://wishlists/dm/panel", { method: "POST", body: JSON.stringify({ sel, last: panelSig(panel) }) });
    return panel;
  };
  if (what === "shop") {
    const sel = (i.data.values || [])[0];
    return reply({ type: 7, data: await show(controlPanel(st, sel), sel) });
  }
  const sel = sid || (posted && st.panel.sel) || "tofu";
  if (what === "refresh" || !h.at || Date.now() - h.at > 45000) return reply({ type: 7, data: await show(controlPanel(st, sel), sel) });
  const job = await (await wishlistStore(env).fetch("https://wishlists/dm/request", {
    method: "POST", body: JSON.stringify({ action: what, shop: sid, interval: what === "speed" ? Number(n) : undefined }) })).json();
  ctx.waitUntil(followUp(async () => {
    for (let k = 0; k < 13; k++) { // (the helper asks every few seconds; opening drop mode takes a few more)
      await sleep(2000);
      const s = await dropModeState(env);
      if (s.result && s.result.id === job.id) return show(controlPanel(s, sel, `${s.result.ok ? "✅" : "⚠️"} ${s.result.message} · <t:${Math.floor(Date.now() / 1000)}:T>`), sel);
    }
    return show(controlPanel(await dropModeState(env), sel, "⏳ Sent to your PC, but it hasn't answered yet. Press Refresh in a moment."), sel);
  }));
  return reply({ type: 6 }); // (the panel's edited when the helper answers)
}

// The posted panel keeps itself up to date: each minute, if what it would show has changed, it's edited.
// (A note from a click stays until then.) A deleted panel is forgotten.
async function refreshPanel(env) {
  const st = await dropModeState(env);
  if (!st.panel || !st.panel.message) return;
  const panel = controlPanel(st, st.panel.sel), sig = panelSig(panel);
  if (sig === st.panel.last) return;
  const [s] = await bot(env, "PATCH", `/channels/${st.panel.channel}/messages/${st.panel.message}`, panel);
  if (s === 404) await wishlistStore(env).fetch("https://wishlists/dm/panel", { method: "POST", body: JSON.stringify({ clear: true }) });
  else if (s < 300) await wishlistStore(env).fetch("https://wishlists/dm/panel", { method: "POST", body: JSON.stringify({ last: sig }) });
}

const dropModeState = async (env) => (await (await wishlistStore(env).fetch("https://wishlists/dm/state")).json()) || {};

// Drop mode on the owner's PC reads the wishlists every few seconds while it's running. If that stops for a
// few minutes (DROP_MODE_QUIET_SECONDS, normally 3 minutes), say so in the stock-alerts channel, once, with
// a ping, and again when it's back.
async function watchDropMode(env) {
  if (!env.WISHLISTS || !(env.DROP_MODE_KEY || "").trim()) return;
  const seen = ((await (await wishlistStore(env).fetch("https://wishlists/seen")).json()) || {}).seen || 0;
  if (!seen) return; // (it's never connected)
  const meta = await getMeta(env), now = Date.now();
  const quiet = now - seen > (parseInt(env.DROP_MODE_QUIET_SECONDS || "180", 10) || 180) * 1000;
  const at = Math.floor(seen / 1000), ping = (env.DISCORD_PING || "").trim();
  if (quiet && !meta.dropModeDown) {
    if (((await dropModeState(env)).stoppedAt || 0) >= seen - 60000) return; // (closed with /dropmode stop: not news)
    meta.dropModeDown = seen;
    await env.STATE.put("meta", JSON.stringify(meta));
    await notify(env, `${ping} ⚠️ **Drop mode has stopped.** It last checked in at <t:${at}:t> (<t:${at}:R>), so wishlist pings and checkouts on your PC aren't happening. I'll say here when it's back.`.trim());
  } else if (!quiet && meta.dropModeDown) {
    const mins = Math.max(1, Math.round((now - meta.dropModeDown) / 60000));
    delete meta.dropModeDown;
    await env.STATE.put("meta", JSON.stringify(meta));
    await notify(env, `✅ Drop mode is back (it was off for about ${mins} minute${mins === 1 ? "" : "s"}).`);
  }
}

async function recordFailure(env, meta, err) {
  console.log("Check failed:", String(err));
  if (err && err.busy) return; // rate-limited this minute; just try again next minute
  const now = Date.now();
  if (!meta.failingSince) {
    meta.failingSince = new Date(now).toISOString();
    await env.STATE.put("meta", JSON.stringify(meta));
  } else if (!meta.warned && now - Date.parse(meta.failingSince) > OUTAGE_WARN_MS) {
    await notify(env, "⚠️ Stock watch hasn't been able to check the shop for 15 minutes. It keeps retrying and will post here when it recovers.");
    meta.warned = true;
    await env.STATE.put("meta", JSON.stringify(meta));
  }
}

// Shopify rate-limits by IP, and Cloudflare's outgoing IPs are shared with
// lots of other sites, so a "slow down" reply is often someone else's traffic.
// Retrying after a short random pause usually goes out on a different IP.
async function shopFetch(url, init, tries = 3) {
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(url, init);
    if (![429, 430, 503].includes(res.status) || attempt >= tries) return res;
    await sleep(1000 + Math.random() * 3000);
  }
}

// With an etag, Shopify answers 304 (no body) when nothing changed; then this
// returns null. Otherwise returns the page body.
async function fetchPage(page, cookie, etag, out) {
  const headers = { "User-Agent": BROWSER_UA, Accept: "application/json", "Cache-Control": "no-cache" };
  if (cookie) headers.Cookie = cookie;
  if (etag) headers["If-None-Match"] = etag;
  const res = await shopFetch(`${SHOP}/products.json?limit=250&page=${page}&_=${Date.now()}`, {
    headers,
    cf: { cacheTtl: 0, cacheEverything: false },
  });
  if ([429, 430, 503].includes(res.status)) throw Object.assign(new Error(`HTTP ${res.status}`), { busy: true });
  if (res.status === 401) {
    // Shopify only answers products.json with 401 when the storefront is behind
    // its password page (checking the homepage redirect as well proved flaky)
    throw Object.assign(new Error(cookie ? "Shop password no longer accepted" : "Shop is password-locked"), { locked: true });
  }
  if (res.status === 304) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  if (out) out.etag = res.headers.get("etag");
  return res.arrayBuffer();
}

const parsePage = (buf) => JSON.parse(new TextDecoder().decode(buf)).products || [];

// Fetches the product list. Returns { unchanged: true } without downloading
// or decoding anything when the shop's data hasn't changed: first via the
// page's etag (Shopify replies 304 with no body), then via opts.skipHash.
async function fetchProducts(cookie, opts = {}) {
  const pages = Math.max(1, opts.pages || 1);
  const viaPassword = !!cookie;
  const tag = {};
  const bufs = [];
  // Conditional request only for the usual single-page catalogue
  const first = await fetchPage(1, cookie, pages === 1 ? opts.etag : null, tag);
  if (first === null) return { unchanged: true, viaPassword, etag: opts.etag };
  bufs.push(first);
  for (let p = 2; p <= pages; p++) bufs.push(await fetchPage(p, cookie));
  const joined = new Uint8Array(bufs.reduce((n, b) => n + b.byteLength, 0));
  let at = 0;
  for (const b of bufs) { joined.set(new Uint8Array(b), at); at += b.byteLength; }
  const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", joined))].map((x) => x.toString(16).padStart(2, "0")).join("");
  if (opts.skipHash && hash === opts.skipHash) return { unchanged: true, hash, viaPassword, etag: tag.etag };

  const all = [];
  let last = [];
  for (const b of bufs) { last = parsePage(b); all.push(...last); }
  let page = pages;
  while (last.length === 250) { // catalogue grew past what we fetched
    last = parsePage(await fetchPage(++page, cookie));
    all.push(...last);
  }
  if (!all.length) throw new Error("Shop returned no products");
  return Object.assign(all, { viaPassword, hash, pageCount: page, etag: tag.etag });
}

// Products in one or more of the shop's collections (deduplicated). Missing
// collections (renamed/removed) are skipped rather than failing the command.
async function fetchCollections(handles, cookie) {
  const seen = new Map();
  for (const h of handles) {
    const headers = { "User-Agent": BROWSER_UA, Accept: "application/json" };
    if (cookie) headers.Cookie = cookie;
    const res = await shopFetch(`${SHOP}/collections/${h}/products.json?limit=250&_=${Date.now()}`, { headers, cf: { cacheTtl: 0 } }, 4);
    if ([429, 430, 503].includes(res.status)) throw Object.assign(new Error(`HTTP ${res.status}`), { busy: true });
    if (res.status === 401) throw Object.assign(new Error("Shop is password-locked"), { locked: true });
    if (res.status === 404) continue;
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    for (const p of (await res.json()).products || []) seen.set(p.id, p);
  }
  return [...seen.values()];
}

// Submit the storefront password form like a browser would; returns the
// session cookie if it lets us see the products, otherwise null.
async function loginWithPassword(password) {
  const res = await fetch(`${SHOP}/password`, {
    method: "POST",
    redirect: "manual",
    headers: { "User-Agent": BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ form_type: "storefront_password", utf8: "✓", password }).toString(),
  });
  const raw = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [res.headers.get("set-cookie") || ""];
  const cookie = raw.filter(Boolean).map((c) => c.split(";")[0]).join("; ");
  if (!cookie) return null;
  const test = await fetch(`${SHOP}/products.json?limit=1&_=${Date.now()}`, { headers: { "User-Agent": BROWSER_UA, Cookie: cookie } });
  return test.ok ? cookie : null;
}

// ---------- Discord slash commands ----------

async function registerCommands(env) {
  if (!env.DISCORD_BOT_TOKEN || !env.DISCORD_APP_ID) return;
  const meta = await getMeta(env);
  if (meta.commandsVersion === COMMANDS_VERSION) return;
  const res = await fetch(`${discordApi(env)}/applications/${env.DISCORD_APP_ID}/commands`, {
    method: "PUT",
    headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN.trim()}`, "Content-Type": "application/json" },
    body: JSON.stringify(COMMANDS),
  });
  if (res.ok) {
    const fresh = await getMeta(env); // re-read so we don't clobber runCheck's changes
    fresh.commandsVersion = COMMANDS_VERSION;
    await env.STATE.put("meta", JSON.stringify(fresh));
  } else {
    console.log("Command registration failed:", res.status, await res.text());
  }
}

function hexToBytes(hex) {
  return new Uint8Array((hex.match(/../g) || []).map((b) => parseInt(b, 16)));
}

async function verifyDiscord(request, env) {
  const sig = request.headers.get("X-Signature-Ed25519");
  const ts = request.headers.get("X-Signature-Timestamp");
  const body = await request.text();
  if (!sig || !ts || !env.DISCORD_PUBLIC_KEY) return null;
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(env.DISCORD_PUBLIC_KEY), { name: "Ed25519" }, false, ["verify"]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, hexToBytes(sig), new TextEncoder().encode(ts + body));
    return ok ? JSON.parse(body) : null;
  } catch {
    return null;
  }
}

const reply = (data) => Response.json(data);

async function handleInteraction(request, env, ctx) {
  const i = await verifyDiscord(request, env);
  if (!i) return new Response("bad signature", { status: 401 });
  if (i.type === 1) return reply({ type: 1 }); // Discord's endpoint check

  if (i.type === 4 && i.data && i.data.name === "wishlist") return reply({ type: 8, data: { choices: await wishlistChoices(i, env) } });
  if (i.type === 4) {
    // Autocomplete for /instock filter: suggest collections matching what's typed,
    // plus whatever was typed as a free-text search
    const typed = ((i.data.options || []).find((o) => o.focused) || {}).value || "";
    const t = norm(typed);
    const choices = FILTERS
      .filter((f) => !t || norm(f.name).includes(t) || f.aliases.some((a) => norm(a).startsWith(t)))
      .map((f) => ({ name: f.name, value: f.value }));
    if (t && !choices.some((c) => norm(c.value) === t)) choices.push({ name: `Search names for “${typed.trim()}”`.slice(0, 100), value: typed.trim().slice(0, 100) });
    return reply({ type: 8, data: { choices: choices.slice(0, 25) } });
  }

  // Defer (shows "thinking…" privately), then edit the reply when the work is done. One with vanish goes
  // away that many seconds later, unless it's changed since (they clicked My wishlist in it, say)
  const followUp = async (work) => {
    let data;
    try {
      data = await work();
    } catch (err) {
      data = { content: `Something went wrong: ${err}` };
    }
    const { vanish, ...shown } = data;
    const original = `${discordApi(env)}/webhooks/${env.DISCORD_APP_ID}/${i.token}/messages/@original`;
    await fetch(original, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowed_mentions: { parse: [] }, ...shown }),
    });
    if (!vanish) return;
    await new Promise((done) => setTimeout(done, vanish * 1000));
    const now = await fetch(original).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (now && now.content === shown.content) await fetch(original, { method: "DELETE" });
  };
  const deferred = () => reply({ type: 5, data: { flags: EPHEMERAL } });

  // The wishlist buttons, menu and pop-up form are for everyone
  if ((i.type === 3 || i.type === 5) && String((i.data && i.data.custom_id) || "").startsWith("wl:")) return wishlistComponent(i, env, ctx, followUp);

  const user = (i.member && i.member.user) || i.user || {};
  const owner = (env.DISCORD_OWNER_ID || "").trim();
  const name = i.data && i.data.name;
  if (name !== "wishlist" && (!owner || user.id !== owner)) {
    return reply({ type: 4, data: { flags: EPHEMERAL, content: "Sorry, only the owner of this stock watcher can use its commands." } });
  }

  if (name === "wishlist-panel") {
    if (!i.guild_id || !i.channel_id) return reply({ type: 4, data: { flags: EPHEMERAL, content: "Use this in a channel in your server." } });
    ctx.waitUntil(followUp(async () => {
      const [s] = await bot(env, "POST", `/channels/${i.channel_id}/messages`, panelMessage());
      if (s === 403) return { content: "I'm not allowed to post in this channel. Give me **Send Messages** and **Embed Links** here, then try again." };
      if (s >= 300) return { content: `Couldn't post the panel (Discord said ${s}).` };
      return { content: "Posted the wishlist panel. Pin it so people can find it: right-click it, then **Pin Message**." };
    }));
    return deferred();
  }

  if (name === "wishlist") {
    if (!i.guild_id) return reply({ type: 4, data: { flags: EPHEMERAL, content: "Use /wishlist in the server, so I can make your private channel there." } });
    ctx.waitUntil(followUp(() => wishlistCommand(i, env, user)));
    return deferred();
  }

  if (name === "password") {
    // The password itself is never stored or echoed: only the shop's session cookie is kept
    const pw = ((i.data.options || []).find((o) => o.name === "password") || {}).value || "";
    ctx.waitUntil(
      followUp(async () => {
        // An open shop lets anyone in, so a password can only be checked while it's locked
        const open = await shopFetch(`${SHOP}/products.json?limit=1&_=${Date.now()}`, { headers: { "User-Agent": BROWSER_UA } });
        if (open.ok) {
          return { content: "🔓 Mr Tofu's shop isn't locked right now, so no password is needed (and I can't check one until it locks). When you get the 🔒 lock message, send it with `/password` then." };
        }
        if ([429, 430, 503].includes(open.status)) return { content: "⏳ The shop is busy right now (it asked me to slow down). Try `/password` again in a moment." };
        const cookie = await loginWithPassword(pw);
        if (!cookie) return { content: "❌ That password didn't get me in. Double-check it and try `/password` again." };
        await env.STATE.put("cookie", cookie);
        const summary = await runCheck(env, { force: true });
        return { content: `✅ Password works. I'm now watching behind the lock.\n${summary}` };
      }),
    );
    return deferred();
  }

  if (name === "scan") {
    ctx.waitUntil(followUp(async () => ({ content: "🔎 " + (await runCheck(env, { force: true })) })));
    return deferred();
  }

  if (name === "instock") {
    ctx.waitUntil(
      followUp(async () => {
        // The saved login works whether or not the shop is locked, so use it when we have it
        const cookie = await env.STATE.get("cookie");
        const filter = resolveFilter(((i.data.options || []).find((o) => o.name === "filter") || {}).value);
        let products;
        try {
          products = filter && filter.handles
            ? await fetchCollections(filter.handles, cookie || null)
            : await fetchProducts(cookie || null);
        } catch (err) {
          if (err && err.busy) return { content: "⏳ Mr Tofu's shop is busy right now (it asked me to slow down). Try `/instock` again in a moment." };
          if (!(err && err.locked)) return { content: `Couldn't check the shop: ${err}` };
          return { content: cookie
            ? "🔑 The saved shop password no longer works. Send the new one with /password."
            : "🔒 The shop is password-locked. Use /password if you know it." };
        }
        let items = products.map(summarise).filter((it) => it.available);
        if (filter && filter.words) items = items.filter((it) => filter.words.every((w) => norm(it.title).includes(w)));
        const scope = filter ? ` · ${filter.label}` : "";
        if (!items.length) return { content: `Nothing is buyable right now${filter ? ` for ${filter.label}` : ""}.` };
        const lines = [];
        for (const it of items) {
          const vid = Object.entries(it.variants).find(([, v]) => v.available)[0];
          const line =
            `**[${it.title}](${SHOP}/products/${it.handle})**${it.price !== null ? ` · $${it.price.toFixed(2)}` : ""}\n` +
            `[ATC 1](${SHOP}/cart/add?id=${vid}&quantity=1) · [ATC 2](${SHOP}/cart/add?id=${vid}&quantity=2) · [⚡ 1](${SHOP}/cart/${vid}:1)`;
          if ([...lines, line].join("\n\n").length > 3900) break;
          lines.push(line);
        }
        return {
          embeds: [{
            title: `🛒 In stock now${scope} (${items.length})`,
            description: lines.join("\n\n"),
            color: 0x22c55e,
            footer: { text: lines.length < items.length ? `Showing ${lines.length} of ${items.length}` : "Mr Tofu Stock Watch" },
          }],
        };
      }),
    );
    return deferred();
  }

  if (i.type === 3 && String((i.data && i.data.custom_id) || "").startsWith("dm:")) return controlClick(i, env, ctx, followUp);
  if (name === "speed") return reply({ type: 4, data: { flags: EPHEMERAL, ...controlPanel(await dropModeState(env), "tofu") } });
  if (name === "dropmode-panel") {
    if (!i.guild_id || !i.channel_id) return reply({ type: 4, data: { flags: EPHEMERAL, content: "Use this in a channel in your server." } });
    ctx.waitUntil(followUp(async () => {
      const panel = controlPanel(await dropModeState(env), "tofu");
      const [s, msg] = await bot(env, "POST", `/channels/${i.channel_id}/messages`, panel);
      if (s === 403) return { content: "I'm not allowed to post in this channel. Give me **Send Messages** here, then try again." };
      if (s >= 300 || !msg || !msg.id) return { content: `Couldn't post the panel (Discord said ${s}).` };
      await wishlistStore(env).fetch("https://wishlists/dm/panel", { method: "POST",
        body: JSON.stringify({ channel: i.channel_id, message: msg.id, sel: "tofu", last: panelSig(panel) }) });
      return { content: "Posted drop mode's controls. Only you can use the buttons, and it keeps itself up to date (each minute). Pin it if you like: right-click it, then **Pin Message**." };
    }));
    return deferred();
  }

  if (name === "dropmode") {
    const action = ((i.data.options || [])[0] || {}).value;
    const st = await dropModeState(env), helper = st.helper || null;
    const fresh = !!helper && Date.now() - helper.at < 45000, ago = (t) => `<t:${Math.floor(t / 1000)}:R>`;
    const say = (content) => reply({ type: 4, data: { flags: EPHEMERAL, content } });
    if (!helper) return say("The helper on your PC hasn't checked in yet. It starts when you log in to Windows.");
    if (action === "status") {
      if (!fresh) return say(`Your PC's helper last checked in ${ago(helper.at)}, so the PC may be off or asleep.`);
      const on = (helper.shops || []).filter((s) => s.running).map((s) => `${s.name} (every ${every(s.interval)})`);
      return say(!helper.running ? "⚪ Drop mode is closed. `/dropmode start` opens it."
        : on.length ? `✅ Drop mode is running, watching ${on.join(", ")}.`
        : "🟡 Drop mode is open but not watching. `/dropmode start` starts watching.");
    }
    if (!fresh) return say(`Your PC's helper last checked in ${ago(helper.at)}, so I can't reach it. Is the PC on?`);
    const job = await (await wishlistStore(env).fetch("https://wishlists/dm/request", { method: "POST", body: JSON.stringify({ action }) })).json();
    ctx.waitUntil(followUp(async () => {
      for (let n = 0; n < 13; n++) { // (the helper asks every few seconds; starting drop mode takes a few more)
        await sleep(2000);
        const s = await dropModeState(env);
        if (s.result && s.result.id === job.id) return { content: (s.result.ok ? "✅ " : "⚠️ ") + s.result.message };
      }
      return { content: "⏳ Sent to your PC, but it hasn't answered yet. Check with `/dropmode status` in a minute." };
    }));
    return deferred();
  }

  if (name === "status") {
    const meta = await getMeta(env);
    const hasPw = !!(await env.STATE.get("cookie"));
    return reply({ type: 4, data: { flags: EPHEMERAL, content: statusLines(meta).join("\n") + `\nSaved shop password: ${hasPw ? "yes" : "no"}` } });
  }

  return reply({ type: 4, data: { flags: EPHEMERAL, content: "I don't know that command." } });
}

// ---------- Wishlists (anyone in the server) ----------
// Kept in KV as one "wishlists" record: { version, categories: { guildId: id }, people: { userId: { name, channel,
// hook, items: [{ text, qty, max }] } } }. Written only when someone changes their list.

const WISHLIST_MAX = 15;
const VIEW_CHANNEL = 1n << 10n, SEND_MESSAGES = 1n << 11n, EMBED_LINKS = 1n << 14n, READ_HISTORY = 1n << 16n;

// Everyone's wishlists live in one Durable Object: a single copy in one place, so a change is seen
// straight away everywhere. (KV can take a minute to reach other parts of the world, and two people
// changing the same list then could undo each other's change.) The first time, the lists kept in KV
// before are brought over; KV keeps that old copy.
// The shops the helper reports: [{ id, name, running, interval }]
const cleanShops = (shops) => (Array.isArray(shops) ? shops : []).slice(0, 25).map((s) => ({
  id: String(s.id || "").slice(0, 40), name: String(s.name || s.id || "").slice(0, 80),
  running: !!s.running, interval: Number(s.interval) || null,
})).filter((s) => s.id);

export class WishlistStore {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/seen") return Response.json({ seen: this.seen || (await this.state.storage.get("seen")) || 0 });
    if (url.pathname.startsWith("/dm/")) return this.dropMode(url, request);
    if (request.method === "PUT") {
      await this.state.storage.put("data", await request.json());
      return new Response("ok");
    }
    if (url.searchParams.has("seen")) await this.markSeen();
    return Response.json((await this.state.storage.get("data")) ?? null);
  }

  // /dropmode: a command waits here for the helper on the owner's PC, which asks every few seconds (and says
  // whether drop mode is running and watching). Commands older than 2 minutes are dropped, so a PC that was
  // off doesn't open drop mode hours later. The helper's news is saved at most once a minute.
  async dropMode(url, request) {
    const store = this.state.storage, now = Date.now();
    if (!this.dm) this.dm = (await store.get("dm")) || {};
    const dm = this.dm, save = () => store.put("dm", dm);
    if (url.pathname === "/dm/request") {
      const { action, shop, interval } = await request.json();
      dm.pending = { id: crypto.randomUUID(), action, shop, interval, at: now };
      await save();
      return Response.json(dm.pending);
    }
    if (url.pathname === "/dm/poll") {
      const b = request.method === "POST" ? await request.json().catch(() => ({})) : {
        running: url.searchParams.get("running") === "1", watching: url.searchParams.get("watching") === "1" };
      dm.helper = { at: now, running: !!b.running, watching: !!b.watching, shops: cleanShops(b.shops) };
      let job = null;
      if (dm.pending && !dm.pending.taken && now - dm.pending.at < 120000) {
        dm.pending.taken = now;
        job = { id: dm.pending.id, action: dm.pending.action, shop: dm.pending.shop, interval: dm.pending.interval };
        await save();
      } else if (now - (this.dmSaved || 0) > 60000) {
        this.dmSaved = now;
        await save();
      }
      return Response.json({ job });
    }
    if (url.pathname === "/dm/done") {
      const r = await request.json();
      dm.result = { id: String(r.id || ""), ok: !!r.ok, message: String(r.message || "").slice(0, 300), at: now };
      dm.helper = { at: now, running: !!r.running, watching: !!r.watching, shops: cleanShops(r.shops) };
      if (dm.pending && dm.pending.id === dm.result.id) {
        if (dm.pending.action === "stop" && r.ok) dm.stoppedAt = now; // (closed on purpose: no "it's stopped" warning)
        dm.pending = null;
      }
      await save();
      return Response.json({ ok: true });
    }
    if (url.pathname === "/dm/panel") { // where the posted panel is: { channel, message, sel, last }, or changes to it
      const b = await request.json();
      dm.panel = b.channel ? { channel: b.channel, message: b.message, sel: b.sel || "tofu", last: b.last || "" }
        : b.clear ? null : { ...(dm.panel || {}), ...b };
      await save();
      return Response.json({ ok: true });
    }
    return Response.json(dm);
  }

  // When drop mode last read the lists (it does every few seconds while it's running): kept in memory,
  // saved at most once a minute (the bot only looks once a minute)
  async markSeen() {
    const now = Date.now();
    this.seen = now;
    if (now - (this.saved || 0) > 60000) {
      this.saved = now;
      await this.state.storage.put("seen", now);
    }
  }
}

const wishlistStore = (env) => env.WISHLISTS.get(env.WISHLISTS.idFromName("wishlists"));

// (Throws if the lists can't be read, rather than handing back an empty record that saving would
// write over everyone's lists with.)
async function getWishlists(env, seen = false) {
  if (!env.WISHLISTS) return JSON.parse((await env.STATE.get("wishlists")) || "{}");
  const res = await wishlistStore(env).fetch(seen ? "https://wishlists/?seen=1" : "https://wishlists/"); // (seen: drop mode asking)
  if (!res.ok) throw new Error(`the wishlists couldn't be read (${res.status})`);
  const data = await res.json();
  if (data) return data;
  let old = {};
  try {
    old = JSON.parse((await env.STATE.get("wishlists")) || "{}");
  } catch {}
  await wishlistStore(env).fetch("https://wishlists/", { method: "PUT", body: JSON.stringify(old) });
  return old;
}

async function putWishlists(env, data) {
  data.version = (data.version || 0) + 1;
  if (!env.WISHLISTS) return env.STATE.put("wishlists", JSON.stringify(data));
  const res = await wishlistStore(env).fetch("https://wishlists/", { method: "PUT", body: JSON.stringify(data) });
  if (!res.ok) throw new Error(`the wishlists couldn't be saved (${res.status})`);
}

// A call to Discord's API as the bot. Returns [status, parsed body]
async function bot(env, method, path, body) {
  const res = await fetch(`${discordApi(env)}${path}`, {
    method,
    headers: { Authorization: `Bot ${(env.DISCORD_BOT_TOKEN || "").trim()}`, "Content-Type": "application/json", "User-Agent": "DiscordBot (tofu-stock-watch, 1.0)" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch {}
  return [res.status, data];
}

const noPermission = "I'm not allowed to make your channel yet. Ask the server owner to give me the **Manage Channels** and **Manage Webhooks** permissions (plus View Channels, Send Messages, Embed Links and Read Message History), then try again.";

// A webhook in someone's channel, for drop mode to post to. Returns [error message, its link]
async function makeHook(env, channelId) {
  const [w, hook] = await bot(env, "POST", `/channels/${channelId}/webhooks`, { name: "Drop mode" });
  if (w === 403) return [noPermission];
  if (w >= 300 || !hook || !hook.id || !hook.token) return [`Couldn't set up the pings in your channel (Discord said ${w}).`];
  return [null, `${apiBase(env)}/webhooks/${hook.id}/${hook.token}`];
}

// The person's private channel (and a webhook in it, which drop mode posts to). Made the first
// time, and again if it's been deleted (a deleted webhook gets a new one). Returns an error
// message, or null when it's ready.
async function ensureChannel(env, data, guildId, user, person) {
  if (!env.DISCORD_BOT_TOKEN) return "The bot isn't fully set up yet (it has no token).";
  if (person.channel) {
    const [status] = await bot(env, "GET", `/channels/${person.channel}`);
    if (status === 200 || (status !== 404 && status !== 403 && person.hook)) { // (there, or a Discord hiccup)
      if (person.hook && (await fetch(person.hook).then((r) => r.status).catch(() => 0)) !== 404) return null;
      const [problem, hook] = await makeHook(env, person.channel);
      if (problem) return problem;
      person.hook = hook;
      return null;
    }
    delete person.channel; // (deleted, or the bot can't see it any more: make a new one)
    delete person.hook;
  }
  // One "🛒 Wishlists" category (in each server) holds everyone's channels
  data.categories = data.categories || {};
  let [status] = data.categories[guildId] ? await bot(env, "GET", `/channels/${data.categories[guildId]}`) : [404];
  if (status !== 200) {
    const [s, cat] = await bot(env, "POST", `/guilds/${guildId}/channels`, { name: "🛒 Wishlists", type: 4 });
    if (s === 403) return noPermission;
    if (s >= 300 || !cat || !cat.id) return `Couldn't make the Wishlists category (Discord said ${s}).`;
    data.categories[guildId] = cat.id;
  }
  const slug = String(person.name || user.username || user.id).toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || user.id;
  const [s, channel] = await bot(env, "POST", `/guilds/${guildId}/channels`, {
    name: `wishlist-${slug}`,
    type: 0,
    parent_id: data.categories[guildId],
    topic: "Your wishlist pings land here. Only you (and the server's admins) can see this channel. Change your list with /wishlist.",
    permission_overwrites: [
      { id: guildId, type: 0, deny: String(VIEW_CHANNEL) }, // @everyone can't see it
      { id: user.id, type: 1, allow: String(VIEW_CHANNEL | READ_HISTORY) },
      { id: env.DISCORD_APP_ID, type: 1, allow: String(VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_HISTORY) },
    ],
  });
  if (s === 403) return noPermission;
  if (s >= 300 || !channel || !channel.id) return `Couldn't make your channel (Discord said ${s}).`;
  person.channel = channel.id; // (kept even if the webhook doesn't work out, so trying again uses this channel)
  const [problem, hook] = await makeHook(env, channel.id);
  if (problem) return problem;
  person.hook = hook;
  const welcome = {
    content: `<@${user.id}> 👋 This is your wishlist channel. When something on your wishlist comes in stock, I'll ping you here with buttons to check out or add it to your cart. Change your list with the buttons below (or \`/wishlist\`).`,
    allowed_mentions: { parse: [], users: [user.id] },
  };
  // (the webhook is the bot's own, so it can have working buttons; if Discord won't take them, it goes without)
  const post = (body, query = "") => fetch(person.hook + query, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.ok).catch(() => false);
  if (!(await post({ ...welcome, components: [row(addButton(), listButton())] }, "?with_components=true"))) await post(welcome);
  return null;
}

const describe = (it) => `**${it.text}**` + (it.qty > 1 ? ` · ${it.qty} of them` : "") + (it.max ? ` · max $${Number(it.max).toFixed(2)}` : "");

// A short name for a wishlist item that stays the same while its words do (for menus and buttons)
function itemKey(text) {
  let h = 0x811c9dc5;
  for (const ch of norm(text)) h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193) >>> 0;
  return h.toString(16).padStart(8, "0");
}

// ---- the buttons, menu and pop-up form ----
const row = (...components) => ({ type: 1, components });
const addButton = (label = "Add something") => ({ type: 2, style: 1, label, emoji: { name: "➕" }, custom_id: "wl:add" });
const listButton = () => ({ type: 2, style: 2, label: "My wishlist", emoji: { name: "📋" }, custom_id: "wl:list" });

const addForm = () => ({
  custom_id: "wl:addform",
  title: "Add to your wishlist",
  components: [
    row({ type: 4, custom_id: "keywords", label: "Keywords (every word must be in the name)", style: 1, required: true, max_length: 100,
      placeholder: "e.g. focused fighters    (a - before a word leaves it out: -tin)" }),
    row({ type: 4, custom_id: "quantity", label: "How many (1-5)", style: 1, required: false, max_length: 1, value: "1" }),
    row({ type: 4, custom_id: "max_price", label: "Max price each (optional)", style: 1, required: false, max_length: 10, placeholder: "e.g. 120" }),
  ],
});

// The panel everyone can use (posted with /wishlist-panel)
const panelMessage = () => ({
  embeds: [{
    color: 0x6cc08d,
    title: "🛒 Wishlists",
    description: "Get pinged in your own private channel when something you want comes in stock.\n\n"
      + "**➕ Add something**: type a few words from the product's name. Every word has to be in it, and a word with a - in front leaves products out (`booster bundle -tin`).\n"
      + "**📋 My wishlist**: see your list, or take things off it.\n\nOnly you can see your list and your channel.",
  }],
  components: [row(addButton(), listButton())],
  allowed_mentions: { parse: [] },
});

// Someone's list, with a menu to take things off and buttons to add or empty it (only they see it)
function listView(person, note = "") {
  const items = person.items || [];
  const top = note ? `${note}\n\n` : "";
  if (!items.length) return { content: top + "Your wishlist is empty. Press **Add something** to start one.", components: [row(addButton())] };
  const where = person.channel ? `<#${person.channel}>` : "your private channel";
  const detail = (it) => [it.qty > 1 ? `${it.qty} of them` : "", it.max ? `max $${Number(it.max).toFixed(2)}` : ""].filter(Boolean).join(" · ");
  return {
    content: (top + `Your wishlist (pings go in ${where}):\n` + items.map((it, n) => `${n + 1}. ${describe(it)}`).join("\n")).slice(0, 2000),
    components: [
      row({
        type: 3, custom_id: "wl:rm", placeholder: "Take something off your list…", min_values: 1, max_values: Math.min(items.length, 25),
        options: items.slice(0, 25).map((it) => ({ label: it.text.slice(0, 100), value: itemKey(it.text), ...(detail(it) ? { description: detail(it) } : {}) })),
      }),
      row(addButton(), { type: 2, style: 2, label: "Empty my list", emoji: { name: "🗑️" }, custom_id: "wl:clear" }),
    ],
  };
}

// The person's wishlist record (a new one the first time), with their current name
async function personFor(env, i, user) {
  const data = await getWishlists(env);
  data.people = data.people || {};
  const name = (i.member && i.member.nick) || user.global_name || user.username || "someone";
  const person = data.people[user.id] || { name, items: [] };
  person.name = name;
  return [data, person];
}

async function savePerson(env, data, user, person) {
  data.people[user.id] = person;
  await putWishlists(env, data);
}

// Add something (from /wishlist add or the pop-up form). Returns the reply.
async function addItem(env, i, user, data, person, keywords, qty, max) {
  const text = String(keywords || "").replace(/\s+/g, " ").trim().slice(0, 100);
  const words = norm(text).split(/[\s,]+/).filter(Boolean);
  if (!words.some((w) => !w.startsWith("-"))) return { content: "Give me at least one word to look for (words with a - in front only leave things out).", components: [row(addButton("Try again"))] };
  if (person.items.some((it) => norm(it.text) === norm(text))) return { content: `${describe({ text })} is already on your wishlist.`, components: [row(listButton())] };
  if (person.items.length >= WISHLIST_MAX) return { content: `Your wishlist is full (${WISHLIST_MAX} things). Take something off it first.`, components: [row(listButton())] };
  const made = () => JSON.stringify([data.categories, person.channel, person.hook]), before = made();
  const problem = await ensureChannel(env, data, i.guild_id, user, person);
  if (problem) {
    if (made() !== before) await savePerson(env, data, user, person); // (keep what was made on the way)
    return { content: problem };
  }
  const item = { text, qty, added: Date.now() }; // (when: so drop mode treats it as new, even if it was on the list before)
  if (max) item.max = max;
  person.items.push(item);
  await savePerson(env, data, user, person);
  return {
    content: `Added ${describe(item)}. I'll ping you in <#${person.channel}> when it's in stock (any shop drop mode watches). Every word has to be in the product's name.`,
    components: [row(addButton("Add another"), listButton())],
    vanish: 15, // (gone after 15 seconds: it's only saying it worked)
  };
}

async function wishlistCommand(i, env, user) {
  const sub = (i.data.options || [])[0] || {};
  const opt = (n) => ((sub.options || []).find((o) => o.name === n) || {}).value;
  const [data, person] = await personFor(env, i, user);

  if (sub.name === "list") return listView(person);
  if (sub.name === "clear") {
    if (!person.items.length) return { content: "Your wishlist is already empty." };
    person.items = [];
    await savePerson(env, data, user, person);
    return { content: "Emptied your wishlist. (Your channel stays, ready for next time.)" };
  }
  if (sub.name === "remove") {
    const want = norm(opt("item"));
    const at = person.items.findIndex((it) => norm(it.text) === want);
    if (at < 0) return { content: "That isn't on your wishlist. See it with `/wishlist list`." };
    const [gone] = person.items.splice(at, 1);
    await savePerson(env, data, user, person);
    return { content: `Took ${describe(gone)} off your wishlist.` };
  }
  if (sub.name === "add") {
    const max = parseFloat(opt("max_price"));
    return addItem(env, i, user, data, person, opt("keywords"), Math.max(1, Math.min(5, parseInt(opt("quantity") || 1, 10) || 1)),
      max > 0 ? Math.round(max * 100) / 100 : null);
  }
  return { content: "Use `/wishlist add`, `/wishlist list`, `/wishlist remove` or `/wishlist clear`." };
}

// A click on a wishlist button or menu, or the pop-up form sent (from the panel, someone's own
// private replies, their channel, or a ping in it). Each person only ever sees and changes their own.
async function wishlistComponent(i, env, ctx, followUp) {
  const user = (i.member && i.member.user) || i.user || {};
  const id = String(i.data.custom_id);
  const mine = (data) => reply({ type: 4, data: { flags: EPHEMERAL, allowed_mentions: { parse: [] }, ...data } }); // a new reply only they see
  const update = (data) => reply({ type: 7, data: { allowed_mentions: { parse: [] }, ...data } }); // change the reply they clicked in
  if (!i.guild_id) return mine({ content: "Use this in the server." });
  if (id === "wl:add") return reply({ type: 9, data: addForm() });
  if (id === "wl:addform") {
    const field = (name) => {
      for (const r of i.data.components || []) for (const c of r.components || (r.component ? [r.component] : [])) if (c.custom_id === name) return String(c.value || "").trim();
      return "";
    };
    const qty = Number(field("quantity") || "1"), maxText = field("max_price").replace(/^\$/, ""), max = maxText ? Number(maxText) : null;
    if (!Number.isInteger(qty) || qty < 1 || qty > 5) return mine({ content: "How many has to be a number from 1 to 5.", components: [row(addButton("Try again"))] });
    if (max !== null && !(max > 0)) return mine({ content: "The max price has to be a number (or leave it empty).", components: [row(addButton("Try again"))] });
    ctx.waitUntil(followUp(async () => {
      const [data, person] = await personFor(env, i, user);
      return addItem(env, i, user, data, person, field("keywords"), qty, max === null ? null : Math.round(max * 100) / 100);
    }));
    return reply({ type: 5, data: { flags: EPHEMERAL } });
  }
  const [data, person] = await personFor(env, i, user);
  const inPlace = ((i.message && i.message.flags) || 0) & EPHEMERAL; // (clicked in one of their own private replies)
  if (id === "wl:list") return (inPlace ? update : mine)(listView(person));
  if (id === "wl:rm") {
    const keys = new Set(i.data.values || []);
    const gone = person.items.filter((it) => keys.has(itemKey(it.text)));
    if (gone.length) {
      person.items = person.items.filter((it) => !keys.has(itemKey(it.text)));
      await savePerson(env, data, user, person);
    }
    return update(listView(person, gone.length ? `Took ${gone.map(describe).join(", ")} off your wishlist.` : "That was already off your list."));
  }
  if (id === "wl:clear") {
    const n = person.items.length;
    if (!n) return update(listView(person));
    return update({
      content: `Empty your whole wishlist (${n} thing${n === 1 ? "" : "s"})?`,
      components: [row({ type: 2, style: 4, label: "Empty it", custom_id: "wl:clearyes" }, { type: 2, style: 2, label: "Keep it", custom_id: "wl:list" })],
    });
  }
  if (id === "wl:clearyes") {
    if (person.items.length) {
      person.items = [];
      await savePerson(env, data, user, person);
    }
    return update(listView(person, "Emptied your wishlist. Your channel stays, ready for next time."));
  }
  if (id.startsWith("wl:drop:")) { // "Remove from my wishlist" on a ping
    const at = person.items.findIndex((it) => itemKey(it.text) === id.slice(8));
    if (at < 0) return mine({ content: "That's already off your wishlist.", components: [row(listButton())] });
    const [gone] = person.items.splice(at, 1);
    await savePerson(env, data, user, person);
    return mine({ content: `Took ${describe(gone)} off your wishlist, so you won't be pinged about it any more.`, components: [row(listButton())] });
  }
  return mine({ content: "That button doesn't do anything any more." });
}

// /wishlist remove: suggest the person's own items
async function wishlistChoices(i, env) {
  const user = (i.member && i.member.user) || i.user || {};
  const typed = norm((((i.data.options || [])[0] || {}).options || []).find((o) => o.focused)?.value || "");
  const person = ((await getWishlists(env)).people || {})[user.id];
  return ((person && person.items) || [])
    .filter((it) => !typed || norm(it.text).includes(typed))
    .slice(0, 25)
    .map((it) => ({ name: `${it.text}${it.qty > 1 ? ` (x${it.qty})` : ""}${it.max ? ` · max $${it.max}` : ""}`.slice(0, 100), value: it.text.slice(0, 100) }));
}

// For drop mode (with DROP_MODE_KEY): everyone's wishlist, and where to ping them
function sameText(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let k = 0; k < a.length; k++) diff |= a.charCodeAt(k) ^ b.charCodeAt(k);
  return diff === 0;
}

const feedPerson = (id, p) => ({ id, name: p.name, hook: p.hook, items: (p.items || []).map((it) => ({ ...it, key: itemKey(it.text) })) });

// Drop mode changing someone's wishlist (with DROP_MODE_KEY): add something, change how many or the max
// price, or take something off. They get a note in their channel saying what changed (it names the owner,
// without pinging anyone), with a button to see their list. Or delete them: their list and their channel.
async function wishlistEdit(request, env, ctx, userId) {
  const key = (env.DROP_MODE_KEY || "").trim();
  if (!key) return new Response("Drop mode's key isn't set up on the bot yet.", { status: 503 });
  if (!sameText(request.headers.get("Authorization") || "", `Bearer ${key}`)) return new Response("forbidden", { status: 403 });
  const no = (error, status = 400) => Response.json({ error }, { status });
  let body;
  try {
    body = await request.json();
  } catch {
    return no("That wasn't a change drop mode understands.");
  }
  const data = await getWishlists(env);
  const person = (data.people || {})[userId];
  if (body.action === "delete") return wishlistDelete(env, data, userId, person);
  if (!person || !person.hook) return no("They haven't got a wishlist channel yet (they make one with /wishlist).", 404);
  person.items = person.items || [];
  const owner = (env.DISCORD_OWNER_ID || "").trim();
  const by = owner ? `<@${owner}>` : "The server owner";
  const money = (v) => Math.round(parseFloat(v) * 100) / 100;
  let note;
  if (body.action === "add") {
    const text = String(body.text || "").replace(/\s+/g, " ").trim().slice(0, 100);
    if (!norm(text).split(/[\s,]+/).filter(Boolean).some((w) => !w.startsWith("-"))) return no("Give it at least one word to look for (words with a - in front only leave things out).");
    if (person.items.some((it) => norm(it.text) === norm(text))) return no(`${text} is already on their wishlist.`);
    if (person.items.length >= WISHLIST_MAX) return no(`Their wishlist is full (${WISHLIST_MAX} things).`);
    const item = { text, qty: Math.max(1, Math.min(5, parseInt(body.qty, 10) || 1)), added: Date.now() };
    if (money(body.max) > 0) item.max = money(body.max);
    person.items.push(item);
    note = `📝 ${by} added ${describe(item)} to your wishlist.`;
  } else if (body.action === "update" || body.action === "remove") {
    const at = person.items.findIndex((it) => itemKey(it.text) === String(body.key || ""));
    if (at < 0) return no("That's not on their wishlist any more.", 404);
    if (body.action === "remove") {
      const [gone] = person.items.splice(at, 1);
      note = `📝 ${by} took ${describe(gone)} off your wishlist.`;
    } else {
      const it = person.items[at];
      if (body.qty !== undefined) it.qty = Math.max(1, Math.min(5, parseInt(body.qty, 10) || 1));
      if (body.max !== undefined) {
        if (money(body.max) > 0) it.max = money(body.max);
        else delete it.max;
      }
      note = `📝 ${by} changed **${it.text}** on your wishlist (now: buy ${it.qty}, ${it.max ? `max $${Number(it.max).toFixed(2)} each` : "no max price"}).`;
    }
  } else {
    return no("That wasn't a change drop mode understands.");
  }
  await putWishlists(env, data);
  const message = { content: note, allowed_mentions: { parse: [] } };
  const post = (body, query = "") => fetch(person.hook + query, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.ok).catch(() => false);
  ctx.waitUntil(post({ ...message, components: [row(listButton())] }, "?with_components=true").then((ok) => ok || post(message)));
  return Response.json({ ok: true, person: feedPerson(userId, person), version: data.version });
}

// Deleting someone (drop mode's Delete button): their list, their channel and its webhook, and them. If they
// use /wishlist again, they start afresh with a new channel.
async function wishlistDelete(env, data, userId, person) {
  if (!person) return Response.json({ error: "They don't have a wishlist any more." }, { status: 404 });
  if (person.hook) await fetch(person.hook, { method: "DELETE" }).catch(() => null); // (nothing can post there now, even if the channel stays)
  let channel = "none";
  if (person.channel) {
    const [s] = await bot(env, "DELETE", `/channels/${person.channel}`);
    channel = s < 300 ? "deleted" : s === 404 ? "gone" : "kept";
  }
  delete data.people[userId];
  await putWishlists(env, data);
  return Response.json({ ok: true, channel, version: data.version });
}

async function wishlistFeed(request, env) {
  const key = (env.DROP_MODE_KEY || "").trim();
  if (!key) return new Response("Drop mode's key isn't set up on the bot yet.", { status: 503 });
  if (!sameText(request.headers.get("Authorization") || "", `Bearer ${key}`)) return new Response("forbidden", { status: 403 });
  const data = await getWishlists(env, true);
  const people = Object.entries(data.people || {}).filter(([, p]) => p.hook).map(([id, p]) => feedPerson(id, p));
  return Response.json({ version: data.version || 0, people }, { headers: { "Cache-Control": "no-store" } });
}

// ---------- Shop data and alerts ----------

function summarise(p) {
  const variants = p.variants || [];
  const inStock = variants.filter((v) => v.available);
  const prices = (inStock.length ? inStock : variants).map((v) => parseFloat(v.price)).filter((n) => !isNaN(n)).sort((a, b) => a - b);
  const vars = {};
  for (const v of variants) vars[v.id] = { title: v.title || "", available: !!v.available };
  return {
    title: p.title,
    handle: p.handle,
    available: inStock.length > 0,
    price: prices.length ? prices[0] : null,
    image: p.images && p.images[0] ? p.images[0].src : null,
    published: p.published_at || null,
    variants: vars,
  };
}

// Shopify reports "sold out" and "not released yet" the same way (unavailable),
// with no stock counts. If an item went public a while ago and can't be bought,
// it sold out; if it's unavailable right as it's published, it's not on sale yet.
const SOLD_OUT_AFTER_MS = 10 * 60 * 1000;
function soldOut(item) {
  if (item.available || !item.published) return false;
  const t = Date.parse(item.published);
  return !isNaN(t) && Date.now() - t > SOLD_OUT_AFTER_MS;
}
const unavailableText = (item) => (soldOut(item) ? "Sold out ❌" : "Not on sale yet 🔜");

// Snapshot keeps only what diff() needs: { productId: [available, { variantId: 0|1 }] }
function compact(current) {
  const out = {};
  for (const [id, item] of Object.entries(current)) {
    const v = {};
    for (const [vid, x] of Object.entries(item.variants)) v[vid] = x.available ? 1 : 0;
    out[id] = [item.available ? 1 : 0, v];
  }
  return out;
}

function keywords(env) {
  return (env.WATCH_KEYWORDS || "").split(",").map((k) => k.trim().toLowerCase()).filter(Boolean);
}

function watched(item, kw) {
  // Whole-word match so "tin" doesn't fire on "Martin"
  const t = item.title.toLowerCase();
  return kw.some((k) => new RegExp(`(?<![a-z0-9])${k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![a-z0-9])`).test(t));
}

function diff(previous, current, kw) {
  const out = [];
  for (const [id, item] of Object.entries(current)) {
    const old = previous[id];
    if (!old) {
      out.push([item.available ? "new" : soldOut(item) ? "new-soldout" : "new-soon", item, null]);
      continue;
    }
    const [wasAvailable, oldVars] = old;
    if (item.available && !wasAvailable) {
      out.push(["restock", item, null]);
      continue;
    }
    const fresh = Object.entries(item.variants)
      .filter(([vid, v]) => v.available && !oldVars[vid])
      .map(([, v]) => v.title);
    if (fresh.length) {
      const named = fresh.filter((t) => t && t !== "Default Title");
      out.push(["option", item, "Now buyable" + (named.length ? `: **${named.join(", ").slice(0, 300)}**` : "")]);
    }
  }
  // Watched items first so they're at the top of the message
  return out.sort((a, b) => watched(b[1], kw) - watched(a[1], kw));
}

function buyLinks(item, kind) {
  let vids = Object.entries(item.variants).filter(([, v]) => v.available);
  const live = vids.length > 0;
  if (!live) vids = Object.entries(item.variants); // links still work once it's buyable
  const note = live ? "" : soldOut(item) ? "\n*Sold out: these work if it restocks.*" : "\n*Not on sale yet: these work once it goes live.*";
  const lines = [];
  for (const [vid, v] of vids) {
    const links = [1, 2, 3, 4, 5]
      .map((n) => (kind === "cart" ? `[ATC ${n}](${SHOP}/cart/add?id=${vid}&quantity=${n})` : `[⚡ ${n}](${SHOP}/cart/${vid}:${n})`))
      .join(" · ");
    const name = v.title && v.title !== "Default Title" ? v.title : "";
    const line = (name && vids.length > 1 ? `**${name}:** ` : "") + links;
    if ([...lines, line].join("\n").length + note.length > 1024) break;
    lines.push(line);
  }
  return lines.length ? lines.join("\n") + note : null;
}

function embed(kind, item, note, kw) {
  let [label, color] = LABELS[kind];
  if (watched(item, kw)) [label, color] = ["🚨 " + label, 0xef4444];
  const fields = [];
  if (item.price !== null) fields.push({ name: "Price", value: `$${item.price.toFixed(2)}`, inline: true });
  fields.push({ name: "Stock", value: item.available ? "In stock ✅" : unavailableText(item), inline: true });
  const opts = Object.values(item.variants).filter((v) => v.available && v.title !== "Default Title").map((v) => v.title);
  if (opts.length) fields.push({ name: "Available options", value: opts.join(", ").slice(0, 1024), inline: false });
  const atc = buyLinks(item, "cart");
  if (atc) fields.push({ name: "🛒 Add to cart", value: atc, inline: false });
  const fast = buyLinks(item, "checkout");
  if (fast) fields.push({ name: "⚡ Lightning checkout (straight to payment)", value: fast, inline: false });
  const e = { author: { name: label }, title: item.title.slice(0, 256), url: `${SHOP}/products/${item.handle}`, color, fields };
  if (note) e.description = note;
  if (item.image) e.thumbnail = { url: item.image };
  return e;
}

// A test alert in the stock-alerts channel (drop mode's "Send a test alert", with DROP_MODE_KEY): something
// that's in stock now, laid out exactly like a real alert, marked as a test (and pinging nobody)
async function testAlert(request, env) {
  const key = (env.DROP_MODE_KEY || "").trim();
  if (!key) return new Response("Drop mode's key isn't set up on the bot yet.", { status: 503 });
  if (!sameText(request.headers.get("Authorization") || "", `Bearer ${key}`)) return new Response("forbidden", { status: 403 });
  if (!env.DISCORD_WEBHOOK_URL) return Response.json({ error: "The bot hasn't got a stock-alerts channel yet (its DISCORD_WEBHOOK_URL)." }, { status: 400 });
  let items = [];
  try {
    items = (await fetchProducts((await env.STATE.get("cookie")) || null)).map(summarise);
  } catch {} // (locked, or busy: the test still goes, without a product)
  const item = items.find((it) => it.available && it.image) || items.find((it) => it.available) || items[0];
  const payload = {
    username: "Mr Tofu Stock Watch",
    content: "🔔 Test: new listings and restocks show up here like this. (Only a test: nothing's changed in the shop.)",
    allowed_mentions: { parse: [] },
  };
  if (item) {
    const [{ embeds }] = alertMessages([["restock", item, null]], keywords(env), null);
    embeds[0].author.name = "🔔 Test · " + embeds[0].author.name;
    payload.embeds = embeds;
  }
  try {
    await postWebhook(env, payload);
  } catch (err) {
    return Response.json({ error: `Couldn't post in the stock-alerts channel (${err.message}). Check the bot's DISCORD_WEBHOOK_URL.` }, { status: 502 });
  }
  return Response.json({ ok: true, item: item ? item.title : null });
}

async function postWebhook(env, payload) {
  if (!env.DISCORD_WEBHOOK_URL) {
    console.log("[dry run] " + JSON.stringify(payload));
    return;
  }
  const res = await fetch(env.DISCORD_WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": "DiscordBot (tofu-stock-watch, 1.0)" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Discord said HTTP ${res.status}`);
}

// ---------- Grid layout for several alerts at once ----------

const KIND_ICON = { new: "🆕", "new-soon": "🔜", "new-soldout": "❌", restock: "🔁", option: "➕" };
const KIND_TEXT = { new: "New", "new-soon": "Not on sale yet", "new-soldout": "Sold out", restock: "Back in stock", option: "New option" };

// One compact tile (an inline embed field) per item: Discord lays these out
// up to 3 across on desktop.
function gridField(kind, item, note, kw) {
  const vars = Object.entries(item.variants);
  const live = vars.filter(([, v]) => v.available);
  const [vid] = (live[0] || vars[0] || []);
  const title = item.title.length > 90 ? item.title.slice(0, 89) + "…" : item.title;
  const lines = [
    `${KIND_ICON[kind]} ${KIND_TEXT[kind]} · ${item.price !== null ? `**$${item.price.toFixed(2)}**` : "price TBC"}`,
  ];
  if (kind === "option" && note) lines.push(note.slice(0, 120));
  if (vid) {
    const nums = (fmt) => [1, 2, 3, 4, 5].map((n) => `[${n}](${fmt(n)})`).join(" ");
    lines.push(`🛒 ${nums((n) => `${SHOP}/cart/add?id=${vid}&quantity=${n}`)}`);
    lines.push(`⚡ ${nums((n) => `${SHOP}/cart/${vid}:${n}`)}`);
  }
  const more = (live.length || vars.length) - 1;
  lines.push(`[View](${SHOP}/products/${item.handle})` + (more > 0 ? ` · +${more} more option${more > 1 ? "s" : ""}` : ""));
  return { name: (watched(item, kw) ? "🚨 " : "") + title, value: lines.join("\n"), inline: true };
}

// Returns [{ embeds, count }] messages: a full card for a single alert, or
// grid cards (tiles + a 2×2 picture gallery) when several land together.
function alertMessages(found, kw, lockNote) {
  if (found.length === 1) {
    const [kind, item, note] = found[0];
    return [{ embeds: [embed(kind, item, [note, lockNote].filter(Boolean).join("\n") || null, kw)], count: 1 }];
  }
  const messages = [];
  let fields = [], images = [], size = 0, loud = false;
  const flush = () => {
    if (!fields.length) return;
    const main = {
      title: `Mr Tofu's shop · ${fields.length} update${fields.length > 1 ? "s" : ""}`,
      url: SHOP, // shared url makes Discord group the images below into one gallery
      color: loud ? 0xef4444 : 0xf5a524,
      fields,
      footer: { text: "🛒 = add that many to cart · ⚡ = straight to checkout" },
    };
    if (lockNote) main.description = lockNote;
    if (images[0]) main.image = { url: images[0] };
    const gallery = images.slice(1, 4).map((u) => ({ url: SHOP, image: { url: u } }));
    messages.push({ embeds: [main, ...gallery], count: fields.length });
    fields = []; images = []; size = 0; loud = false;
  };
  for (const [kind, item, note] of found) {
    const f = gridField(kind, item, note, kw);
    const len = f.name.length + f.value.length;
    if (fields.length >= 24 || size + len > 5200) flush(); // Discord: 25 fields / 6000 chars per message
    fields.push(f);
    size += len;
    loud = loud || watched(item, kw);
    if (item.image && images.length < 4) images.push(item.image);
  }
  flush();
  return messages;
}

async function send(env, messages, loud) {
  const ping = (env.DISCORD_PING || "").trim();
  for (const { embeds, count } of messages) {
    for (let i = 0; i < embeds.length; i += 10) {
      await postWebhook(env, {
        username: "Mr Tofu Stock Watch",
        content: (ping ? ping + " " : "") + (loud ? "🚨 **Watched item update!** " : "") +
          `${count} update${count !== 1 ? "s" : ""} at Mr Tofu's shop`,
        embeds: embeds.slice(i, i + 10),
        allowed_mentions: { parse: ["users", "roles", "everyone"] },
      });
    }
  }
}

async function notify(env, text) {
  try {
    await postWebhook(env, { username: "Mr Tofu Stock Watch", content: text });
  } catch (err) {
    console.log("Couldn't post status message:", String(err));
  }
}
