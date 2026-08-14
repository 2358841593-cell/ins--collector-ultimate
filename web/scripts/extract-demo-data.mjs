import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const projectRoot = path.resolve(import.meta.dirname, "../..");
const runRoot = path.join(projectRoot, "data/runs/SKIN6-20260810");
const sourceFile = path.join(runRoot, "decisions.delivery-20260811-r2-final-v2.json");
const outputFile = path.join(projectRoot, "web/.private/demo-creators.json");

const source = JSON.parse(fs.readFileSync(sourceFile, "utf8"));
const databaseFile = path.join(projectRoot, "data/creator_cache.db");

function modashPicture(handle) {
  const file = path.join(runRoot, "modash_raw", `${handle}.json`);
  if (!fs.existsSync(file)) return null;
  const raw = JSON.parse(fs.readFileSync(file, "utf8"));
  return raw?.profile?.profileData?.profile?.picture ?? null;
}

function cleanUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    url.search = "";
    return url.toString();
  } catch {
    return value;
  }
}

function compactComment(comment, postUrl) {
  return {
    username: comment.username,
    grade: comment.grade,
    gradeZh: comment.grade_zh,
    translatedZh: comment.translated_zh ?? comment.text,
    originalText: comment.original_text ?? comment.text,
    sourceLanguage: comment.source_language,
    translationStatus: comment.translation_status,
    postUrl,
  };
}

