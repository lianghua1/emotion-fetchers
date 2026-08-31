import type { FollowerData, FollowerClassification } from './types.js';

/**
 * Score a single follower for authenticity, 0–100 (higher is more real).
 *
 * Every account starts at a perfect 100 and loses points for signals that
 * correlate with automation or throwaway accounts. The weights are deliberate
 * and public — this is a transparent heuristic, not a black box:
 *
 *   - default / missing profile picture   −20
 *   - zero tweets                          −30   (very few, <5)   −15
 *   - very new account   <7d −30   <30d −20   <90d −10
 *   - lopsided follow ratio   >50× −25   >20× −15   >10× −10
 *   - mass-follow shape (5000+ following, <100 followers)   −15
 *   - zero followers while following 100+   −20
 *
 * A missing bio is intentionally NOT penalized — see ./standard.ts.
 */
export function scoreFollower(f: FollowerData): number {
  let score = 100;
  const pm = f.public_metrics || { followers_count: 0, following_count: 0, tweet_count: 0, listed_count: 0 };
  // A missing bio is not evidence of a bot: plenty of long-active, clearly
  // human accounts run without one. We judge realness on activity, age,
  // picture, and follow behaviour instead.
  if (!f.profile_image_url || f.profile_image_url.includes('default_profile')) score -= 20;
  if (pm.tweet_count === 0) score -= 30;
  else if (pm.tweet_count < 5) score -= 15;
  if (f.created_at) {
    const ageDays = Math.floor((Date.now() - new Date(f.created_at).getTime()) / (1000 * 60 * 60 * 24));
    if (ageDays < 7) score -= 30;
    else if (ageDays < 30) score -= 20;
    else if (ageDays < 90) score -= 10;
  }
  const ratio = pm.following_count / Math.max(pm.followers_count, 1);
  if (ratio > 50) score -= 25;
  else if (ratio > 20) score -= 15;
  else if (ratio > 10) score -= 10;
  if (pm.following_count > 5000 && pm.followers_count < 100) score -= 15;
  if (pm.followers_count === 0 && pm.following_count > 100) score -= 20;
  return Math.max(0, Math.min(100, score));
}

/**
 * Bucket a 0–100 score into a class.
 *   score >= 70  → real
 *   score  > 40  → suspicious
 *   otherwise    → bot
 */
export function classifyFollower(score: number): FollowerClassification {
  if (score >= 70) return 'real';
  if (score > 40) return 'suspicious';
  return 'bot';
}

/** The human-readable reasons a follower lost points, for display/audit. */
export function buildReasons(f: FollowerData): string[] {
  const reasons: string[] = [];
  const pm = f.public_metrics || { followers_count: 0, following_count: 0, tweet_count: 0, listed_count: 0 };
  if (!f.profile_image_url || f.profile_image_url.includes('default_profile')) reasons.push('Default profile picture');
  if (pm.tweet_count === 0) reasons.push('Zero tweets');
  else if (pm.tweet_count < 5) reasons.push('Very few tweets');
  if (pm.following_count > 5000 && pm.followers_count < 100) reasons.push('Mass following pattern');
  const ratio = pm.following_count / Math.max(pm.followers_count, 1);
  if (ratio > 20) reasons.push(`Following ${Math.round(ratio)}× more than followers`);
  if (f.created_at) {
    const ageDays = Math.floor((Date.now() - new Date(f.created_at).getTime()) / (1000 * 60 * 60 * 24));
    if (ageDays < 30) reasons.push(`Account only ${ageDays} days old`);
  }
  return reasons;
}
