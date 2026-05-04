/**
 * Model Registry — Single source of truth for all available AI models.
 * 
 * Each model has:
 *   key        — unique identifier used in API calls
 *   name       — human-readable display name
 *   provider   — API provider (openai, anthropic, google, perplexity, kimi, kie)
 *   tier       — 'standard' (free/cheap) or 'premium' (flagship/expensive)
 *   strengths  — array of keywords describing what the model excels at
 *   group      — UI grouping label
 * 
 * Customize by calling setModels() or by editing the defaults below.
 */

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT MODELS
// ═══════════════════════════════════════════════════════════════════════════════

const DEFAULT_CHAT_MODELS = [
  // OpenAI
  { key: 'gpt-5.2',      name: 'GPT-5.2 (Latest)',      provider: 'openai',      group: 'OpenAI',     tier: 'premium',  strengths: ['reasoning', 'analysis', 'coding', 'math', 'general'] },
  { key: 'gpt-5',        name: 'GPT-5',                 provider: 'openai',      group: 'OpenAI',     tier: 'premium',  strengths: ['reasoning', 'analysis', 'coding', 'general'] },
  { key: 'o3',           name: 'o3 (Reasoning)',         provider: 'openai',      group: 'OpenAI',     tier: 'premium',  strengths: ['deep-reasoning', 'math', 'logic', 'science', 'coding'] },
  { key: 'o3-mini',      name: 'o3 Mini',               provider: 'openai',      group: 'OpenAI',     tier: 'standard', strengths: ['reasoning', 'math', 'fast'] },
  { key: 'gpt-4.1',      name: 'GPT-4.1',               provider: 'openai',      group: 'OpenAI',     tier: 'premium',  strengths: ['coding', 'instruction-following', 'long-context'] },
  { key: 'gpt-4o',       name: 'GPT-4o',                provider: 'openai',      group: 'OpenAI',     tier: 'premium',  strengths: ['multimodal', 'vision', 'general', 'fast'] },
  { key: 'gpt-4o-mini',  name: 'GPT-4o Mini',           provider: 'openai',      group: 'OpenAI',     tier: 'standard', strengths: ['fast', 'cheap', 'general'] },
  // Anthropic
  { key: 'claude-opus-4-5',   name: 'Claude Opus 4.5',   provider: 'anthropic', group: 'Anthropic', tier: 'premium',  strengths: ['writing', 'nuance', 'creative', 'analysis', 'long-context'] },
  { key: 'claude-sonnet-4-5', name: 'Claude Sonnet 4.5',  provider: 'anthropic', group: 'Anthropic', tier: 'premium',  strengths: ['writing', 'coding', 'analysis', 'balanced'] },
  { key: 'claude-haiku-3-5',  name: 'Claude Haiku 3.5',   provider: 'anthropic', group: 'Anthropic', tier: 'standard', strengths: ['fast', 'cheap', 'concise'] },
  // Google Gemini
  { key: 'gemini-2.5-pro',    name: 'Gemini 2.5 Pro',    provider: 'gemini',    group: 'Google',    tier: 'premium',  strengths: ['multimodal', 'research', 'long-context', 'vision'] },
  { key: 'gemini-2.0-flash',  name: 'Gemini 2.0 Flash',  provider: 'gemini',    group: 'Google',    tier: 'standard', strengths: ['fast', 'multimodal', 'cheap'] },
  // Perplexity (online — built-in web search)
  { key: 'sonar-pro',         name: 'Sonar Pro (Online)', provider: 'perplexity', group: 'Perplexity', tier: 'premium',  strengths: ['web-search', 'real-time', 'citations', 'research'] },
  { key: 'sonar',             name: 'Sonar (Online)',     provider: 'perplexity', group: 'Perplexity', tier: 'standard', strengths: ['web-search', 'real-time', 'fast'] },
  { key: 'sonar-reasoning',   name: 'Sonar Reasoning',   provider: 'perplexity', group: 'Perplexity', tier: 'premium',  strengths: ['web-search', 'reasoning', 'deep-research'] },
  // Kimi (MoonShot AI)
  { key: 'kimi-k2',           name: 'Kimi K2 (Flagship)', provider: 'kimi',      group: 'Kimi',      tier: 'premium',  strengths: ['long-context', 'multilingual', 'analysis'] },
];

// ═══════════════════════════════════════════════════════════════════════════════
// IMAGE MODELS
// ═══════════════════════════════════════════════════════════════════════════════

