/**
 * Score a follower JSON dump with follower-standard.
 * Usage: node scripts/score_followers.mjs <followers.json>
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeXUser,
  auditFollowers,
  scoreFollowerFull,
} from "../vendor/follower-standard/dist/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

function main() {
  const input = process.argv[2];
  if (!input) {
    console.error("Usage: node scripts/score_followers.mjs <followers.json>");
    process.exit(2);
  }
  const raw = JSON.parse(readFileSync(resolve(input), "utf8"));
  const users = Array.isArray(raw) ? raw : raw.followers || raw.users || [];
  const followers = users.map(normalizeXUser);
  const audit = auditFollowers(followers);
  const scored = followers
    .map(scoreFollowerFull)
    .sort((a, b) => a.score - b.score);

  const out = {
    handle: raw.handle || raw.screen_name || null,
    sampled: followers.length,
    profile: raw.profile || null,
    audit,
    worst: scored.slice(0, 15).map((f) => ({
      username: f.username,
      score: f.score,
      classification: f.classification,
      reasons: f.reasons,
      metrics: f.public_metrics,
    })),
  };
  console.log(JSON.stringify(out, null, 2));
}

main();
