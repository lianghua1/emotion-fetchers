/**
 * A single follower, normalized to the shape the scorer understands.
 *
 * This is deliberately a plain data object with no network or platform
 * coupling — you bring the data (from the X API, a CSV, a database, anywhere),
 * the library judges it. Every field except `id`/`username`/`name` is optional
 * so partial data degrades gracefully rather than throwing.
 */
export interface FollowerData {
  id: string;
  username: string;
  name: string;
  description?: string;
  profile_image_url?: string;
  /** ISO 8601 account-creation timestamp, e.g. "2019-03-01T00:00:00.000Z". */
  created_at?: string;
  public_metrics?: {
    followers_count: number;
    following_count: number;
    tweet_count: number;
    listed_count: number;
  };
  verified?: boolean;
  verified_type?: string;
}

export type FollowerClassification = 'real' | 'suspicious' | 'bot';

/** One scored follower: the numeric score, its class, and why. */
export interface ScoredFollower extends FollowerData {
  score: number;
  classification: FollowerClassification;
  reasons: string[];
}

/** Aggregate quality report for a set of followers. */
export interface FollowerAudit {
  followersScored: number;
  /** Mean per-follower score, 0–100. */
  qualityScore: number;
  /** Percentage split across classes (each 0–100, one decimal place). */
  breakdown: { real: number; suspicious: number; bot: number };
  /** Human-readable, cohort-level warning strings (may be empty). */
  redFlags: string[];
  /** Plain-language summary keyed off `qualityScore`. */
  verdict: string;
  /** The worst-scoring non-real accounts, for spot-checking. */
  flagged: Array<{
    username: string;
    name: string;
    score: number;
    classification: FollowerClassification;
    reasons: string[];
  }>;
}