const DEFAULT_IMAGE_MODELS = [
  { key: 'nano-banana',       name: 'Nano Banana',         provider: 'kie',    tier: 'premium',  strengths: ['photorealistic', 'portrait', 'nature', 'general', 'hd'] },
  { key: 'imagen-4-ultra',    name: 'Imagen 4 Ultra',      provider: 'kie',    tier: 'premium',  strengths: ['artistic', 'creative', 'fantasy', 'illustration', 'high-quality'] },
  { key: 'seedream-5-lite',   name: 'Seedream 5 Lite',     provider: 'kie',    tier: 'standard', strengths: ['text-rendering', 'logo', 'typography', 'fast', 'poster', 'sign'] },
  { key: 'gpt-image-1-5',    name: 'GPT Image 1.5',       provider: 'openai', tier: 'premium',  strengths: ['product', 'commercial', 'clean', 'precise', 'instruction-following'] },
  { key: 'flux-pro',          name: 'Flux Pro',            provider: 'kie',    tier: 'premium',  strengths: ['photorealistic', 'detailed', 'professional'] },
  { key: 'ideogram-3',        name: 'Ideogram 3',          provider: 'kie',    tier: 'premium',  strengths: ['text-rendering', 'typography', 'logo', 'design'] },
];

// ═══════════════════════════════════════════════════════════════════════════════
// VIDEO MODELS
// ═══════════════════════════════════════════════════════════════════════════════

const DEFAULT_VIDEO_MODELS = [
  { key: 'kling-3',        name: 'Kling 3.0',          provider: 'kie',    tier: 'standard', strengths: ['general', 'fast', 'affordable', 'versatile'] },
  { key: 'sora-2',         name: 'Sora 2',             provider: 'openai', tier: 'premium',  strengths: ['cinematic', 'film', 'realistic', 'high-quality', 'dramatic'] },
  { key: 'wan-2-6',        name: 'Wan 2.6',            provider: 'kie',    tier: 'premium',  strengths: ['lip-sync', 'talking', 'speech', 'dialogue', 'character'] },
  { key: 'seedance-1-5',   name: 'Seedance 1.5',       provider: 'kie',    tier: 'premium',  strengths: ['dance', 'motion', 'choreography', 'action', 'dynamic'] },
  { key: 'veo-3-1',        name: 'Veo 3.1',            provider: 'google', tier: 'premium',  strengths: ['cinematic', 'long-form', 'narrative', 'documentary'] },
  { key: 'kling-3-pro',    name: 'Kling 3.0 Pro',      provider: 'kie',    tier: 'premium',  strengths: ['high-quality', 'detailed', 'professional'] },
];

// ═══════════════════════════════════════════════════════════════════════════════
// MUTABLE STATE — override via setModels()
// ═══════════════════════════════════════════════════════════════════════════════

let chatModels  = [...DEFAULT_CHAT_MODELS];
let imageModels = [...DEFAULT_IMAGE_MODELS];
let videoModels = [...DEFAULT_VIDEO_MODELS];

/**
 * Override default models. Pass only the categories you want to change.
 * @param {{ chat?: Array, image?: Array, video?: Array }} models
 */
export function setModels(models = {}) {
  if (models.chat)  chatModels  = models.chat;
  if (models.image) imageModels = models.image;
  if (models.video) videoModels = models.video;
}

/** Reset to built-in defaults */
export function resetModels() {
  chatModels  = [...DEFAULT_CHAT_MODELS];
  imageModels = [...DEFAULT_IMAGE_MODELS];
  videoModels = [...DEFAULT_VIDEO_MODELS];
}

export function getChatModels()  { return chatModels; }
export function getImageModels() { return imageModels; }
export function getVideoModels() { return videoModels; }

export function getChatModel(key)  { return chatModels.find(m => m.key === key); }
export function getImageModel(key) { return imageModels.find(m => m.key === key); }
export function getVideoModel(key) { return videoModels.find(m => m.key === key); }

export function getPremiumChatModels()  { return chatModels.filter(m => m.tier === 'premium'); }
export function getStandardChatModels() { return chatModels.filter(m => m.tier === 'standard'); }

export function getModelsByStrength(strength) {
  return {
    chat:  chatModels.filter(m => m.strengths.includes(strength)),
    image: imageModels.filter(m => m.strengths.includes(strength)),
    video: videoModels.filter(m => m.strengths.includes(strength)),
  };
}