function profile(candidate, meta = {}) {
  const comments = (candidate.intent_posts ?? []).flatMap((post) =>
    (post.intent_comments ?? []).map((comment) => compactComment(comment, post.post_url)),
  );

  return {
    handle: candidate.handle,
    fullName: candidate.creator_fullname ?? candidate.full_name,
    picture: modashPicture(candidate.handle),
    instagramUrl: `https://www.instagram.com/${candidate.handle}/`,
    biography: candidate.biography,
    decisionSummary: meta.isGoldenSeed ? "客户已批准 · 金种子" : candidate.decision_summary,
    isGoldenSeed: Boolean(meta.isGoldenSeed),
    clientStatus: meta.clientStatus ?? null,
    approvedAt: meta.approvedAt ?? null,
    finalPool: candidate.final_pool,
    normalizedTotal: candidate.normalized_total,
    aiVettingScore: candidate.ai_vetting_score,
    followerCount: candidate.follower_count,
    postsCount: candidate.posts_count,
    accountType: candidate.account_type,
    isBusiness: candidate.is_business,
    creatorVerified: candidate.creator_verified,
    creatorCountry: candidate.creator_country,
    creatorGender: candidate.creator_gender,
    contactsHasEmail: candidate.contacts_has_email,
    followersGrowthPct: candidate.followers_growth_pct,
    campaignTrack: candidate.campaign_track,
    category: candidate.category,
    coreNicheKey: candidate.core_niche_key,
    discoverySources: candidate.discovery_sources,
    discoveredVia: candidate.discovered_via,
    discoveryBatch: candidate.discovery_batch,
    storefront: {
      status: candidate.storefront_status,
      type: candidate.storefront_type,
      url: cleanUrl(candidate.storefront_url ?? candidate.amazon_storefront_link),
      bioLinks: [...new Set((candidate.bio_links ?? []).map(cleanUrl).filter(Boolean))],
    },
    performance: {
      avgLikes: candidate.avg_likes,
      avgComments: candidate.avg_comments,
      avgReelsPlays: candidate.avg_reels_plays,
      generalEr: candidate.general_er,
      modashEr: candidate.modash_er,
      realEr: candidate.real_er,
      realErMedian: candidate.real_er_median,
      realErWindow: candidate.real_er_window,
      reelsEr: candidate.reels_er,
      staticEr: candidate.static_er,
      meetsErBenchmark: candidate.meets_er_benchmark,
    },
    commerce: {
      amazonFindsRatio: candidate.amazon_finds_ratio,
      sponsorshipSaturation: candidate.sponsorship_saturation,
      promotionalPostCount: candidate.promotional_post_count,
      organicRelevantPosts: candidate.organic_relevant_posts,
      skuCategoriesHit: candidate.sku_categories_hit,
      ingredientsScore: candidate.ingredients_score,
      deviceSpecsScore: candidate.device_specs_score,
      skinScienceScore: candidate.skin_science_score,
      brandCollaborations: candidate.brand_collaborations,
    },
    integrity: {
      fakePct: candidate.fake_pct,
      likersFakePct: candidate.likers_fake_pct,
      lowQualityRatio: candidate.low_quality_ratio,
      audienceTypes: candidate.audience_types,
      audienceReachability: candidate.audience_reachability,
    },
    audience: {
      targetCountriesPct: candidate.target_countries_audience_pct,
      topCountry: candidate.top_audience_country,
      countries: candidate.audience_countries,
      cities: candidate.audience_cities,
      genders: candidate.audience_genders,
      ages: candidate.audience_ages,
      gendersPerAge: candidate.audience_genders_per_age,
      topLanguage: candidate.top_language,
      topLanguagePct: candidate.top_language_pct,
      languages: candidate.audience_languages,
      interests: candidate.audience_interests,
      mentions: candidate.audience_mentions,
      hashtags: candidate.audience_hashtags,
    },
    likers: {
      topCountry: candidate.likers_top_country,
      countries: candidate.likers_countries,
      genders: candidate.likers_genders,
      ages: candidate.likers_ages,
      fakePct: candidate.likers_fake_pct,
    },
    intent: {
      highCount: candidate.high_intent_count,
      total: candidate.intent_total,
      highRatio: candidate.high_intent_ratio,
      byGrade: candidate.intent_by_grade,
      translatedByGrade: candidate.translated_intent_by_grade,
      validComments: candidate.valid_comments,
      commentsRead: candidate.comments_read,
      commentsAnalyzed: candidate.comments_analyzed,
      attemptedPosts: candidate.comment_attempted_posts,
      completedPosts: candidate.comment_completed_posts,
      unavailablePosts: candidate.comment_unavailable_posts,
      comments,
      translationSummary: candidate.comment_translation_summary,
    },
    evidence: {
      deepCollectionStatus: candidate.deep_collection_status,
      deepTargetPosts: candidate.deep_target_posts,
      deepSuccessfulPosts: candidate.deep_successful_posts,
      deepFailedPosts: candidate.deep_failed_posts,
      primaryRefsSource: candidate.primary_refs_source,
      pricingCapturedAt: candidate.pricing_captured_at,
      pricing: candidate.pricing_estimate,
      pricingReelSamples: (candidate.pricing_reel_samples ?? []).map((sample) => ({
        url: sample.url,
        caption: sample.caption ? [...sample.caption].slice(0, 420).join("") : sample.caption,
        playCount: sample.play_count,
        likeCount: sample.like_count,
        commentCount: sample.comment_count,
        pinned: sample.pinned,
      })),
      sponsoredPosts: candidate.sponsored_post_samples,
      commentTranslation: candidate.comment_translation_summary,
      modashReport: candidate.modash_report,
    },
    scoring: {
      modules: candidate.score_by_module,
      gates: candidate.gate_results,
      reviewReasons: candidate.review_reasons,
      excludeReasons: candidate.exclude_reasons,
      missingData: candidate.missing_data,
      codes: candidate.codes,
    },
  };
}

