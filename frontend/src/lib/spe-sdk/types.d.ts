/**
 * SoulPrint Dynamic Intelligence SDK — TypeScript Definitions
 */

// ═══════════════════════════════════════════════════════════════════════════════
// MODEL TYPES
// ═══════════════════════════════════════════════════════════════════════════════

export interface Model {
  key: string;
  name: string;
  provider: string;
  group?: string;
  tier: 'standard' | 'premium';
  strengths: string[];
}

export interface ModelRegistryConfig {
  chat?: Model[];
  image?: Model[];
  video?: Model[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// INTENT TYPES
// ═══════════════════════════════════════════════════════════════════════════════

export type IntentType = 'chat' | 'image' | 'video' | 'image_edit' | 'refused';

export type ChatSubtype = 'question' | 'task' | 'creative' | 'code' | 'analysis' | 'web_search' | 'math' | 'general';
export type ImageSubtype = 'infographic' | 'logo' | 'photo' | 'art' | 'diagram' | 'general';
export type VideoSubtype = 'cinematic' | 'talking' | 'dance' | 'animation' | 'general';
export type ImageEditSubtype = 'aspect_ratio' | 'mask_edit' | 'style_change' | 'general';

export interface IntentResult {
  type: IntentType;
  subtype: ChatSubtype | ImageSubtype | VideoSubtype | ImageEditSubtype | null;
  confidence: 'high' | 'medium' | 'default';
}

// ═══════════════════════════════════════════════════════════════════════════════
// ROUTING TYPES
// ═══════════════════════════════════════════════════════════════════════════════

export interface ModelSelectionResult {
  model: Model;
  reason: string;
  confidence: 'user-selected' | 'high' | 'medium' | 'default' | 'fallback';
}

export interface RouteResult {
  intent: IntentResult;
  model: Model | null;
  reason: string;
  confidence: string;
}

export interface RouteContext {
  hasImageAttachment?: boolean;
  lastImageUrl?: string | null;
  hasVideoAttachment?: boolean;
}

export interface RouteOptions {
  preferredModel?: string;
  tier?: 'premium' | 'standard' | 'any';
}

export interface RouteInput {
  message: string;
  context?: RouteContext;
  options?: RouteOptions;
}

// ═══════════════════════════════════════════════════════════════════════════════
// FUNCTION SIGNATURES
// ═══════════════════════════════════════════════════════════════════════════════

// Main
export function route(input: RouteInput): RouteResult;

// Intent
export function detectIntent(message: string, context?: RouteContext): IntentResult;

// Model Selection
export function selectChatModel(message: string, options?: RouteOptions): ModelSelectionResult;
export function selectImageModel(prompt: string, options?: { preferredModel?: string }): ModelSelectionResult;
export function selectVideoModel(prompt: string, options?: { preferredModel?: string }): ModelSelectionResult;

// Registry
export function setModels(models: ModelRegistryConfig): void;
export function resetModels(): void;
export function getChatModels(): Model[];
export function getImageModels(): Model[];
export function getVideoModels(): Model[];
export function getChatModel(key: string): Model | undefined;
export function getImageModel(key: string): Model | undefined;
export function getVideoModel(key: string): Model | undefined;
export function getPremiumChatModels(): Model[];
export function getStandardChatModels(): Model[];
export function getModelsByStrength(strength: string): { chat: Model[]; image: Model[]; video: Model[] };
