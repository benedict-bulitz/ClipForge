export type CreateOptions = Record<string, string | number | boolean | null>;

export const CREATION_PREFERENCES_KEY = "clipforge-create-preferences";
export const CREATION_PREFERENCES_VERSION = 1;

export const DEFAULT_CREATE_OPTIONS: CreateOptions = {
  language: "auto",
  aspect_ratio: "9:16",
  style: "documentary",
  platform: "auto",
  min_duration: null,
  max_duration: 60,
  pacing: "fast",
  attention_density: "normal",
  attention_interval_seconds: 1.5,
  research: "auto",
  voice_id: "marin",
  voice_presentation: "neutral",
  voice_tone: "warm",
  voice_speed: 1,
  captions_enabled: true,
  caption_style: "karaoke",
  caption_position: "lower",
  caption_font_size: 72,
  caption_text_color: "#ffffff",
  caption_highlight_color: "#ff6838",
  caption_words_per_group: 4,
  music_enabled: true,
  music_mood: null,
  music_volume: 0.14,
  music_ducking: true,
  music_fades: true,
};

const allowed = new Set(Object.keys(DEFAULT_CREATE_OPTIONS));
const choices: Record<string, readonly string[]> = {
  language: ["auto", "en", "de"],
  aspect_ratio: ["9:16", "1:1", "16:9"],
  style: ["documentary", "cinematic", "editorial"],
  platform: ["auto", "tiktok", "instagram", "youtube"],
  pacing: ["slow", "balanced", "fast"],
  research: ["auto", "on", "off"],
  voice_id: ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar"],
  voice_presentation: ["neutral", "masculine", "feminine"],
  voice_tone: ["warm", "calm", "energetic", "deep", "documentary", "conversational"],
  caption_style: ["clean", "bold", "minimal", "pop", "boxed", "outline", "karaoke"],
  caption_position: ["upper", "center", "lower"],
  music_mood: ["ambient", "documentary", "tech", "cinematic"],
};

export function sanitizeCreatePreferences(value: unknown): CreateOptions {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ...DEFAULT_CREATE_OPTIONS };
  const source = value as Record<string, unknown>;
  const candidate = source.version === CREATION_PREFERENCES_VERSION && source.preferences && typeof source.preferences === "object"
    ? source.preferences as Record<string, unknown>
    : source;
  const clean: CreateOptions = { ...DEFAULT_CREATE_OPTIONS };
  for (const [key, item] of Object.entries(candidate)) {
    if (!allowed.has(key)) continue;
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean" || item === null) clean[key] = item;
  }
  for (const [key, values] of Object.entries(choices)) {
    if (key === "music_mood" && clean[key] === null) continue;
    if (!values.includes(String(clean[key]))) clean[key] = DEFAULT_CREATE_OPTIONS[key];
  }
  if (typeof clean.max_duration !== "number" || clean.max_duration < 10 || clean.max_duration > 180) clean.max_duration = 60;
  if (clean.min_duration !== null && (typeof clean.min_duration !== "number" || clean.min_duration < 10 || clean.min_duration > 180)) clean.min_duration = null;
  if (typeof clean.min_duration === "number" && clean.min_duration > Number(clean.max_duration)) clean.min_duration = null;
  if (typeof clean.voice_speed !== "number" || clean.voice_speed < 0.7 || clean.voice_speed > 1.4) clean.voice_speed = 1;
  if (typeof clean.caption_font_size !== "number" || clean.caption_font_size < 32 || clean.caption_font_size > 112) clean.caption_font_size = 72;
  if (typeof clean.caption_words_per_group !== "number" || clean.caption_words_per_group < 2 || clean.caption_words_per_group > 8) clean.caption_words_per_group = 4;
  if (typeof clean.music_volume !== "number" || clean.music_volume < 0 || clean.music_volume > 1) clean.music_volume = 0.14;
  for (const key of ["captions_enabled", "music_enabled", "music_ducking", "music_fades"]) {
    if (typeof clean[key] !== "boolean") clean[key] = DEFAULT_CREATE_OPTIONS[key];
  }
  for (const key of ["caption_text_color", "caption_highlight_color"]) {
    if (!/^#[0-9a-f]{6}$/i.test(String(clean[key]))) clean[key] = DEFAULT_CREATE_OPTIONS[key];
  }
  return clean;
}

export function serializeCreatePreferences(options: CreateOptions): string {
  const preferences: CreateOptions = {};
  for (const [key, value] of Object.entries(options)) {
    if (allowed.has(key)) preferences[key] = value;
  }
  return JSON.stringify({ version: CREATION_PREFERENCES_VERSION, preferences });
}

export function parseCreatePreferences(raw: string | null): CreateOptions {
  if (!raw) return { ...DEFAULT_CREATE_OPTIONS };
  try {
    return sanitizeCreatePreferences(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_CREATE_OPTIONS };
  }
}

export function loadCreatePreferences(): CreateOptions {
  if (typeof window === "undefined") return { ...DEFAULT_CREATE_OPTIONS };
  try {
    return parseCreatePreferences(window.localStorage.getItem(CREATION_PREFERENCES_KEY));
  } catch {
    return { ...DEFAULT_CREATE_OPTIONS };
  }
}

export function saveCreatePreferences(options: CreateOptions) {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(CREATION_PREFERENCES_KEY, serializeCreatePreferences(options));
  } catch {
    // Private browsing or storage policy may disable local preferences.
  }
}

export function resetCreatePreferences(): CreateOptions {
  try {
    if (typeof window !== "undefined") window.localStorage.removeItem(CREATION_PREFERENCES_KEY);
  } catch {
    // Defaults still apply even when storage is unavailable.
  }
  return { ...DEFAULT_CREATE_OPTIONS };
}

export function resolvePreviewLanguage(prompt: string, choice: unknown): "en" | "de" {
  if (choice === "de") return "de";
  if (choice === "en") return "en";
  const words = prompt.toLocaleLowerCase().match(/[a-zäöüß]+/g) ?? [];
  const german = new Set(["wieso", "warum", "wird", "nicht", "unser", "unsere", "wenn", "darauf", "gebaut", "die", "der", "das", "und"]);
  const english = new Set(["why", "what", "how", "does", "our", "when", "the", "and", "is"]);
  return words.filter((word) => german.has(word)).length > words.filter((word) => english.has(word)).length ? "de" : "en";
}
