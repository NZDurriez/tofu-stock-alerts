// Mr Tofu stock watcher on Cloudflare Workers.
// Runs every minute (cron trigger), compares the shop's product list with the
// last snapshot in KV, and posts Discord alerts for new listings, restocks and
// newly-buyable options. Same behaviour as check_stock.py on GitHub Actions.
//
// Also answers Discord slash commands at /interactions (owner only):
//   /password <pw>  log in behind the shop's password page and keep watching there
//   /scan           check right now        /instock  list what's buyable
//   /status         what the watcher sees right now

const SHOP = "https://mrtofu.store";
const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36";
const OUTAGE_WARN_MS = 15 * 60 * 1000;
const DISCORD_API = "https://discord.com/api/v10";
const EPHEMERAL = 64;

const LABELS = {
  new: ["🆕 New in the shop", 0xf5a524],
  "new-soon": ["🆕 New listing (not on sale yet)", 0xa78bfa],
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
const COMMANDS_VERSION = 2;
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
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// KV reads can be up to a minute stale at the edge, so within one run keep
// our own writes in memory and read those first. Stops repeat alerts between
// the several checks a run makes.
function withMemory(kv) {
  const mem = new Map();
  return {
    get: async (k) => (mem.has(k) ? mem.get(k) : kv.get(k)),
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
      const n = Math.max(1, Math.min(6, parseInt(env.CHECKS_PER_MINUTE || "4", 10) || 4));
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
      return `Couldn't check the shop: ${err}`;
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
      return `Couldn't check the shop: ${e2}`;
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

// With an etag, Shopify answers 304 (no body) when nothing changed; then this
// returns null. Otherwise returns the page body.
async function fetchPage(page, cookie, etag, out) {
  const headers = { "User-Agent": BROWSER_UA, Accept: "application/json", "Cache-Control": "no-cache" };
  if (cookie) headers.Cookie = cookie;
  if (etag) headers["If-None-Match"] = etag;
  const res = await fetch(`${SHOP}/products.json?limit=250&page=${page}&_=${Date.now()}`, {
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
    const res = await fetch(`${SHOP}/collections/${h}/products.json?limit=250&_=${Date.now()}`, { headers, cf: { cacheTtl: 0 } });
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
  const res = await fetch(`${DISCORD_API}/applications/${env.DISCORD_APP_ID}/commands`, {
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

  const user = (i.member && i.member.user) || i.user || {};
  const owner = (env.DISCORD_OWNER_ID || "").trim();
  if (!owner || user.id !== owner) {
    return reply({ type: 4, data: { flags: EPHEMERAL, content: "Sorry, only the owner of this stock watcher can use its commands." } });
  }

  const name = i.data && i.data.name;
  // Defer (shows "thinking…" privately), then edit the reply when the work is done
  const followUp = async (work) => {
    let data;
    try {
      data = await work();
    } catch (err) {
      data = { content: `Something went wrong: ${err}` };
    }
    await fetch(`${DISCORD_API}/webhooks/${env.DISCORD_APP_ID}/${i.token}/messages/@original`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowed_mentions: { parse: [] }, ...data }),
    });
  };
  const deferred = () => reply({ type: 5, data: { flags: EPHEMERAL } });

  if (name === "password") {
    // The password itself is never stored or echoed: only the shop's session cookie is kept
    const pw = ((i.data.options || []).find((o) => o.name === "password") || {}).value || "";
    ctx.waitUntil(
      followUp(async () => {
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

  if (name === "status") {
    const meta = await getMeta(env);
    const hasPw = !!(await env.STATE.get("cookie"));
    return reply({ type: 4, data: { flags: EPHEMERAL, content: statusLines(meta).join("\n") + `\nSaved shop password: ${hasPw ? "yes" : "no"}` } });
  }

  return reply({ type: 4, data: { flags: EPHEMERAL, content: "I don't know that command." } });
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
    variants: vars,
  };
}

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
      out.push([item.available ? "new" : "new-soon", item, null]);
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
  if (!live) vids = Object.entries(item.variants); // not on sale yet: links work once it goes live
  const note = live ? "" : "\n*Not on sale yet: these work once it goes live.*";
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
  fields.push({ name: "Stock", value: item.available ? "In stock ✅" : "Not available yet ❌", inline: true });
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

const KIND_ICON = { new: "🆕", "new-soon": "🔜", restock: "🔁", option: "➕" };
const KIND_TEXT = { new: "New", "new-soon": "Not on sale yet", restock: "Back in stock", option: "New option" };

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