function goldenSeedRows() {
  const sql = `
    SELECT handle, full_name, follower_count, media_count, external_url,
           is_business, category, brand_account_type, core_niche_key,
           storefront_status, amazon_storefront_link, is_private, is_verified,
           biography, seed_followers, modash_er, real_er, high_intent_count,
           final_pool, discovery_batch, stage_json, data_json,
           client_status, approved_at
    FROM creator_profiles
    WHERE tier = 2 OR client_status IN ('approved', 'collaborated')
    ORDER BY approved_at DESC, COALESCE(seed_followers, follower_count, 0) DESC, handle ASC;
  `;
  const raw = execFileSync("sqlite3", ["-json", databaseFile, sql], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  return raw.trim() ? JSON.parse(raw) : [];
}

const goldenCreators = goldenSeedRows().map((row) => {
  const stage = row.stage_json ? JSON.parse(row.stage_json) : {};
  const shallow = row.data_json ? JSON.parse(row.data_json) : {};
  const candidate = {
    ...shallow,
    ...stage,
    handle: row.handle,
    full_name: stage.full_name ?? shallow.full_name ?? row.full_name,
    creator_fullname: stage.creator_fullname ?? shallow.creator_fullname ?? row.full_name,
    follower_count: stage.follower_count ?? row.seed_followers ?? row.follower_count,
    posts_count: stage.posts_count ?? row.media_count,
    external_url: stage.external_url ?? row.external_url,
    is_business: stage.is_business ?? Boolean(row.is_business),
    is_private: stage.is_private ?? Boolean(row.is_private),
    creator_verified: stage.creator_verified ?? Boolean(row.is_verified),
    category: stage.category ?? row.category,
    brand_account_type: stage.brand_account_type ?? row.brand_account_type,
    core_niche_key: stage.core_niche_key ?? row.core_niche_key,
    storefront_status: stage.storefront_status ?? row.storefront_status,
    storefront_url: stage.storefront_url ?? row.amazon_storefront_link,
    biography: stage.biography ?? shallow.biography ?? row.biography,
    modash_er: stage.modash_er ?? row.modash_er,
    real_er: stage.real_er ?? row.real_er,
    high_intent_count: stage.high_intent_count ?? row.high_intent_count,
    final_pool: stage.final_pool ?? row.final_pool ?? "Golden-Seed",
    discovery_batch: stage.discovery_batch ?? row.discovery_batch,
  };
  return profile(candidate, {
    isGoldenSeed: true,
    clientStatus: row.client_status,
    approvedAt: row.approved_at,
  });
});

const goldenHandles = new Set(goldenCreators.map((creator) => creator.handle.toLowerCase()));
const discoveryCreators = source.candidates
  .filter((candidate) => !goldenHandles.has(candidate.handle.toLowerCase()))
  .sort((a, b) => (b.normalized_total ?? -1) - (a.normalized_total ?? -1))
  .map((candidate) => profile(candidate));

const homepageHandles = [
  "beauty.n.furbabyfanatic",
  "oceanekarol",
  "thedorajay",
  "yejj.lee",
  "angiemdf",
  "alenamari",
  "alessandra_zanoni",
  "lauraestada",
];
const homepageOrder = new Map(homepageHandles.map((handle, index) => [handle, index]));

function completeness(creator) {
  return (
    (creator.picture ? 18 : 0) +
    (creator.storefront.url ? 22 : 0) +
    (creator.intent.comments.length ? 12 : 0) +
    (creator.intent.translationSummary?.status === "complete" ? 6 : 0) +
    (creator.audience.countries?.length ? 9 : 0) +
    (creator.audience.ages?.length ? 6 : 0) +
    (creator.audience.genders?.length ? 5 : 0) +
    (creator.integrity.fakePct != null ? 6 : 0) +
    (creator.evidence.pricing?.status === "complete" ? 8 : 0) +
    (creator.evidence.pricingReelSamples?.length ? 5 : 0) +
    (creator.evidence.sponsoredPosts?.length ? 3 : 0)
  );
}

const creators = [...goldenCreators, ...discoveryCreators].sort((a, b) => {
  const aHomepage = homepageOrder.get(a.handle);
  const bHomepage = homepageOrder.get(b.handle);
  if (aHomepage != null || bHomepage != null) {
    if (aHomepage == null) return 1;
    if (bHomepage == null) return -1;
    return aHomepage - bHomepage;
  }
  return completeness(b) - completeness(a) || (b.normalizedTotal ?? -1) - (a.normalizedTotal ?? -1) || a.handle.localeCompare(b.handle);
});

fs.mkdirSync(path.dirname(outputFile), { recursive: true });
fs.writeFileSync(
  outputFile,
  `${JSON.stringify({ generatedAt: source.generated_at, sourceRun: "SKIN6-20260810", goldenSeedCount: goldenCreators.length, discoveryCount: discoveryCreators.length, creators }, null, 2)}\n`,
);

console.log(`Wrote ${creators.length} private creators (${goldenCreators.length} golden seeds + ${discoveryCreators.length} discovery candidates) to ${outputFile}`);
