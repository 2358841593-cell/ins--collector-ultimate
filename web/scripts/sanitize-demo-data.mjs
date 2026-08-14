import fs from "node:fs";
import path from "node:path";

const webRoot = path.resolve(import.meta.dirname, "..");
const inputFile = path.resolve(
  process.argv[2] ?? path.join(webRoot, ".private/demo-creators.json"),
);
const outputFile = path.resolve(
  process.argv[3] ?? path.join(webRoot, "app/demo-creators.json"),
);

if (!fs.existsSync(inputFile)) {
  throw new Error(`Private demo data not found: ${inputFile}`);
}

const source = JSON.parse(fs.readFileSync(inputFile, "utf8"));
if (!Array.isArray(source.creators) || source.creators.length < 8) {
  throw new Error("Private demo data must contain at least 8 creators");
}

const pictures = [
  "/creator-michelle.jpg",
  "/collector-hero-creator-v2.png",
  "/collector-creator-group-v2.png",
];

function syntheticComment(comment, index) {
  return {
    ...comment,
    username: `demo_viewer_${String(index + 1).padStart(2, "0")}`,
    translatedZh: "看起来很适合日常使用。",
    originalText: "This looks useful for an everyday routine.",
    postUrl: "https://www.instagram.com/",
  };
}

function publicCreator(creator, index) {
  const copy = structuredClone(creator);
  delete copy.isGoldenSeed;
  delete copy.clientStatus;
  delete copy.approvedAt;
  copy.handle = `demo_creator_${String(index + 1).padStart(2, "0")}`;
  copy.fullName = `Demo Creator ${String(index + 1).padStart(2, "0")}`;
  copy.picture = pictures[index % pictures.length];
  copy.instagramUrl = "https://www.instagram.com/";
  copy.biography = "Synthetic creator fixture for repository builds and UI tests.";
  copy.decisionSummary = "Synthetic fixture · no customer decision";
  copy.contactsHasEmail = false;
  copy.discoveryBatch = "PUBLIC-FIXTURE";
  copy.discoverySources = ["synthetic_fixture"];
  copy.discoveredVia = "synthetic_fixture";
  copy.storefront = {
    ...copy.storefront,
    status: "not_collected",
    type: null,
    url: null,
    bioLinks: [],
  };
  copy.commerce = {
    ...copy.commerce,
    brandCollaborations: [],
  };
  copy.intent = {
    ...copy.intent,
    comments: (copy.intent?.comments ?? [])
      .slice(0, 2)
      .map(syntheticComment),
  };
  copy.audience = {
    ...copy.audience,
    mentions: [],
    hashtags: [],
  };
  copy.evidence = {
    ...copy.evidence,
    modashReport: null,
    sponsoredPosts: [],
    pricing: {
      ...copy.evidence?.pricing,
      reels: [],
    },
    pricingReelSamples: (copy.evidence?.pricingReelSamples ?? [])
      .slice(0, 2)
      .map((sample) => ({
        ...sample,
        url: "https://www.instagram.com/",
        caption: "Synthetic public fixture post.",
      })),
  };
  copy.scoring = {
    ...copy.scoring,
    reviewReasons: [],
    excludeReasons: [],
    missingData: [],
    codes: [],
  };
  return copy;
}

const fixture = {
  generatedAt: "2026-08-14T00:00:00Z",
  sourceRun: "PUBLIC-FIXTURE",
  goldenSeedCount: 0,
  discoveryCount: 8,
  creators: source.creators.slice(0, 8).map(publicCreator),
};

const serialized = `${JSON.stringify(fixture, null, 2)}\n`;
for (const forbidden of [
  '"clientStatus"',
  '"approvedAt"',
  '"isGoldenSeed"',
  "客户已批准",
  "SKIN6-",
]) {
  if (serialized.includes(forbidden)) {
    throw new Error(`Public fixture still contains forbidden marker: ${forbidden}`);
  }
}

fs.writeFileSync(outputFile, serialized);
console.log(`Wrote ${fixture.creators.length} synthetic creators to ${outputFile}`);
