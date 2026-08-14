import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${pathname}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the Collector product home", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>COLLECTOR/);
  assert.match(html, /告诉 COLLECTOR/);
  assert.match(html, /class="headline-keep">要找谁，/);
  assert.match(html, /传统第三方发现服务/);
  assert.match(html, /Agent 接手整条业务链/);
  assert.match(html, /本地资产/);
  assert.match(html, /collector-hero-creator-v2/);
  assert.match(html, /creator-michelle\.jpg/);
  assert.match(html, /体验 Discovery/);
  assert.match(html, /href="\/discovery"/);
  assert.doesNotMatch(html, /codex-preview|Building your site|react-loading-skeleton/i);
});

test("server-renders the gated Discovery workspace", async () => {
  const response = await render("/discovery");
  assert.equal(response.status, 200);
  const html = await response.text();

  assert.match(html, /运行后，/);
  assert.match(html, /再显示创作者/);
  assert.match(html, /discovery-creator-studio-v1\.webp/);
  assert.match(html, /运行默认 Discovery/);
  assert.match(html, /亚马逊导购类/);
  assert.match(html, /体验版仅支持下方默认条件/);
  assert.doesNotMatch(html, /为你匹配的创作者<\/h2>/);
});

test("keeps product copy independent from referenced vendors", async () => {
  const [home, discovery, layout] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/discovery/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(home, /AGENT EXECUTION MAP/);
  assert.match(home, /团队自己的本地达人数据库/);
  assert.match(home, /repo-flow-svg/);
  assert.match(home, /证据完整？/);
  assert.match(home, /仅补失败项/);
  assert.doesNotMatch(home, /亚马逊导购|Amazon/);
  assert.match(discovery, /DiscoveryIdle/);
  assert.match(layout, /COLLECTOR/);
  assert.doesNotMatch(`${home}\n${discovery}`, /EasyKOL|Modash/i);
});

test("repository fixture contains synthetic creators only", async () => {
  const fixture = JSON.parse(
    await readFile(new URL("../app/demo-creators.json", import.meta.url), "utf8"),
  );
  const serialized = JSON.stringify(fixture);

  assert.equal(fixture.sourceRun, "PUBLIC-FIXTURE");
  assert.equal(fixture.creators.length, 8);
  assert.ok(fixture.creators.every((creator) => /^demo_creator_\d{2}$/.test(creator.handle)));
  assert.doesNotMatch(
    serialized,
    /clientStatus|approvedAt|isGoldenSeed|客户已批准|SKIN6-/,
  );
});
