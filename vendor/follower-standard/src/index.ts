/**
 * follower-standard — an open, transparent scorer for X/Twitter follower
 * authenticity. The reference implementation of the TweetBoost Follower
 * Standard (https://tweetboost.ai/standard).
 *
 * Pure functions, zero network, zero secrets. Bring the follower data; the
 * library decides how real it looks — and shows its work.
 */
export type {
  FollowerData,
  FollowerClassification,
  ScoredFollower,
  FollowerAudit,
} from './types.js';
export { FOLLOWER_STANDARD, BIO_IS_NOT_A_BOT_SIGNAL } from './standard.js';
export { scoreFollower, classifyFollower, buildReasons } from './score.js';
export { auditFollowers, scoreFollowerFull } from './audit.js';
export { normalizeXUser, type RawXUser } from './normalize.js';
