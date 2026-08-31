import type { FollowerData } from './types.js';

/**
 * A loose "user" object as returned by the various X/Twitter follower
 * endpoints. Field names differ between API v1.1, v2, and third-party mirrors,
 * so every field is optional and multiple aliases are accepted.
 */
export interface RawXUser {
  id?: string;
  name?: string;
  userName?: string;
  screen_name?: string;
  description?: string;
  url?: string | null;
  location?: string;
  profilePicture?: string;
  profile_image_url?: string;
  profile_image_url_https?: string;
  createdAt?: string;
  created_at?: string;
  followers?: number;
  followers_count?: number;
  following?: number;
  following_count?: number;
  friends_count?: number;
  statusesCount?: number;
  statuses_count?: number;
  isVerified?: boolean;
  isBlueVerified?: boolean;
  verified?: boolean;
  verifiedType?: string | null;
}

/**
 * Map a raw X/Twitter user object into the scorer's {@link FollowerData}
 * shape, tolerating the naming differences across API versions and mirrors.
 * Anything the scorer doesn't understand is simply dropped.
 */
export function normalizeXUser(f: RawXUser): FollowerData {
  return {
    id: f.id || f.userName || f.screen_name || '',
    username: f.userName || f.screen_name || '',
    name: f.name || '',
    description: f.description || '',
    profile_image_url: f.profilePicture || f.profile_image_url || f.profile_image_url_https || '',
    created_at: f.createdAt || f.created_at || '',
    public_metrics: {
      followers_count: f.followers ?? f.followers_count ?? 0,
      following_count: f.following ?? f.following_count ?? f.friends_count ?? 0,
      tweet_count: f.statusesCount ?? f.statuses_count ?? 0,
      listed_count: 0,
    },
    verified: !!(f.isVerified || f.isBlueVerified || f.verified),
    verified_type: f.verifiedType || undefined,
  };
}
