export interface Candidate {
  person_name: string;
  person_party: string | null;
  mentions: number;
  fav: number;
  warn: number;
  alert: number;
  avg_sentiment?: number | null;
}

export interface KPIs {
  total: number; prev_total: number;
  rss: number;   prev_rss: number;
  social: number; prev_social: number;
  persons: number; prev_persons: number;
  anomalies: number;
}

export interface Hero {
  person_name: string;
  person_party: string | null;
  mentions: number;
  prev_mentions: number | null;
}

export interface MapCity {
  city: string;
  mentions: number;
  latitude: number;
  longitude: number;
  mayor: string;
  party: string;
  population: number;
}

export interface Anomaly {
  person_name: string;
  person_role: string;
  day: string;
  mentions: number;
  zscore: number;
}

export interface OverviewData {
  phase: string;
  days_to_first_round: number | null;
  freshness: string;
  hero: Hero | null;
  kpis: KPIs;
  map_data: MapCity[];
  candidates: Candidate[];
  anomalies: Anomaly[];
}

export interface WeeklyMention {
  person_name: string;
  person_role: string;
  week_start: string;
  mentions_total: number;
  electoral_phase: string;
}

export interface DailySentiment {
  person_name: string;
  day: string;
  sentiment_label: "FAVORABLE" | "WARNING" | "ALERT";
  n: number;
}

export interface CityData {
  city: string; mentions: number; population: number;
  mayor: string; party: string;
  latitude: number; longitude: number;
}

export interface FeedData  { source_feed: string; articles: number; }
export interface PlatformData { platform: string; platform_category: string; events: number; }
export interface ScheduleData { hour: number; dow_num: number; dow_name: string; articles: number; }

export interface SentimentPct { person_name: string; sentiment_label: string; n: number; }
export interface TopicData { topic_label: string; critical_count: number; }
export interface QualityData {
  person_name: string; person_role: string; platform: string;
  avg_conf: number; avg_res_conf: number; n: number;
}

export interface ClusterData {
  cluster_id: number; cluster_label: string;
  article_count: number; centroid_title: string | null; cluster_run_date: string;
}

export interface SearchResult {
  answer: string;
  sources: Array<{
    content_hash: string;
    narrative_summary: string;
    published_at: string;
    title: string;
    source_feed: string;
    distance: number;
  }>;
  query: string;
}

export interface Mayor {
  person_name: string;
  person_city: string;
  person_party: string | null;
  supports_candidate: string | null;
  mentions: number;
  avg_sentiment: number | null;
  fav: number;
  warn: number;
  alert: number;
}

export interface MayorEndorsement {
  city: string;
  mayor_name: string;
  mayor_party: string | null;
  endorsed_candidate: string;
  endorsement_source: "manual" | "llm";
  manual_override: string | null;
  llm_candidate: string | null;
  llm_confidence: "alta" | "media" | "baixa" | null;
  llm_rationale: string | null;
  article_count: number | null;
  endorsement_model: string | null;
  investigated_at: string | null;
}

export type ElectoralPhase =
  | "pre_campaign" | "campaign_1st" | "between_rounds"
  | "campaign_2nd" | "post_election" | "none";
