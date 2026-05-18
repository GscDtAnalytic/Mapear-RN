import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export const fetchOverview = (days: number) =>
  api.get("/overview", { params: { days } }).then((r) => r.data);

export const fetchRanking = (days: number) =>
  api.get("/candidates/ranking", { params: { days } }).then((r) => r.data);

export const fetchSourceSplit = (days: number) =>
  api.get("/candidates/source-split", { params: { days } }).then((r) => r.data);

export const fetchGroupComparison = (days: number) =>
  api.get("/candidates/group-comparison", { params: { days } }).then((r) => r.data);

export const fetchEngagement = (days: number) =>
  api.get("/candidates/engagement", { params: { days } }).then((r) => r.data);

export const fetchMayors = (days: number) =>
  api.get("/candidates/mayors", { params: { days } }).then((r) => r.data);

export const fetchMayorEndorsements = (days: number) =>
  api.get("/candidates/mayor-endorsements", { params: { days } }).then((r) => r.data);

export const fetchWeekly = (days: number) =>
  api.get("/trends/weekly", { params: { days } }).then((r) => r.data);

export const fetchDailySentiment = (days: number) =>
  api.get("/trends/daily-sentiment", { params: { days } }).then((r) => r.data);

export const fetchSpikes = (days: number, zMin: number) =>
  api.get("/trends/spikes", { params: { days, z_min: zMin } }).then((r) => r.data);

export const fetchCities = (days: number) =>
  api.get("/coverage/cities", { params: { days } }).then((r) => r.data);

export const fetchFeeds = (days: number) =>
  api.get("/coverage/feeds", { params: { days } }).then((r) => r.data);

export const fetchPlatforms = (days: number) =>
  api.get("/coverage/platforms", { params: { days } }).then((r) => r.data);

export const fetchSchedule = (days: number) =>
  api.get("/coverage/schedule", { params: { days } }).then((r) => r.data);

export const fetchSentimentPct = (days: number, conf: number) =>
  api.get("/alerts/sentiment-pct", { params: { days, conf } }).then((r) => r.data);

export const fetchTopics = (days: number, mode: string) =>
  api.get("/alerts/topics", { params: { days, mode } }).then((r) => r.data);

export const fetchQuality = (days: number) =>
  api.get("/alerts/quality", { params: { days } }).then((r) => r.data);

export const fetchClusters = (days: number) =>
  api.get("/narratives/clusters", { params: { days } }).then((r) => r.data);

export const fetchRecentArticles = (days: number) =>
  api.get("/narratives/recent-articles", { params: { days } }).then((r) => r.data);

export const postSearch = (query: string, k = 5) =>
  api.post("/narratives/search", { query, k }).then((r) => r.data);
